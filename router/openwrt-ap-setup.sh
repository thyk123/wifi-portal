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
SSID_STAFF=${SSID_STAFF:-Office@GadingCounseling}
SSID_GUEST=${SSID_GUEST:-Guest@GadingCounseling}
# Permanent rescue address kept on the LAN ports. Even if the trunk/VLANs are
# wrong, plugging a laptop into lan2..lan4 always reaches the AP here.
RESCUE_IP=${RESCUE_IP:-192.168.1.1}
# Seconds the watchdog waits for a human/machine to confirm reachability.
WATCHDOG=${WATCHDOG:-300}
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
# Handle opkg (<=24.10), apk (>=25.x) and vendor builds with neither.
if command -v opkg >/dev/null 2>&1; then
    if ! opkg list-installed 2>/dev/null | grep -Eq '^wpad-(mbedtls|openssl) '; then
        opkg update || true
        for p in wpad-basic-mbedtls wpad-basic-openssl wpad-mini; do
            opkg remove --force-depends "$p" 2>/dev/null || true
        done
        opkg install wpad-mbedtls || log "WARN: could not install wpad-mbedtls"
    else
        echo "already installed"
    fi
elif command -v apk >/dev/null 2>&1; then
    if ! apk info -e wpad-mbedtls >/dev/null 2>&1; then
        apk update || true
        apk del wpad-basic-mbedtls wpad-basic-openssl wpad-mini 2>/dev/null || true
        apk add wpad-mbedtls || log "WARN: could not install wpad-mbedtls"
    else
        echo "already installed"
    fi
else
    log "WARN: no opkg/apk on this firmware - skipping package step"
    log "      OWE needs the full wpad; check with: iwinfo | head"
fi

# ---------------------------------------------------------------------------
log "network: bridge VLANs 1 (mgmt) / 100 (staff) / 200 (guest)"

# The trunk port must be a member of the LAN bridge before VLAN tagging means
# anything. On DSA targets (mediatek/filogic etc.) wan is a separate netdev
# that may still be owned by a 'wan' interface - detach it first.
BRDEV=$(uci show network 2>/dev/null | sed -n "s/^network\.\([^.]*\)\.name='br-lan'$/\1/p" | head -1)
if [ -z "$BRDEV" ]; then
    BRDEV=$(uci add network device)
    uci set "network.$BRDEV.name=br-lan"
    uci set "network.$BRDEV.type=bridge"
fi
uci add_list "network.$BRDEV.ports=$TRUNK" 2>/dev/null || true
# remember the pre-existing LAN ports (lan1..lan4) before touching VLANs
LANPORTS=$(uci -q get "network.$BRDEV.ports" | tr ' ' '\n' | grep -vx "$TRUNK" | grep -v '^$' | tr '\n' ' ')
log "network: lan ports = [$LANPORTS] trunk = $TRUNK"
# de-duplicate ports
PORTS=$(uci -q get "network.$BRDEV.ports" | tr ' ' '\n' | awk '!seen[$0]++' | tr '\n' ' ')
uci -q delete "network.$BRDEV.ports"
for p in $PORTS; do uci add_list "network.$BRDEV.ports=$p"; done

# remove the WAN interface that owns the trunk port (dumb-AP mode)
for w in wan wan6; do
    if uci -q get "network.$w" >/dev/null 2>&1; then
        if _has_trunk=; uci -q get "network.$w.device" | grep -qx "$TRUNK"; then _has_trunk=1; fi
        if [ -n "${_has_trunk:-}" ]; then
            log "network: removing interface '$w' (it owned $TRUNK)"
            uci delete "network.$w"
        fi
    fi
done

# A zone pointing at a deleted network makes fw4 refuse to load, which would
# leave the OLD ruleset active - strip the removed interfaces from the wan zone.
if ! uci -q get network.wan >/dev/null 2>&1; then
    ZW=$(uci show firewall 2>/dev/null | sed -n "s/^firewall\.\([^.]*\)\.name='wan'$/\1/p" | head -1)
    if [ -n "$ZW" ]; then
        uci -q delete "firewall.$ZW.network" || true
        uci -q delete "firewall.$ZW.masq" || true
        uci -q delete "firewall.$ZW.mtu_fix" || true
        log "firewall: cleaned wan zone ($ZW) after removing the wan interface"
    fi
