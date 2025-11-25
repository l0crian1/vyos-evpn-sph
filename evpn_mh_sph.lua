-- /etc/frr/scripts/evpn_mh_sph.lua
local bit_ok, bit = pcall(require, "bit")

local function is_non_df(flags)
    return (bit_ok and bit.band(flags or 0, 1) ~= 0) or (((flags or 0) % 2) == 1)
end

-- Simple JSON string escaper
local function json_escape_str(s)
    s = tostring(s or "")
    s = s:gsub("\\", "\\\\"):gsub('"', '\\"')
    return '"' .. s .. '"'
end

-- Safe filesystem-name generator
local function safe_filename(s)
    s = tostring(s or "")
    return s:gsub("[^%w%-_.]", "_")  -- replace unsafe chars
end

local BASE_DIR = "/run/frr/evpn-mh"

local function write_df_status(ifname, non_df)
    local filename = BASE_DIR .. "/evpn_df_status_" .. safe_filename(ifname) .. ".json"

    local f = io.open(filename, "w")
    if not f then
        -- optional debug logs go here
        return
    end

    local df_status = non_df and "non-df" or "df"

    local json = string.format(
        '{"interface":%s,"df_status":%s}\n',
        json_escape_str(ifname),
        json_escape_str(df_status)
    )

    f:write(json)
    f:close()
end

function on_rib_process_dplane_results(ctx)
    if ctx and ctx.br_port then
        local ifname = ctx.zd_ifname or ""
        local non_df = is_non_df(ctx.br_port.flags or 0)
        write_df_status(ifname, non_df)
    end
    return {}
end
