#!/usr/bin/env python3
import os
import sys
import time
import syslog
import tempfile
import json

from vyos.utils.dict import dict_search
from vyos.utils.dict import dict_search_args
from vyos.utils.process import cmd
from vyos.utils.process import rc_cmd
from vyos.utils.process import run
from vyos.template import render

nftables_conf = '/run/nftables_evpn_sph.conf'
underlay_iface = ['eth1', 'eth3']
evpn_dir = "/run/frr/evpn-mh"

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

def nft_table_exists(table_name):
    rc, _ = rc_cmd(f"sudo nft list table {table_name}")
    if rc == 0:
        return True
    else:
        return False

def is_flooding_enabled(iface):
    bond_dict = json.loads(cmd(f"sudo bridge -d -j link show dev {iface}"))
    bond_dict = index_dicts_by_key('ifname', bond_dict)
    if bond_dict:
        vals = (bond_dict[iface]["flood"], bond_dict[iface]["mcast_flood"], bond_dict[iface]["bcast_flood"])

        all_true  = all(vals)
        all_false = not any(vals)

        if all_true:
            return True
        elif all_false:
            return False

def get_vteps(es_data, vteps):
    if es_data:
        vtep_dict = dict_search(f'vteps', es_data)
        if vtep_dict:
            for vtep in vtep_dict:
                vteps.add(dict_search('vtep', vtep))
            
        return vteps
    return set()

def flooding_state(iface, state):
    run(f"sudo bridge link set dev {iface} flood {state}")
    run(f"sudo bridge link set dev {iface} mcast_flood {state}")
    run(f"sudo bridge link set dev {iface} bcast_flood {state}")

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

def update_sph_filters(es_dict):
    def get_configured_state(vals):
        if all(vals):
            return 'df'
        elif not any(vals):
            return 'non-df'
        else:
            return 'unknown'
    config_dict = {}
    flood_dict = {}

    tmp = get_es_data()
    if tmp != es_dict:
        es_dict = tmp

    netdev_table_exists = nft_table_exists("netdev evpn_sph")
    bridge_table_exists = nft_table_exists("bridge evpn_sph")

    vteps = set()
    update_required = False
    is_df = set()
    update_required = False

    for iface in es_dict.keys():
        reported_df_state = es_dict[iface]['df_status']
        is_flooding_enabled_state = is_flooding_enabled(iface)

        vals = (netdev_table_exists, bridge_table_exists, is_flooding_enabled_state)
        configured_state = get_configured_state(vals)

        if reported_df_state == configured_state:
            continue
        update_required = True
        vteps = get_vteps(es_dict[iface], vteps)
        is_df.add(reported_df_state)

        if reported_df_state == 'df':
            flood_dict[iface] = 'on'
        else:
            flood_dict[iface] = 'off'


    if not update_required:
        return
    if 'df' in is_df:
        config_dict['vteps'] = ', '.join(vteps)    
    config_dict['netdev_table_exists'] = netdev_table_exists
    config_dict['bridge_table_exists'] = bridge_table_exists

    for iface in flood_dict.keys():
        flooding_state(iface, flood_dict[iface])

    render(nftables_conf, 'frr/evpn.mh.sph.j2', config_dict)     
    rc, _ = rc_cmd('sudo nft -c --file /run/nftables_nat.conf')
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
        es_dict = get_es_data()
        refresh_count = 0
        first_run = True
        update_required = False
        while True:
            if update_required or not es_dict:
                es_dict = get_es_data()

            if not es_dict:
                time.sleep(0.5)
                continue

            bond_interfaces = es_dict.keys()

            if first_run:
                update_sph_filters(es_dict)
                first_run = False
                time.sleep(0.5)
                continue

            df_dict = get_df_status()
            if not df_dict:
                time.sleep(0.5)
                continue

            update_required = False
            for interface in bond_interfaces:
                if interface not in df_dict:
                    continue
                if es_dict[interface]['df_status'] == df_dict[interface]:
                    time.sleep(0.5)
                    refresh_count += 1
                    continue
                else:
                    update_required = True
                    break

            if refresh_count == 10:
                refresh_count = 0
                update_required = True
                
            if update_required:
                update_sph_filters(es_dict)
                refresh_count = 0
            else:
                time.sleep(0.5)
                continue

            time.sleep(0.5)

    except Exception as e:
        print("ERROR:", e)
        raise

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