fi

# rebuild the VLAN table from scratch (idempotent)
while uci -q delete network.@bridge-vlan[0]; do :; done

uci add network bridge-vlan >/dev/null
uci set network.@bridge-vlan[-1].device='br-lan'
uci set network.@bridge-vlan[-1].vlan='1'
# VLAN 1 = untagged everywhere: the trunk AND every local LAN port, so the
# rescue address stays reachable on lan1..lan4.
for p in $LANPORTS; do
    uci add_list network.@bridge-vlan[-1].ports="$p:u*"
done
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

# --- rescue address: always present on the LAN ports -------------------------
uci -q delete network.rescue
uci set network.rescue=interface
uci set network.rescue.device='br-lan'
uci set network.rescue.proto='static'
uci set network.rescue.ipaddr="$RESCUE_IP"
uci set network.rescue.netmask='255.255.255.0'
uci -q delete network.rescue.ip6assign || true
log "network: rescue address $RESCUE_IP on the LAN ports (always reachable)"

# Wi-Fi VLANs carry no IP on the AP — that alone blocks LuCI/SSH from them
uci set network.staff=interface
uci set network.staff.device='br-lan.100'
uci set network.staff.proto='none'

uci set network.guest=interface
uci set network.guest.device='br-lan.200'
uci set network.guest.proto='none'

uci commit network

# ---------------------------------------------------------------------------
log "wireless: two OWE SSIDs on every radio (dual band), client isolation"

# RADIOS=auto -> every wifi-device present (radio0 = 2.4G, radio1 = 5G, ...)
if [ "${RADIOS:-auto}" = "auto" ]; then
    RADIOS=$(uci show wireless 2>/dev/null | sed -n "s/^wireless\.\(radio[0-9]*\)=wifi-device$/\1/p" | sort || true)
    [ -n "$RADIOS" ] || RADIOS="$RADIO"
fi
log "wireless: radios = $RADIOS"

if [ "$DISABLE_OTHER" = "1" ]; then
    for s in $(uci show wireless | sed -n "s/^wireless\.\([^.=]*\)=wifi-iface$/\1/p"); do
        case "$s" in
            staff_*|guest_*|staff|guest) continue ;;
        esac
        uci set "wireless.$s.disabled='1'"
    done
fi

# drop the old single-radio sections from previous runs
uci -q delete wireless.staff
uci -q delete wireless.guest

# Per-SSID radio lists. Default: every radio, i.e. the SSID is DUAL BAND.
# Override e.g. GUEST_RADIOS=radio0 to keep guests on 2.4 GHz only.
STAFF_RADIOS=${STAFF_RADIOS:-$RADIOS}
GUEST_RADIOS=${GUEST_RADIOS:-$RADIOS}
log "wireless: $SSID_STAFF on [$STAFF_RADIOS] (dual band)"
log "wireless: $SSID_GUEST on [$GUEST_RADIOS]"

for r in $STAFF_RADIOS; do
    uci set "wireless.staff_$r=wifi-iface"
    uci set "wireless.staff_$r.device=$r"
    uci set "wireless.staff_$r.mode='ap'"
    uci set "wireless.staff_$r.ssid=$SSID_STAFF"
    uci set "wireless.staff_$r.network='staff'"
    uci set "wireless.staff_$r.encryption='${ENC_STAFF:-owe}'"
    uci set "wireless.staff_$r.ieee80211w='2'"
    uci set "wireless.staff_$r.isolate='1'"
    uci set "wireless.staff_$r.disabled='0'"
done

