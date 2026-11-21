# Gading Wi-Fi — OpenWrt AP Runbook (execute top to bottom)

> Target: TOTOLINK X6000R-class APs running **OpenWrt 25.12.x (apk, DSA / mediatek-filogic)**.
> Executor: an AI or engineer. **Every step has a VERIFY line. Do not continue past a failed VERIFY.**
> Written 2026-11-21 after a lockout caused by changing `network` without a rescue address.

---

## 0. Non-negotiable rules

1. **NEVER reload `network` on an AP until you have confirmed it still has a reachable address.**
   The script in §6 has a watchdog that auto-reverts. Use it. Do not hand-edit `network` without it.
2. **The trunk port must be a member of `br-lan`** before VLAN tagging means anything. On DSA
   targets (`mediatek/filogic`) the `wan` netdev is NOT in `br-lan` by default.
3. **Management lives in VLAN 100**, untagged on the AP side (`192.168.88.0/24`).
   Wi-Fi clients live in VLAN 100 (`10.77.10.0/24`) and VLAN 200 (`10.77.20.0/24`) — tagged.
4. **Keep `192.168.1.1` as a permanent rescue address** on the AP's LAN ports.
5. Access to the AP is **only** for `192.168.88.0/24`, `10.10.10.0/24`, `192.168.1.0/24`.
   Wi-Fi clients (`10.77.0.0/16`) must be **blocked**.

### Topology

```
phone ──Wi-Fi──┐
               ├─ AP (dumb AP, no routing, no NAT, no WAN)
AP lan1 ───────┤
               └── Ruijie port 3 (trunk, native 100, tagged 100+200)
                        │
                        └── Ruijie port 7 ── MikroTik ether4 ┐
                            Ruijie port 8 ── MikroTik ether5 ┴─ br-lan (vlan-filtering)
                                                                          │
                                              vlan100-staff 10.77.10.1/24 = hs-staff hotspot
                                              vlan200-guest 10.77.20.1/24 = hs-guest hotspot
                                              mgmt          192.168.88.1/24 (VLAN 100, untagged)
```

| VLAN | Who | Subnet | On the AP |
|------|-----|--------|-----------|
| 1 | AP internal / untagged | – | becomes VLAN 100 via switch PVID |
| 100 | staff clients + all management | `10.77.10.0/24`, mgmt `192.168.88.0/24` | tagged 100 + untagged (mgmt) |
| 200 | guest clients | `10.77.20.0/24` | tagged 200 |

---

## 1. Pre-flight: what the AP will need from the network

The AP needs **internet once** to install `wpad-mbedtls` (OWE support).

### The one-cable answer

A factory OpenWrt AP has:

- **WAN port** → DHCP client → *this* is where internet comes from.
- **LAN ports (lan1–lan4)** → `192.168.1.1` bridge, management only, **no internet**.

So with a single cable:

1. Put the cable in the AP's **WAN** port → other end into **Ruijie port 3**.
   The AP's WAN receives `10.77.10.x` from `dhcp-staff` and gets internet through the MikroTik.
2. Manage the AP **over its factory Wi-Fi**: connect a phone/laptop to the open SSID
   `OpenWrt` (or `LEDE`) that the AP broadcasts, then browse to **`http://192.168.1.1`**.
   That SSID is bridged to `br-lan`, so `192.168.1.1` works while the cable provides internet.

**VERIFY (on the AP console, i.e. the LuCI "Terminal" or SSH from that Wi-Fi):**

```sh
ip route | head -3          # must show: default via 10.77.10.1 dev wan
ping -c2 1.1.1.1            # must reply
```

If there is no default route, stop: the cable is in a LAN port, or switch port 3 is not
native VLAN 100. Fix the cabling before continuing.

> If you prefer cable-based management instead: cable into `lan1` and accept that the AP has
> **no internet** — then `apk add wpad-mbedtls` fails and the script skips OWE (§6 note). OWE will
> not work until the AP has been online once.

---

## 2. Ruijie RG-ES208GC — VLAN settings

UI: `http://192.168.88.2` (management VLAN 100). Verify after each save by reloading the page.

| Port | Connects to | Mode | Native VLAN | Permit VLAN |
|------|-------------|------|-------------|-------------|
| **7** | MikroTik `ether4` | Trunk | **1** | `1, 100, 200` |
| **8** | MikroTik `ether5` | Trunk | **1** | `1, 100, 200` |
| **3** | AP #1 | Trunk | **100** | `100, 200` |
| **4** | AP #2 | Trunk | **100** | `100, 200` |
| 1, 2, 5 | spares | Access | **100** | — |
| 6 | unused | Access | **100** | — |

