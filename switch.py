#!/usr/bin/python3
import sys
import struct
import wrapper
import threading
import time
from wrapper import recv_from_any_link, send_to_link, get_switch_mac , get_interface_name

#extrag date dintr-un cadru Ethernet
def parse_ethernet_header(data):
    dest_mac = data[0:6]
    src_mac = data[6:12]
    
    ether_type = (data[12] << 8) + data[13]

    vlan_id = -1 #vlan implicit
    if ether_type == 0x8200: # daca EtherType indica un cadru VLAN
        vlan_tci = int.from_bytes(data[14:16], byteorder='big')
        vlan_id = vlan_tci & 0x0FFF #se citeste vlan id
        ether_type = (data[16] << 8) + data[17]

    return dest_mac, src_mac, ether_type, vlan_id


def create_vlan_tag(vlan_id):
    return struct.pack('!H', 0x8200) + struct.pack('!H', vlan_id & 0x0FFF)

def add_vlan_tag(frame, vlan_id):
    vlan_tag = create_vlan_tag(vlan_id)
    return frame[:12] + vlan_tag + frame[12:]

def remove_vlan_tag(frame):
    return frame[:12] + frame[16:]

def send_bdpu_every_sec():
    while True:
        # TODO Send BDPU every second if necessary
        time.sleep(1)

def is_unicast(mac_address):
    return mac_address not in ["FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00"]

# citire date din switchID.cfg
def get_data_from_config(file_path):
    config_data = None
    try:
        with open(file_path, 'r') as config_file:
            config_data = ''.join(config_file.readlines())
    except FileNotFoundError:
        print(f"[ERROR] Nu s-a găsit fișierul specificat: {file_path}")
    except IOError:
        print(f"[ERROR] Eroare la citirea fișierului: {file_path}")
    return config_data

def main():
    switch_id = sys.argv[1]
    num_interfaces = wrapper.init(sys.argv[2:]) #initializez porturile
    interfaces = range(0, num_interfaces)
    file_data = get_data_from_config(f'configs/switch{switch_id}.cfg').split('\n')

    mac_table = {}
    trunk_ports = []
    interface_types = {}

    # iterez prin interfaces si preiau datele din cfg, ignor prima linie
    for i in interfaces:
        line_split = file_data[i + 1].split(' ')
        if len(line_split) == 2: #verific daca linia este formata exact din doua componente
            interface_name, port_type = line_split
            if port_type == 'T': # daca portul e de tip trunk, nu are VLAN ID si setez tipul
                interface_types[interface_name] = {'is_trunk': True, 'vlan_id': None}
                trunk_ports.append(i)
            else:
                # altfel tipul portului e access, il setez + setare VLAN ID
                interface_types[interface_name] = {'is_trunk': False, 'vlan_id': int(port_type)}

    # Create and start a new thread that deals with sending BDPU

    t = threading.Thread(target=send_bdpu_every_sec)
    t.start()

    while True:
        # Note that data is of type bytes([...]).
        # b1 = bytes([72, 101, 108, 108, 111])  # "Hello"
        # b2 = bytes([32, 87, 111, 114, 108, 100])  # " World"
        # b3 = b1[0:2] + b[3:4].
        interface, data, length = recv_from_any_link()

        dest_mac, src_mac, ethertype, vlan_id = parse_ethernet_header(data)

        # Print the MAC src and MAC dst in human readable format
        dest_mac = ':'.join(f'{b:02x}' for b in dest_mac)
        src_mac = ':'.join(f'{b:02x}' for b in src_mac)

        # obtin informatii despre nume/tipul portului curent si despre VLAN-ul sau
        current_interface = get_interface_name(interface)
        source_is_trunk = interface_types[current_interface]['is_trunk']
        
        # source e access si trebuie sa retin VLAN ID
        if not source_is_trunk:
            vlan_id = interface_types[current_interface]['vlan_id']

        # actualizez tabela MAC cu adresa sursa si VLAN
        mac_table[(src_mac, vlan_id)] = interface

        # verific daca adresa e unicast
        if is_unicast(dest_mac):
            if (dest_mac, vlan_id) in mac_table:
                dest_interface = mac_table[(dest_mac, vlan_id)] #port destinatie
                
                # determin tipul portului de destinatie
                is_dest_trunk = interface_types[get_interface_name(dest_interface)]['is_trunk']

                # trimit de la trunk pe trunk fara modificari
                if is_dest_trunk:
                    if source_is_trunk:
                        send_to_link(dest_interface, length, data)
                    else:
                        # access->trunk, trebuie sa adaug TAG VLAN si dim. creste cu 4 bytes 
                        send_to_link(dest_interface, length + 4, add_vlan_tag(data, vlan_id))
                else:
                    if source_is_trunk:
                        # trunk->access, elimin TAG VLAN, modific dimensiunea
                        send_to_link(dest_interface, length - 4, remove_vlan_tag(data))
                    else:
                        # access->access, se trimite direct
                        send_to_link(dest_interface, length, data)
            else:
                # adresa MAC de destinatie nu se afla in tabela MAC =>flooding
                for o in interfaces:
                    if o != interface: #nu trimit cardul pt unde a venit
                        o_interface_name = get_interface_name(o)
                        is_out_trunk = interface_types[o_interface_name]['is_trunk']
                        
                        if is_out_trunk:
                            if source_is_trunk:
                                send_to_link(o, length, data)
                            else:
                                send_to_link(o, length + 4, add_vlan_tag(data, vlan_id))
                        elif interface_types[o_interface_name]['vlan_id'] == vlan_id: #daca portul de iesire este access
                            if source_is_trunk:
                                send_to_link(o, length - 4, remove_vlan_tag(data))
                            else:
                                send_to_link(o, length, data)
        else:
            # daca adresa MAC nu e unicast => broadcast/multicast
            for o in interfaces:
                if o != interface:
                    o_interface_name = get_interface_name(o)
                    is_out_trunk = interface_types[o_interface_name]['is_trunk']
                    
                    if is_out_trunk:
                        if source_is_trunk:
                            send_to_link(o, length, data)
                        else:
                            send_to_link(o, length + 4, add_vlan_tag(data, vlan_id))
                    elif interface_types[o_interface_name]['vlan_id'] == vlan_id:
                        if source_is_trunk:
                            send_to_link(o, length - 4, remove_vlan_tag(data))
                        else:
                            send_to_link(o, length, data)


if __name__ == "__main__":
    main()