for r in $GUEST_RADIOS; do
    uci set "wireless.guest_$r=wifi-iface"
    uci set "wireless.guest_$r.device=$r"
    uci set "wireless.guest_$r.mode='ap'"
    uci set "wireless.guest_$r.ssid=$SSID_GUEST"
    uci set "wireless.guest_$r.network='guest'"
    uci set "wireless.guest_$r.encryption='${ENC_GUEST:-owe}'"
    uci set "wireless.guest_$r.ieee80211w='2'"
    uci set "wireless.guest_$r.isolate='1'"
    uci set "wireless.guest_$r.disabled='0'"
done

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

# --- management access: SSH + LuCI only from the management subnets --------
# Reachable over the trunk (untagged -> VLAN 100) but never from Wi-Fi clients.
MGMT_NET=${MGMT_NET:-192.168.88.0/24}
ADMIN_NETS=${ADMIN_NETS:-"192.168.88.0/24 10.10.10.0/24 192.168.1.0/24"}
for keep in ssh_any web_any deny_clients; do uci -q delete "firewall.$keep"; done

uci set firewall.deny_clients=rule
uci set firewall.deny_clients.name='Deny-mgmt-from-wifi-clients'
uci set firewall.deny_clients.src='*'
uci set firewall.deny_clients.src_ip='10.77.0.0/16'
uci set firewall.deny_clients.proto='tcp'
uci set firewall.deny_clients.dest_port='22 80 443'
uci set firewall.deny_clients.target='DROP'

n=0
for net in $ADMIN_NETS; do
    n=$((n + 1))
    uci set "firewall.admin_ssh_$n=rule"
    uci set "firewall.admin_ssh_$n.name=Allow-SSH-$net"
    uci set "firewall.admin_ssh_$n.src=*"
    uci set "firewall.admin_ssh_$n.src_ip=$net"
    uci set "firewall.admin_ssh_$n.proto=tcp"
    uci set "firewall.admin_ssh_$n.dest_port=22"
    uci set "firewall.admin_ssh_$n.target=ACCEPT"

    uci set "firewall.admin_web_$n=rule"
    uci set "firewall.admin_web_$n.name=Allow-LuCI-$net"
    uci set "firewall.admin_web_$n.src=*"
    uci set "firewall.admin_web_$n.src_ip=$net"
    uci set "firewall.admin_web_$n.proto=tcp"
    uci set "firewall.admin_web_$n.dest_port=80 443"
    uci set "firewall.admin_web_$n.target=ACCEPT"
done

# dropbear: listen on every interface + allow password & key auth
if ! uci -q get dropbear.@dropbear[0] >/dev/null 2>&1; then
    uci add dropbear dropbear >/dev/null
fi
uci set dropbear.@dropbear[0].Interface=''
uci set dropbear.@dropbear[0].PasswordAuth='on'
uci set dropbear.@dropbear[0].RootPasswordAuth='on'
uci set dropbear.@dropbear[0].Port='22'

uci commit firewall
uci commit dropbear

log "mgmt: ssh+luci open on all zones, dropbear on all interfaces"

# ---------------------------------------------------------------------------
log "applying"

# --- watchdog: revert everything unless somebody confirms reachability -------
# A background job survives the network reload. If no one creates
# /tmp/ap-keep within $WATCHDOG seconds, the previous config is restored and
# the box reboots - so a broken AP always comes back on its old address.
rm -f /tmp/ap-keep
(
    sleep "$WATCHDOG"
    if [ ! -f /tmp/ap-keep ]; then
        for f in network wireless firewall dhcp; do
            [ -f "/etc/config/$f.bak.$STAMP" ] && cp "/etc/config/$f.bak.$STAMP" "/etc/config/$f"
        done
        logger -t ap-setup "watchdog: no confirmation, reverting and rebooting"
        reboot
    fi
) >/dev/null 2>&1 &
log "watchdog: revert+reboot in ${WATCHDOG}s unless /tmp/ap-keep exists"

/etc/init.d/network reload || true
sleep 3
wifi reload || wifi up || true
/etc/init.d/firewall reload || true
/etc/init.d/dropbear restart || true

echo
log "CONFIRM within ${WATCHDOG}s:  ssh root@<new-ip> 'touch /tmp/ap-keep'"
log "otherwise this AP reverts to its previous config and reboots"
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