- Remove VLAN `900` and `10` from every port.
- VLAN 100 stays **tagged** on the uplinks 7/8 (the MikroTik side is a tagged trunk).
- VLAN 100 is **native/untagged** on the AP ports 3/4, which is what makes the AP's
  `192.168.1.1` / `192.168.88.21` reachable.

**VERIFY (from the MikroTik):**

```rsc
/interface bridge host print where vid=100
/interface bridge host print where vid=200
```

Both must list MACs learned on `ether4`. If VLAN 200 shows nothing, one uplink is not permitting it.

---

## 3. Reset the AP to factory state

1. Unplug power. Hold the **reset** button. Plug power back in while holding.
2. Keep holding until the LED blinks **fast** (~10 s), then release. The device is in failsafe mode.
3. SSH in (no password) and wipe the overlay:

```sh
firstboot -y && reboot
```

4. The AP returns to its factory config: `192.168.1.1`, open SSID `OpenWrt`.

**VERIFY:** connect to the `OpenWrt` SSID → `http://192.168.1.1` loads LuCI with no password.

---

## 4. Check OWE support (decides encryption mode)

On the AP console (LuCI → Terminal, or SSH from the `OpenWrt` Wi-Fi):

```sh
wpad -v 2>&1 | head -2
apk list --installed 2>/dev/null | grep -i wpad
iw phy | grep -wi OWE
```

| Result | Meaning | Action |
|--------|---------|--------|
| `apk list` shows `wpad-mbedtls`, `iw phy` shows `OWE` | full build, OWE works | use the defaults in §6 |
| `apk list` shows `wpad-basic-*` | OWE unsupported until upgraded | AP must be online (§1), then `apk update && apk add wpad-mbedtls` |
| no `wpad` at all | broken image | reflash OpenWrt, stop here |

If you need a fallback without OWE, add `ENC_STAFF=psk2 ENC_GUEST=psk2 WPA_KEY=<secret>` to §6
(open SSIDs still work; OWE only adds link-layer encryption).

---

## 5. Permanent access (do this BEFORE any config change)

From the AP console — install the operator's public key so SSH never needs a password again:

```sh
mkdir -p /etc/dropbear
cat >> /etc/dropbear/authorized_keys <<'EOF'
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC2heFMIqAu1fpwDPx94hiY1WQ/orGWhrh3wsOmkWIDzOCX/nf0/gBescHwEfUFDrCDqHohQuUiMowo7wCFoc1uxgmdRCNkjteUHIpStsnaUB4d5RGttNmRKbYWVV7ow6OEvbunDpoxM8rNaqG6+WTwuthrSGFoMx3FpEbFR0Qlf9qZMLSq+oiCCh/dOHGhQewSkkmdhwB2Yk4/zmOst1FlTgcz5TJX/m8IfaSwLcLE516lMgzKPnSmqShBCrmZJ3VZDVat3WxFe91CrAFX/Hy9HUV6dwWFYgq3+RnGHrfcciPIcact17ze/RyqRB992tWCbs3xqn3KY7Rc0X36SMFX copilot@devserver
EOF
chmod 600 /etc/dropbear/authorized_keys
/etc/init.d/dropbear restart
```

**VERIFY (from a management host on `10.10.10.0/24` or `192.168.1.0/24`):**

```sh
nc -z <ap-ip> 22 && echo "ssh reachable"
ssh -i ~/.ssh/wifi_router_ed25519 root@<ap-ip> 'echo KEY_OK'
```

---

## 6. Configure the AP (step by step — verify each block)

Set the variables once, then run the blocks in order. **Do not skip step 6.6.**

```sh
TRUNK=wan                 # the port whose cable goes to the switch (wan or lan1..lan4)
MGMT_IP=192.168.88.21     # .21 for AP1, .22 for AP2
RESCUE_IP=192.168.1.1
```

### 6.1 Back up (mandatory)

```sh
STAMP=$(date +%Y%m%d-%H%M%S)
for f in network wireless firewall dhcp dropbear; do
    [ -f "/etc/config/$f" ] && cp "/etc/config/$f" "/etc/config/$f.bak.$STAMP"
done
ls -l /etc/config/*.bak.$STAMP
```

