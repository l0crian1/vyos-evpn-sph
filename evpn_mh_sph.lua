-- /etc/frr/scripts/evpn_mh_sph.lua
local bit_ok, bit = pcall(require, "bit")

local function is_non_df(flags)
        return (bit_ok and bit.band(flags or 0, 1) ~= 0) or (((flags or 0) % 2) == 1)
end

local function shq(s)  -- shell-quote
        s = tostring(s or "")
        return "'" .. s:gsub("'", "'\\''") .. "'"
end

function on_rib_process_dplane_results(ctx)
        if ctx and ctx.br_port then
                local ifname = ctx.zd_ifname or ""
                local non_df = is_non_df(ctx.br_port.flags or 0) and "1" or "0"

                -- Call python helper with args: <iface> <non_df>
                local py  = "/home/vyos/test.py"
                local cmd = string.format("/usr/bin/env python3 %s %s %s >/dev/null 2>&1 &",
                        shq(py), shq(ifname), shq(non_df))
                os.execute(cmd)
        end
        return {}  -- required
end
