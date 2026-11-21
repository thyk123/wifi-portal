 #!/bin/sh
# ============================================================================
# Gading AP setup — idempotent one-shot config for OpenWrt 23.05 / 24.10
# ----------------------------------------------------------------------------
# Creates: trunk VLANs 1/100/200, mgmt on VLAN 1, two OWE SSIDs
#          (Office-Staff -> VLAN 100, Office-Guest -> VLAN 200), client
#          isolation on both, firewall zones that drop all inputs from the
#          Wi-Fi VLANs so LuCI/SSH are unreachable from them.
#
# Run it ONCE per AP. The only thing that normally differs is TRUNK
# (some APs use lan1, others lan2/wan).
#
#   ssh root@10.10.10.20 \
#     'TRUNK=lan1 MGMT=static MGMT_IP=192.168.88.21 sh -s' < openwrt-ap-setup.sh
#
#   # several APs, same config:
#   for ip in 10.10.10.20 10.10.10.21 10.10.10.22; do
#     ssh root@"$ip" 'TRUNK=lan1 MGMT=static MGMT_IP=192.168.88.21 sh -s' < openwrt-ap-setup.sh
#   done
#
# SAFETY: run over a WIRED connection. It rewrites /etc/config/network, so a
# Wi-Fi session would drop mid-way. A timestamped backup is taken first and
# printed at the end for rollback.
# ============================================================================
set -eu

TRUNK=${TRUNK:-lan1}                 # LAN port cabled to the MikroTik trunk
RADIO=${RADIO:-radio0}               # radio to broadcast on
MGMT=${MGMT:-static}                # dhcp | static  (mgmt rides untagged -> VLAN 100)
MGMT_IP=${MGMT_IP:-192.168.88.21}    # only for MGMT=static (one per AP)
MGMT_GW=${MGMT_GW:-192.168.88.1}
SSID_STAFF=${SSID_STAFF:-Office-Staff}
SSID_GUEST=${SSID_GUEST:-Office-Guest}
DISABLE_OTHER=${DISABLE_OTHER:-1}    # 1 = turn off any other SSID on that radio

[ -f /etc/openwrt_release ] || { echo "!! not OpenWrt"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "!! run as root"; exit 1; }

log() { printf '\n== %s\n' "$*"; }

STAMP=$(date +%Y%m%d-%H%M%S)
for f in network wireless firewall dhcp; do
    cp "/etc/config/$f" "/etc/config/$f.bak.$STAMP"
done
log "backup: /etc/config/{network,wireless,firewall,dhcp}.bak.$STAMP"

# ---------------------------------------------------------------------------
log "packages: full wpad (OWE + 802.11w)"
if ! opkg list-installed 2>/dev/null | grep -Eq '^wpad-(mbedtls|openssl) '; then
    opkg update
    for p in wpad-basic-mbedtls wpad-basic-openssl wpad-mini; do
        opkg remove --force-depends "$p" 2>/dev/null || true
    done
    opkg install wpad-mbedtls
else
    echo "already installed"
fi

# ---------------------------------------------------------------------------
log "network: bridge VLANs 1 (mgmt) / 100 (staff) / 200 (guest)"

# rebuild the VLAN table from scratch (idempotent)
while uci -q delete network.@bridge-vlan[0]; do :; done

uci add network bridge-vlan >/dev/null
uci set network.@bridge-vlan[-1].device='br-lan'
uci set network.@bridge-vlan[-1].vlan='1'
uci add_list network.@bridge-vlan[-1].ports="$TRUNK:u*"

uci add network bridge-vlan >/dev/null
uci set network.@bridge-vlan[-1].device='br-lan'
uci set network.@bridge-vlan[-1].vlan='100'
uci add_list network.@bridge-vlan[-1].ports="$TRUNK:t"

uci add network bridge-vlan >/dev/null
uci set network.@bridge-vlan[-1].device='br-lan'
uci set network.@bridge-vlan[-1].vlan='200'
uci add_list network.@bridge-vlan[-1].ports="$TRUNK:t"

