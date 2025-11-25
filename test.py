#!/usr/bin/env python3
import os
import sys
import time
import syslog
import tempfile
import json

from vyos.utils.dict import dict_search
from vyos.utils.process import cmd
from vyos.utils.process import rc_cmd
from vyos.utils.process import run

DEBOUNCE_MS = 500
STATE_DIR = "/run/frr/evpn-mh"
underlay_iface = ['eth1', 'eth3']

def log_message(message, process, level=syslog.LOG_INFO):   
    syslog.openlog(process, syslog.LOG_PID)
    syslog.syslog(level, message)    
    syslog.closelog()

def atomic_write(path, content):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".evpn_", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def create_token(iface, non_df):
    os.makedirs(STATE_DIR, exist_ok=True)
    token_path = os.path.join(STATE_DIR, f"{iface}.token")
    token = f"{time.time_ns()}:{non_df}"
    return token_path, token

def read_file(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None

def read_token(token_path):
    return read_file(token_path)

def is_latest_token(token_path, token):
    return read_token(token_path) == token

def state_is_unchanged(state_path, non_df):
    existing = read_file(state_path)
    return existing == non_df

def write_non_df_state(state_path, non_df):
    atomic_write(state_path, f"{non_df}\n")

def check_tables(table_name):
    rc, _ = rc_cmd(f"sudo nft list table {table_name}")
    if rc == 0:
        return True
    else:
        return False

def delete_nft_table(table_name):
    run(f"sudo nft delete table {table_name}")

def create_netdev_table(vteps):
    run(f"sudo nft add table netdev evpn_sph")

    run(f"sudo nft add set netdev evpn_sph vteps '{{ type ipv4_addr; flags interval; }}'")
    run(f"sudo nft add element netdev evpn_sph vteps {{ {', '.join(vteps)} }}")

    run(f"sudo nft add chain netdev evpn_sph evpn_sph_ingress '{{ type filter hook ingress devices = {{ {', '.join(underlay_iface)} }} priority 0; policy accept; }}'")

    run(f"sudo nft add rule netdev evpn_sph evpn_sph_ingress ip saddr @vteps udp dport 4789 meta mark set 0x04fc867d counter")

def create_bridge_table():
    run(f"sudo nft add table bridge evpn_sph")
    run(f"sudo nft add chain bridge evpn_sph evpn_sph_forward '{{ type filter hook forward priority 0; policy accept; }}'")

    run(f"sudo nft add rule bridge evpn_sph evpn_sph_forward meta mark 0x04fc867d meta pkttype multicast counter drop")
    run(f"sudo nft add rule bridge evpn_sph evpn_sph_forward meta mark 0x04fc867d meta pkttype broadcast counter drop")

def get_vteps(iface):
    es_data = json.loads(cmd("vtysh -c 'show evpn es detail json' | jq 'map({ (.accessPort): . }) | add'"))
    vteps = []
    if es_data:
        vtep_dict = dict_search(f'{iface}.vteps', es_data)
        for vtep in vtep_dict:
            vteps.append(dict_search('vtep', vtep))
        
    return vteps

def change_flooding(iface, state):
    run(f"sudo bridge link set dev {iface} flood {state}")
    run(f"sudo bridge link set dev {iface} mcast_flood {state}")
    run(f"sudo bridge link set dev {iface} bcast_flood {state}")

def main():
    iface = sys.argv[1] if len(sys.argv) > 1 else ""
    non_df = sys.argv[2] if len(sys.argv) > 2 else ""

    token_path, token = create_token(iface, non_df)
    atomic_write(token_path, token)

    time.sleep(DEBOUNCE_MS / 1000.0)

    if not is_latest_token(token_path, token):
        return 0

    state_path = os.path.join(STATE_DIR, f"{iface}.state")

    if state_is_unchanged(state_path, non_df):
        return 0

    netdev_table_exists = check_tables('netdev evpn_sph')
    bridge_table_exists = check_tables('bridge evpn_sph')

    vteps = get_vteps(iface)

    if not vteps:
        return 0

    if non_df == "1":
        is_df = 'non-df'
        if netdev_table_exists:
            delete_nft_table("netdev evpn_sph")
        if bridge_table_exists:
            delete_nft_table("bridge evpn_sph")
        change_flooding(iface, "off")
    else:
        is_df = 'df'
        if not netdev_table_exists:
            create_netdev_table(vteps)
        if not bridge_table_exists:
            create_bridge_table()
        change_flooding(iface, "on")

    msg = f"SPH filters for {iface} have been set as {is_df}"
    log_message(msg, "frr-evpn-mh")

    write_non_df_state(state_path, non_df)    

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log_message(e, "frr-evpn-error", syslog.LOG_ERR)
        sys.exit(1)
