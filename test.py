#!/usr/bin/env python3
import os
import sys
import time
import json
import signal
import threading

from vyos.utils.dict import dict_search
from vyos.utils.dict import dict_search_args
from vyos.utils.process import cmd
from vyos.utils.process import rc_cmd
from vyos.utils.misc import wait_for
from vyos.template import render

nftables_conf = '/run/nftables_evpn_sph.conf'
evpn_dir = "/run/frr/evpn-mh"
refresh_timer = 30 # seconds

stop_event = threading.Event()

def _handle_termination_signal(signum, frame):
    stop_event.set()

signal.signal(signal.SIGTERM, _handle_termination_signal)
signal.signal(signal.SIGINT, _handle_termination_signal)

def is_process_up(cmd):
    rc, _ = rc_cmd(cmd)
    if rc == 0:
        return True
    else:
        return False

def index_dicts_by_key(path, dict_list, delim="."):
    """
    Build a dictionary keyed by a nested value from each entry.

    Resolves a nested key for each dict in a list and reinserts it into 
    the dict under the new key. Dicts that are missing the key will be
    placed into an "unknown" list of dicts.

    Parameters
    ----------
    path : str
        Nested key path. Path can be separated by other delimiters if 
        the delim argument is provided. 
        (e.g. "interface", "interface.name", or "interface--name--address").        
    dict_list : list[dict]
        Dictionaries to index.
    delim : str, optional
        Path delimiter. Defaults to ".".

    Returns
    -------
    dict
        Mapping of {resolved_value: dict}, optionally with "unknown": [dicts].
    """
    parts = path.split(delim)
    result = {}
    unknown_entries = []

    for entry in dict_list:
        key_value = dict_search_args(entry, *parts)
        if key_value is None:
            unknown_entries.append(entry)
        else:
            result[key_value] = entry

    if unknown_entries:
        result["unknown"] = unknown_entries

    return result    

def load_file_with_mtime(path):
    try:
        stat = os.stat(path)
        mtime = stat.st_mtime
    except FileNotFoundError:
        return None, None

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception:
        # JSON incomplete or corrupted due to write burst
        return None, mtime

    return data, mtime


def get_df_status():
    """
    Returns:
        dict: { ifname: "df" | "non-df" }
        OR {} (empty) if nothing valid found
    """

    status = {}

    for fname in os.listdir(evpn_dir):
        if not fname.startswith("evpn_df_status_") or not fname.endswith(".json"):
            continue

        path = os.path.join(evpn_dir, fname)

        data1, ts1 = load_file_with_mtime(path)
        if data1 is None:
            continue

        time.sleep(0.5)

        data2, ts2 = load_file_with_mtime(path)
        if data2 is None or ts1 != ts2:
            continue

        ifname    = data2["interface"]
        df_status = data2["df_status"]

        if df_status in ("df", "non-df"):
            status[ifname] = df_status

    return status

def get_nft_object(nft_cmd):
    rc, output = rc_cmd(f"sudo nft list {nft_cmd}")
    if rc != 0:
        return None
    return output

def get_vteps(es_data, vteps):
    if es_data:
        vtep_dict = dict_search(f'vteps', es_data)
        if vtep_dict:
            for vtep in vtep_dict:
                vteps.add(dict_search('vtep', vtep))
            
        return set(vteps)
    return set()

def get_es_data():
    rc, es_data = rc_cmd("sudo vtysh -c 'show evpn es detail json'")
    if rc != 0:
        print(f"Error getting ES data: {es_data}")
        return {}

    es_data = json.loads(es_data)
    es_data = index_dicts_by_key('accessPort', es_data)
    if es_data:
        for iface in es_data.keys():
            if iface == 'unknown':
                continue

            if 'df' in dict_search(f'{iface}.flags', es_data):
                es_data[iface]['df_status'] = 'df'
            elif 'nonDF' in dict_search(f'{iface}.flags', es_data):
                es_data[iface]['df_status'] = 'non-df'    
            else:
                es_data[iface]['df_status'] = 'unknown'

    return es_data

def get_underlay_iface():
    bgp_peers = json.loads(cmd("sudo vtysh -c 'show bgp l2vpn evpn summary established json'"))
    bgp_peers = dict_search('peers', bgp_peers).keys()
    underlay_iface = set()
    for peer in bgp_peers:
        peer_route = json.loads(cmd(f"sudo vtysh -c 'show ip route {peer} json'"))
        peer_route = next(iter(peer_route.values()))[0]
        for interface in dict_search('nexthops', peer_route):
            underlay_iface.add(dict_search('interfaceName', interface))
    return list(underlay_iface)