# management rides the UNTAGGED bridge port: the switch assigns PVID 100 to it,
# so this lands in the same VLAN as the switch mgmt (192.168.88.0/24).
uci set network.lan.device='br-lan'
uci set network.lan.ifname='br-lan' 2>/dev/null || true   # pre-21.02 fallback
if [ "$MGMT" = "static" ]; then
    uci set network.lan.proto='static'
    uci set network.lan.ipaddr="$MGMT_IP"
    uci set network.lan.netmask='255.255.255.0'
    uci set network.lan.gateway="$MGMT_GW"
    uci -q delete network.lan.ip6assign || true
else
    uci set network.lan.proto='dhcp'
    uci -q delete network.lan.ipaddr || true
    uci -q delete network.lan.netmask || true
fi

# Wi-Fi VLANs carry no IP on the AP — that alone blocks LuCI/SSH from them
uci set network.staff=interface
uci set network.staff.device='br-lan.100'
uci set network.staff.proto='none'

uci set network.guest=interface
uci set network.guest.device='br-lan.200'
uci set network.guest.proto='none'

uci commit network

# ---------------------------------------------------------------------------
log "wireless: two OWE SSIDs with client isolation"

if [ "$DISABLE_OTHER" = "1" ]; then
    for s in $(uci show wireless | sed -n "s/^wireless\.\([^.=]*\)=wifi-iface$/\1/p"); do
        [ "$s" = "staff" ] && continue
        [ "$s" = "guest" ] && continue
        uci set "wireless.$s.disabled='1'"
    done
fi

uci set wireless.staff=wifi-iface
uci set wireless.staff.device="$RADIO"
uci set wireless.staff.mode='ap'
uci set wireless.staff.ssid="$SSID_STAFF"
uci set wireless.staff.network='staff'
uci set wireless.staff.encryption='owe'
uci set wireless.staff.ieee80211w='2'
uci set wireless.staff.isolate='1'
uci set wireless.staff.disabled='0'

uci set wireless.guest=wifi-iface
uci set wireless.guest.device="$RADIO"
uci set wireless.guest.mode='ap'
uci set wireless.guest.ssid="$SSID_GUEST"
uci set wireless.guest.network='guest'
uci set wireless.guest.encryption='owe'
uci set wireless.guest.ieee80211w='2'
uci set wireless.guest.isolate='1'
uci set wireless.guest.disabled='0'

uci commit wireless

# ---------------------------------------------------------------------------
log "firewall: drop every input from the Wi-Fi VLANs"

uci -q delete firewall.staff
uci set firewall.staff=zone
uci set firewall.staff.name='staff'
uci set firewall.staff.input='DROP'
uci set firewall.staff.output='DROP'
uci set firewall.staff.forward='DROP'
uci add_list firewall.staff.network='staff'

uci -q delete firewall.guest
uci set firewall.guest=zone
uci set firewall.guest.name='guest'
uci set firewall.guest.input='DROP'
uci set firewall.guest.output='DROP'
uci set firewall.guest.forward='DROP'
uci add_list firewall.guest.network='guest'

uci commit firewall

# ---------------------------------------------------------------------------
log "applying"
/etc/init.d/network reload || true
sleep 3
wifi reload || wifi up || true
/etc/init.d/firewall reload || true
sleep 2

# ---------------------------------------------------------------------------
log "verify"
echo "-- bridge vlans"
uci show network | grep bridge-vlan | sed 's/^network\.//'
echo "-- mgmt address"
ip -4 addr show br-lan.1 2>/dev/null | grep inet || echo "(no address yet — DHCP?)"
echo "-- radios"
iw dev 2>/dev/null | grep -E 'Interface|ssid|channel' || true
echo "-- OWE / hostapd"
logread 2>/dev/null | tail -40 | grep -iE 'owe|wpa3|hostapd' || echo "(no owe lines yet)"
echo
echo "Rollback: cp /etc/config/network.bak.$STAMP /etc/config/network && reboot"
echo "          (same for wireless / firewall if needed)"