**VERIFY:** five backup files exist (dropbear may be absent).

### 6.2 Attach the trunk port to the LAN bridge

```sh
BRDEV=$(uci show network | sed -n "s/^network\.\([^.]*\)\.name='br-lan'$/\1/p" | head -1)
[ -n "$BRDEV" ] || { BRDEV=$(uci add network device); uci set network.$BRDEV.name='br-lan'; uci set network.$BRDEV.type='bridge'; }
LANDED=0; for p in $(uci -q get network.$BRDEV.ports); do [ "$p" = "$TRUNK" ] || LANDED="$LANDED $p"; done
uci add_list network.$BRDEV.ports="$TRUNK"
uci -q delete network.$BRDEV.ports
for p in $LANDED $TRUNK; do uci add_list network.$BRDEV.ports="$p"; done
echo "lan ports:$LANDED trunk:$TRUNK"
```

**VERIFY:** `uci -q get network.$BRDEV.ports` lists every LAN port **and** `$TRUNK` exactly once.

### 6.3 Remove the WAN interface (dumb-AP mode)

```sh
uci -q delete network.wan
uci -q delete network.wan6
# a firewall zone pointing at a deleted network makes fw4 refuse to load
ZW=$(uci show firewall | sed -n "s/^firewall\.\([^.]*\)\.name='wan'$/\1/p" | head -1)
if [ -n "$ZW" ]; then uci -q delete firewall.$ZW.network; uci -q delete firewall.$ZW.masq; uci -q delete firewall.$ZW.mtu_fix; fi
```

**VERIFY:** `uci -q get network.wan` prints nothing.

### 6.4 VLAN table

```sh
while uci -q delete network.@bridge-vlan[0]; do :; done

# VLAN 1: untagged on the trunk AND every LAN port (rescue address lives here)
uci add network bridge-vlan; uci set network.@bridge-vlan[-1].device='br-lan'
uci set network.@bridge-vlan[-1].vlan='1'
for p in $LANDED $TRUNK; do uci add_list network.@bridge-vlan[-1].ports="$p:u*"; done

# VLAN 100 + 200: tagged on the trunk only
uci add network bridge-vlan; uci set network.@bridge-vlan[-1].device='br-lan'
uci set network.@bridge-vlan[-1].vlan='100'; uci add_list network.@bridge-vlan[-1].ports="$TRUNK:t"
uci add network bridge-vlan; uci set network.@bridge-vlan[-1].device='br-lan'
uci set network.@bridge-vlan[-1].vlan='200'; uci add_list network.@bridge-vlan[-1].ports="$TRUNK:t"

uci show network | grep bridge-vlan
```

**VERIFY:** VLAN 1 lists every LAN port + trunk as `u*`; 100 and 200 list only `$TRUNK:t`.

### 6.5 Addresses

```sh
# client VLANs: no IP on the AP, ever
uci set network.staff=interface; uci set network.staff.device='br-lan.100'; uci set network.staff.proto='none'
uci set network.guest=interface; uci set network.guest.device='br-lan.200'; uci set network.guest.proto='none'

# management (VLAN 100, untagged)
uci set network.lan.device='br-lan'
uci set network.lan.proto='static'
uci set network.lan.ipaddr="$MGMT_IP"
uci set network.lan.netmask='255.255.255.0'
uci set network.lan.gateway='192.168.88.1'
uci -q delete network.lan.ip6assign

# rescue (never removed)
uci -q delete network.rescue
uci set network.rescue=interface
uci set network.rescue.device='br-lan'
uci set network.rescue.proto='static'
uci set network.rescue.ipaddr="$RESCUE_IP"
uci set network.rescue.netmask='255.255.255.0'

uci commit network
```

**VERIFY (before reloading anything):**

```sh
uci show network | grep -E 'lan\.ipaddr|rescue\.ipaddr|staff\.proto|guest\.proto'
```

Must show `lan.ipaddr=192.168.88.21`, `rescue.ipaddr=192.168.1.1`, both `proto=none`.

### 6.6 Wireless (dual band, both SSIDs, OWE, client isolation)