def update_sph_filters(es_dict):
    def get_configured_status(df_set, non_df_set, interface, configured_state_dict):
        if df_set and interface in df_set:
            configured_state_dict[interface] = 'df'
        elif non_df_set and interface in non_df_set:
            configured_state_dict[interface] = 'non-df'
        else:
            configured_state_dict[interface] = 'unknown'

    tmp = get_es_data()
    if tmp != es_dict:
        es_dict = tmp

    netdev_table_exists = bool(get_nft_object("table netdev evpn_sph"))
    bridge_table_exists = bool(get_nft_object("table bridge evpn_sph"))
    df_set = get_nft_object("set bridge evpn_sph df_bonds")
    non_df_set = get_nft_object("set bridge evpn_sph non_df_bonds")

    configured_state_dict = {
        'df_interfaces': [],
        'non_df_interfaces': []
    }

    vteps = set()
    interfaces = []
    update_required = False
    if not all((netdev_table_exists, bridge_table_exists)):
        update_required = True

    for iface in es_dict.keys():
        reported_df_state = es_dict[iface]['df_status']
        get_configured_status(df_set, non_df_set, iface, configured_state_dict)
        
        if configured_state_dict[iface] != reported_df_state:
            update_required = True        
        if reported_df_state == 'df':
            configured_state_dict['df_interfaces'].append(iface)
        elif reported_df_state == 'non-df':
            configured_state_dict['non_df_interfaces'].append(iface)

        vteps = get_vteps(es_dict[iface], vteps)

        interfaces.append(iface)

    print(configured_state_dict)

    if not update_required:
        return

    underlay_iface = get_underlay_iface()

    config_dict = {}        
    config_dict['vteps'] = vteps    
    config_dict['netdev_table_exists'] = netdev_table_exists
    config_dict['bridge_table_exists'] = bridge_table_exists
    config_dict['df_interfaces'] = configured_state_dict['df_interfaces']
    config_dict['non_df_interfaces'] = configured_state_dict['non_df_interfaces']
    config_dict['interfaces'] = interfaces
    config_dict['underlay_iface'] = underlay_iface

    render(nftables_conf, 'frr/evpn.mh.sph.j2', config_dict)     
    rc, _ = rc_cmd(f'sudo nft -c --file {nftables_conf}')
    if rc != 0:
        print(f"nftables configuration validation failed: {rc}")
        return
    
    rc, _ = rc_cmd(f'sudo nft --file {nftables_conf}')
    if rc != 0:
        print(f"Failed to apply nftables configuration: {rc}")
        return

    print('SPH filters have been updated!')

def main():
    try:
        print("Waiting for FRR and Nftables to be ready...")
        frr_ready = wait_for(is_process_up, 'sudo vtysh -c "show evpn es detail json"', interval=0.5, timeout=10)
        nft_ready = wait_for(is_process_up, 'sudo nft list tables', interval=0.5, timeout=10)
        print("FRR ready!") if frr_ready else print("FRR not ready!")
        print("Nftables ready!") if nft_ready else print("Nftables not ready!")
        if not all((frr_ready, nft_ready)):
            print("FRR and/or Nftables not ready! Restarting Daemon...")
            sys.exit(1)
        
        es_dict = get_es_data()
        refresh_count = 0
        first_run = True
        update_required = False
        while not stop_event.is_set():
            if update_required or not es_dict: # If the system was just updated, or there is no ES data, get the latest data
                es_dict = get_es_data()

            if not es_dict: # If there is no ES data, wait and try again
                time.sleep(0.5)
                continue

            bond_interfaces = es_dict.keys()

            if first_run: # If this is the first run, update the SPH filters
                update_sph_filters(es_dict)
                first_run = False
                time.sleep(0.5)
                continue

            df_dict = get_df_status()
            if not df_dict: # If there is no DF status data, wait and try again
                time.sleep(0.5)
                continue

            update_required = False
            for interface in bond_interfaces:
                if interface not in df_dict:
                    continue
                if es_dict[interface]['df_status'] == df_dict[interface]: # If the DF status is the same as the reported status, then no update is needed
                    time.sleep(0.5)
                    continue
                else: # If the DF status is different, then an update is needed
                    update_required = True
                    break

            if refresh_count == int(refresh_timer / 1.5): # There are normally three 0.5 second waits per loop; total is 1.5 seconds; if the time has elapsed, then an update is needed
                refresh_count = 0
                update_required = True

            refresh_count += 1
                
            if update_required:
                update_sph_filters(es_dict)
                refresh_count = 0

            time.sleep(0.5)

    except Exception as e:
        print("ERROR:", e)
        raise

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
