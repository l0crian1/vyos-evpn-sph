1. Create FRR scripts directory
   ```
   mkdir -p /etc/frr/scripts
   ```
2. Place `evpn_mh_sph.lua` script in `/etc/frr/scripts/` directory
3. Modify `underlay_iface` towards the top of `test.py` with your underlay interfaces.
4. Place modified `test.py` in `/home/vyos/`
5. Add this line to `/usr/lib/python3/dist-packages/vyos/frrender.py` directly after `output = '!\n'` around line 676.
   ```
   output += 'zebra on-rib-process script evpn_mh_sph\n'
   ```
6. Place `evpn.mh.sph.j2` into `/usr/share/vyos/templates/frr/`
7. Place `vyos-evpn-sph.service` in `/run/systemd/system/`
8. Run these commands:
   ```
   sudo systemctl daemon-reload
   sudo systemctl enable vyos-evpn-sph.service
   sudo systemctl start vyos-evpn-sph.service
   ```
9. Reboot VyOS