```sh
RADIOS=$(uci show wireless | sed -n "s/^wireless\.\(radio[0-9]*\)=wifi-device$/\1/p" | sort)
[ -n "$RADIOS" ] || RADIOS="radio0"
echo "radios: $RADIOS"

# disable every pre-existing SSID
for s in $(uci show wireless | sed -n "s/^wireless\.\([^.=]*\)=wifi-iface$/\1/p"); do
    case "$s" in staff_*|guest_*) continue;; esac
    uci set wireless.$s.disabled='1'
done
uci -q delete wireless.staff; uci -q delete wireless.guest

for r in $RADIOS; do
    uci set wireless.staff_$r=wifi-iface; uci set wireless.staff_$r.device=$r
    uci set wireless.staff_$r.mode='ap';    uci set wireless.staff_$r.ssid='Office@GadingCounseling'
    uci set wireless.staff_$r.network='staff'; uci set wireless.staff_$r.encryption='owe'
    uci set wireless.staff_$r.ieee80211w='2';  uci set wireless.staff_$r.isolate='1'
    uci set wireless.staff_$r.disabled='0'

    uci set wireless.guest_$r=wifi-iface;   uci set wireless.guest_$r.device=$r
    uci set wireless.guest_$r.mode='ap';    uci set wireless.guest_$r.ssid='Guest@GadingCounseling'
    uci set wireless.guest_$r.network='guest'; uci set wireless.guest_$r.encryption='owe'
    uci set wireless.guest_$r.ieee80211w='2';  uci set wireless.guest_$r.isolate='1'
    uci set wireless.guest_$r.disabled='0'
done
uci commit wireless
```

**VERIFY:**

```sh
uci show wireless | grep -E 'staff_|guest_' | grep -E 'ssid|device|isolate'
```

Expect two `staff_*` and two `guest_*` blocks (one per radio), all `isolate='1'`.

### 6.7 Firewall + management ACL

```sh
uci -q delete firewall.staff
uci set firewall.staff=zone; uci set firewall.staff.name='staff'
uci set firewall.staff.input='DROP'; uci set firewall.staff.output='DROP'; uci set firewall.staff.forward='DROP'
uci add_list firewall.staff.network='staff'

uci -q delete firewall.guest
uci set firewall.guest=zone; uci set firewall.guest.name='guest'
uci set firewall.guest.input='DROP'; uci set firewall.guest.output='DROP'; uci set firewall.guest.forward='DROP'
uci add_list firewall.guest.network='guest'

# clients must never reach management
uci -q delete firewall.deny_clients
uci set firewall.deny_clients=rule; uci set firewall.deny_clients.name='Deny-mgmt-from-clients'
uci set firewall.deny_clients.src='*'; uci set firewall.deny_clients.src_ip='10.77.0.0/16'
uci set firewall.deny_clients.proto='tcp'; uci set firewall.deny_clients.dest_port='22 80 443'
uci set firewall.deny_clients.target='DROP'

# management subnets only
i=0
for net in 192.168.88.0/24 10.10.10.0/24 192.168.1.0/24; do
    i=$((i+1))
    uci set firewall.msh_$i=rule; uci set firewall.msh_$i.src='*'
    uci set firewall.msh_$i.src_ip=$net; uci set firewall.msh_$i.proto='tcp'
    uci set firewall.msh_$i.dest_port='22'; uci set firewall.msh_$i.target='ACCEPT'
    uci set firewall.mweb_$i=rule; uci set firewall.mweb_$i.src='*'
    uci set firewall.mweb_$i.src_ip=$net; uci set firewall.mweb_$i.proto='tcp'
    uci set firewall.mweb_$i.dest_port='80 443'; uci set firewall.mweb_$i.target='ACCEPT'
done

uci commit firewall
```

**VERIFY:** `uci show firewall | grep -c 'src_ip'` → `6`.

### 6.8 Apply — WITH the watchdog

This is the step that previously caused the lockout. The watchdog restores the backups and
reboots unless `/tmp/ap-keep` is created within 300 s.

```sh
rm -f /tmp/ap-keep
(
  sleep 300
  if [ ! -f /tmp/ap-keep ]; then
      for f in network wireless firewall dhcp dropbear; do
          [ -f "/etc/config/$f.bak.$STAMP" ] && cp "/etc/config/$f.bak.$STAMP" "/etc/config/$f"
      done
      logger -t ap-setup "watchdog: no confirmation, reverting"
      reboot
  fi
) >/dev/null 2>&1 &

/etc/init.d/network reload
sleep 3
wifi reload || wifi up
/etc/init.d/firewall reload
/etc/init.d/dropbear restart
```

**IMMEDIATELY, from a management host (within 5 minutes):**

```sh
ping -c2 192.168.1.1          # rescue address
ping -c2 192.168.88.21        # management address
ssh -i ~/.ssh/wifi_router_ed25519 root@192.168.88.21 'touch /tmp/ap-keep; echo KEEP_SET'
```

`KEEP_SET` disarms the watchdog. **If it does not print, do nothing** — the AP reverts itself and
reboots back to its previous working configuration after 5 minutes.

---

## 7. Verification checklist

Run from a management host (`10.10.10.199` or any `192.168.88.x` host).

| # | Test | Expected |
|---|------|----------|
| 1 | `ping -c2 192.168.88.21` | replies |
| 2 | `ping -c2 192.168.1.1` | replies (rescue path) |
| 3 | `ssh root@192.168.88.21 'iw dev \| grep ssid'` | `Office@GadingCounseling` **and** `Guest@GadingCounseling` twice each (2 radios) |
| 4 | `ssh root@192.168.88.21 'uci get wireless.staff_radio0.encryption'` | `owe` |
| 5 | `ssh root@192.168.88.21 'uci get wireless.staff_radio1.isolate'` | `1` |
| 6 | `curl -s http://192.168.88.21/` | LuCI responds |
| 7 | From a **staff** client: `nc -zv 192.168.88.21 22` | **fails** (blocked) |
| 8 | From a **guest** client: `nc -zv 192.168.88.21 80` | **fails** (blocked) |
| 9 | Client joins `Office@GadingCounseling` → `ipconfig` / `ip a` | `10.77.10.x`, gateway `10.77.10.1` |
| 10 | Client joins `Guest@GadingCounseling` → same | `10.77.20.x`, gateway `10.77.20.1` |
| 11 | Staff client, pre-login: `curl -sk https://wifi.gadingkonseling.org/` | `{"ok":true}` |
| 12 | Staff client opens the portal → **Masuk dengan SSO** → Casdoor | login completes, `https://wifi.gadingkonseling.org/callback/staff` returns 200 |
| 13 | MikroTik: `/ip hotspot active print` | staff user with `profile-staff`, guest `T-<mac>` with `profile-guest` |
| 14 | Two clients on the same SSID: `ping` each other | **fails** (client isolation) |
| 15 | Guest client: `nc -zv 10.10.10.199 22` | **fails** (guest → RFC1918 blocked) |

---

## 8. Rollback

On the AP (if it is still reachable):

```sh
ls /etc/config/*.bak.*        # pick the stamp used in §6.1
for f in network wireless firewall dhcp dropbear; do
    [ -f "/etc/config/$f.bak.$STAMP" ] && cp "/etc/config/$f.bak.$STAMP" "/etc/config/$f"
done
/etc/init.d/network reload; wifi reload; /etc/init.d/firewall reload
```

If it is **not** reachable: do nothing for 5 minutes (watchdog), or power-cycle and use
**failsafe**: hold reset while powering on until the LED blinks fast → SSH `192.168.1.1` (no password) → fix or `firstboot -y`.

---

## 9. Known pitfalls (all of these already bit us)

| Symptom | Cause | Fix |
|---------|-------|-----|
| AP reachable before `wifi reload`, gone after | `network.lan` had no address, or `br-lan` lost its ports | §6.2 + §6.4: trunk **and** LAN ports must be in VLAN 1 |
| `lan1..lan4` dead, `192.168.1.1` unreachable | `bridge-vlan` exists but the LAN ports were not listed in VLAN 1 | §6.4 |
| `wget` on the AP: `Operation not permitted` | AP has no default route (no WAN internet) | §1 — cable in the **WAN** port |
| `opkg: not found` | OpenWrt ≥ 25 uses `apk` | use `apk add`, not `opkg` |
| fw4 fails to reload, old rules stay active | a zone referenced the deleted `wan` network | §6.3 cleanup |
| `scp` fails: `sftp-server: not found` | OpenSSH 9 defaults to SFTP, dropbear has none | `scp -O` |
| SSID missing on 5 GHz | only `radio0` configured | §6.6 loops over **all** radios |
| Duplicate SSID entries | an old single-radio `wireless.staff` section survived | §6.6 deletes `wireless.staff` / `wireless.guest` first |
| Clients can reach the AP's LuCI | management ACL missing | §6.7 `Deny-mgmt-from-clients` |
| Two uplinks, one carries nothing | STP blocks the second path (loop prevention) | expected — the bridge provides redundancy |
