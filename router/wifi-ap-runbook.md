# Wi-Fi AP runbook — OpenWrt APs on VLAN 100/200 (safe, step by step)

> **Audience:** an automated agent or engineer executing on-site.
> **Scope:** configure the OpenWrt access points so the MikroTik hotspots work on
> VLAN 100 (staff) and VLAN 200 (guest), **without ever losing management access**.
>
> **Do not skip steps. Do not continue past a failed VERIFY.**
> This document was written after a lockout; the safeguards below exist because
> of it.

---

## 0. Golden rules (read before touching anything)

1. **Configure each AP with a laptop plugged into one of its LAN ports.**
   Never rely on the switch/VLAN path while changing it.
2. **Never touch `ether3` on the MikroTik** — that is the office LAN and the
   only remote path. Everything AP-related is `ether4` / `ether5`.
3. **One AP at a time.** Pilot on a single AP, verify, then repeat.
4. **The script self-reverts.** After it runs you have **300 seconds** to create
   `/tmp/ap-keep` on the AP. If nobody confirms, it restores the previous config
   and reboots. That is intentional — it is the anti-lockout net.
5. When in doubt, power-cycle and use **failsafe mode** (§2.3). Nothing here is
   unrecoverable with physical access to the AP.

---

## 1. Current state of the network (already built — do not re-create)

### MikroTik (`10.10.10.1`, RouterOS 7.19.6, hEX)

| Item | Value |
|---|---|
| Office LAN | `10.10.10.0/24` on `ether3` — **untouched** |
| Trunk bridge | `hs-bridge`, `vlan-filtering=yes`, ports `ether4` + `ether5` |
| VLAN 100 (staff) | `10.77.10.1/24` on `vlan100-staff`, hotspot `hs-staff` (pool `pool-staff`, `10.77.10.10-250`) |
| VLAN 200 (guest) | `10.77.20.1/24` on `vlan200-guest`, hotspot `hs-guest` (pool `pool-guest`, `10.77.20.10-250`) |
| Management | `192.168.88.1/24` on `vlan100-staff` (same L2 as VLAN 100) |
| Staff hotspot | `login-by=cookie,http-pap`, cookie 1d, user profile `profile-staff` (20M/20M, session 1d) |
| Guest hotspot | `login-by=cookie,http-pap,trial`, cookie 4h, trial 4h reset 1d, profile `profile-guest` (5M/5M, session 4h) |
| Staff pages | `hotspot-staff/login.html` → redirects to the portal |
| Guest pages | `hotspot-guest/{login,alogin,status,logout,error}.html` (branded) |
| Portal | `https://wifi.gadingkonseling.org` → Caddy → `10.10.10.200:8585` (LXC, Portainer) |
| Temporary probe | `192.168.1.2/24` on `vlan100-staff` (remove in §7) |

### Ruijie RG-ES208GC smart switch (`192.168.88.2`, mgmt VLAN 100)

| Port | Connects to | Mode | PVID / Native | Tagged (Permit) |
|---|---|---|---|---|
| **7** | MikroTik `ether4` | Trunk | **1** | **1, 100, 200** |
| **8** | MikroTik `ether5` | Trunk | **1** | **1, 100, 200** |
| **3** | AP #1 (trunk port) | Trunk | **100** | **100, 200** |
| **4** | AP #2 (trunk port) | Trunk | **100** | **100, 200** |
| 1, 2, 5, 6 | spares | Access | 100 | — |

> VLAN 900 and VLAN 10 are obsolete — remove them everywhere.
> VLAN 100 must stay **tagged** on 7/8 (the router side is a tagged trunk) and
> **native 100** on the AP ports (the AP's management is untagged).

### Access point (OpenWrt 25.12.6, DSA, `apk`)

| Port | Role |
|---|---|
| `lan1` | **trunk to the switch** (Tagged 100/200 + untagged mgmt) |
| `lan2..lan4` | management/rescue (untagged, VLAN 1) |
| `wan` | unused (the script removes its interface) |

---

## 2. Preparation

### 2.1 Inventory (write these down before starting)

```
AP name: ______________   MAC (wan): ____________   planned mgmt IP: 192.168.88.21
Switch port: ____         Extra mgmt IP (rescue): 192.168.1.1 (automatic)
```

Use `.21`, `.22`, `.23`, … one per AP.

### 2.2 Reset the AP to a known state (recommended)

From a laptop plugged into a **LAN** port of the AP:

```sh
ssh root@192.168.1.1
# password: usually empty on a factory image (just press Enter)
firstboot -y && reboot
```

Wait ~90 s. The AP is back at **`192.168.1.1`**, user `root`, **no password**.

> If the AP is already a clean factory image, skip this step.

### 2.3 Failsafe recovery (use if the AP is unreachable later)

1. Unplug power.
2. Hold **reset**, plug power back in while holding.
3. Keep holding until the LED blinks **fast** (~10 s), then release.
4. Laptop on a LAN port, static IP `192.168.1.10/24`.
5. `ssh root@192.168.1.1` → then `mount_root` to edit configs, or `firstboot -y && reboot` to start clean.

---

## 3. Configure ONE AP (pilot)

### Step 3.1 — cable the laptop, not the switch

- Laptop → **AP `lan2`** (a LAN port, **not** `lan1`, **not** `wan`).
- Laptop network settings: static **`192.168.1.10`**, mask `255.255.255.0`, gateway `192.168.1.1`.

**VERIFY**
```
ping 192.168.1.1        → replies
```

If it does not reply: the cable is in the `wan` port. Move it to `lan2` and retry.

### Step 3.2 — get shell access

```sh
ssh root@192.168.1.1
```

**VERIFY** — you get a shell prompt `root@OpenWrt:~#`. Then:

```sh
. /etc/openwrt_release; echo "$DISTRIB_ID $DISTRIB_RELEASE"
```

Expected: `OpenWrt 25.12.x` (or 24.10.x). If it is a vendor/Viettel image, stop — the script targets stock OpenWrt.

### Step 3.3 — record the current uplink port name

```sh
ip -o link | awk -F': ' '{print $2}' | grep -E 'lan|wan'
ip route
```

Expected something like:

```
wan
lan4@eth0
lan3@eth0
lan2@eth0
lan1@eth0
br-lan
default via 192.168.1.1 dev br-lan   (or no default at all)
```

The port you will cable to the switch is **`lan1`** → this is `TRUNK=lan1` below.
If your AP uses `lan2` for the trunk, use that instead.

### Step 3.4 — install the script

Copy the script from the appendix (§9) into `/tmp/ap.sh` on the AP, e.g.:

```sh
cat > /tmp/ap.sh <<'ENDOFSCRIPT'
... paste the whole §9 content ...
ENDOFSCRIPT
```

Or upload it with scp from the laptop (`OpenSSH 9+ needs -O` because dropbear has no sftp):

```powershell
scp -O ap.sh root@192.168.1.1:/tmp/ap.sh
```

**VERIFY**

```sh
wc -l /tmp/ap.sh      → more than 300 lines
sh -n /tmp/ap.sh      → prints nothing  (syntax OK)
```

**Do not continue if `sh -n` prints anything.**

### Step 3.5 — run it

```sh
TRUNK=lan1 MGMT=static MGMT_IP=192.168.88.21 sh /tmp/ap.sh
```

Expected log lines (order matters):

```
== backup: /etc/config/{network,wireless,firewall,dhcp}.bak.<stamp>
== packages: full wpad (OWE + 802.11w)
== network: lan ports = [lan1 lan2 lan3 lan4] trunk = lan1
== network: bridge VLANs 1 (mgmt) / 100 (staff) / 200 (guest)
== network: rescue address 192.168.1.1 on the LAN ports (always reachable)
== wireless: radios = radio0 radio1
== wireless: Office@GadingCounseling on [radio0 radio1] (dual band)
== wireless: Guest@GadingCounseling on [radio0 radio1]
== mgmt: ssh+luci open on all zones, dropbear on all interfaces
== applying
== watchdog: revert+reboot in 300s unless /tmp/ap-keep exists
== CONFIRM within 300s:  ssh root@<new-ip> 'touch /tmp/ap-keep'
```

> **You now have 5 minutes.** Watchdog countdown started.

### Step 3.6 — CONFIRM IMMEDIATELY (do not skip)

**From the same laptop session** (it is still on `lan2`, which stays in the
management VLAN, so `192.168.1.1` still answers):

```sh
ping -c2 192.168.1.1
ssh root@192.168.1.1 'touch /tmp/ap-keep'
```

Then verify the new address is live:

```sh
ping -c2 192.168.1.1
```

**VERIFY** — `192.168.1.1` replies **after** the change. The watchdog is disarmed
only when `/tmp/ap-keep` exists:

```sh
ssh root@192.168.1.1 'ls -l /tmp/ap-keep'
```

If `ssh` is refused but `ping` works, try again within the window; if it fails
completely, do nothing and let the watchdog revert (§5).

### Step 3.7 — check the radio and VLAN config on the AP

```sh
ssh root@192.168.1.1 '
  echo "--- interfaces";  uci show wireless | grep -E 'ssid|device|encryption'
  echo "--- bridge vlans"; uci show network | grep bridge-vlan
  echo "--- radios up";    iwinfo | grep -E "^[a-z0-9]+ +ESSID"
'
```

**VERIFY**

* four `wifi-iface` sections: `staff_radio0`, `staff_radio1`, `guest_radio0`, `guest_radio1`
* both SSIDs visible twice (2.4 GHz + 5 GHz), `encryption='owe'`
* `bridge-vlan` contains VLAN 1 (untagged: lan1..lan4 + trunk) and VLAN 100/200 (tagged: trunk)
* each SSID has `network='staff'` / `network='guest'`

### Step 3.8 — move the AP onto the switch

1. Unplug the laptop.
2. Cable **AP `lan1` → switch port 3** (port 4 for the second AP).
3. Nothing else in the AP's other LAN ports.

**VERIFY from the office LAN** (the engineer's machine or the devserver `10.10.10.199`):

```sh
ping 192.168.88.21                                  → replies
ssh root@192.168.88.21 'uptime'                     → works
```

If it does not answer: the switch port's PVID is not 100, or the trunk cable is
in the wrong AP port → fix the cable/port, the AP is still reachable at
`192.168.1.1` through `lan2`.

---

## 4. Verify the Wi-Fi (do this ONCE, on the pilot AP)

### 4.1 SSIDs are broadcast, both bands

On a phone/tablet in range, the Wi-Fi list must show:

```
Office@GadingCounseling     (2.4 GHz and 5 GHz — same name twice is normal)
Guest@GadingCounseling      (2.4 GHz and 5 GHz)
```

No password prompt appears (OWE = "Enhanced Open"; Android shows `WPA3-OWE`).

### 4.2 Staff client gets VLAN 100

Connect a device to `Office@GadingCounseling`, then on the MikroTik:

```rsc
/ip hotspot host print
```

**VERIFY** — the device appears with `SERVER = hs-staff` and an address in
`10.77.10.x`.

### 4.3 Guest client gets VLAN 200

Connect a device to `Guest@GadingCounseling`:

```rsc
/ip hotspot host print
```

**VERIFY** — `SERVER = hs-guest`, address `10.77.20.x`.

### 4.4 Client isolation

From a staff client, try to reach another client on the same SSID:

```
ping <other-client-ip>     → must fail (timeout / unreachable)
```

### 4.5 Management is NOT reachable from clients

From a staff client:

```sh
nc -zv 192.168.88.21 22      → must be blocked/timeout
nc -zv 192.168.88.21 80      → must be blocked/timeout
```

From the office LAN both must work.

### 4.6 Staff flow (SSO)

1. Forget/rejoin `Office@GadingCounseling`.
2. The browser opens `https://wifi.gadingkonseling.org/hotspot?ip=10.77.10.x…`
   (branded page, one **Masuk dengan SSO** button).
3. Casdoor login (password + TOTP).
4. Back to the portal → short "Menghubungkan…" page → online.

**VERIFY on the MikroTik**

```rsc
/ip hotspot user print
/ip hotspot active print
```

Must show a hotspot user named after the Casdoor account with `profile-staff`,
and an active session in `10.77.10.x`. Rate limit 20 Mbit.

### 4.7 Guest flow (trial)

1. Rejoin `Guest@GadingCounseling` → branded **Wi-Fi Tamu** page.
2. Tap **Hubungkan ke Internet** → status page ("Anda Terhubung", 4 h, 5 Mbit).

**VERIFY**

```rsc
/ip hotspot active print        → user T-<mac>, profile-guest
```

---

## 5. If something went wrong (watchdog did its job)

Symptom: after step 3.5 you cannot reach the AP and 5 minutes pass.

* The AP restores the **previous** config from
  `/etc/config/{network,wireless,firewall,dhcp}.bak.<stamp>` and reboots.
* It is reachable again at its **old** address (`192.168.1.1` on a LAN port).

Then:

```sh
ssh root@192.168.1.1
logread | grep -i ap-setup      # shows "watchdog: no confirmation, reverting"
ls /etc/config/*.bak.*          # the timestamp of the attempt
```

Fix the cause (§8), then re-run step 3.5.

---

## 6. Roll out to the remaining APs

Repeat §3 per AP with these differences only:

| AP | `MGMT_IP` | Switch port |
|---|---|---|
| AP #1 (pilot) | `192.168.88.21` | 3 |
| AP #2 | `192.168.88.22` | 4 |
| AP #3 | `192.168.88.23` | 5 (set the port to Trunk/PVID 100/tagged 100,200 first) |

Same command each time:

```sh
TRUNK=lan1 MGMT=static MGMT_IP=192.168.88.22 sh /tmp/ap.sh
```

Do **not** run two APs at once.

---

## 7. Clean up the MikroTik afterwards

Only after every AP is verified:

```rsc
# remove the temporary probe addresses
/ip address remove [find address="192.168.1.2/24"]
/ip address remove [find address="10.20.30.2/24"]

# confirm what should remain
/ip address print
```

Expected remaining: `10.10.10.1/24` (ether3), `10.77.10.1/24` (vlan100-staff),
`10.77.20.1/24` (vlan200-guest), `192.168.88.1/24` (vlan100-staff).

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `192.168.1.1` unreachable with laptop on `lan2` | cable in the AP's `wan` port | move the cable to `lan2` |
| `192.168.1.1` unreachable, laptop gets no link | dead PSU/cable | swap cable/PSU |
| After step 3.5 no `192.168.88.21` | switch port PVID ≠ 100 | set port 3/4 to Trunk, Native 100, Permit 100,200 |
| After step 3.5 no `192.168.1.1` either | `network reload` failed | wait for the watchdog, then `logread` and re-run |
| SSIDs missing | `wpad-basic` has no OWE | `apk add wpad-mbedtls` (needs internet) |
| SSIDs present but clients can't join | OWE unsupported by the radio | re-run with `ENC_STAFF=psk2 ENC_GUEST=psk2 KEY=... ` (script supports `ENC_*`) |
| Client joins, gets no IP | VLAN not reaching the AP | verify `bridge-vlan` on the AP and PVID on the switch |
| Client gets `10.77.20.x` on the staff SSID | SSID↔network mismatch | check `wifi-iface.network` = `staff`/`guest` |
| Portal shows `403 staff network` | client IP not in `10.77.10.x`, or `STAFF_HOTSPOT` env wrong | check `/ip hotspot host print`; env must be `STAFF_HOTSPOT=hs-staff`, `STAFF_PREFIX=10.77.10.` |
| Portal shows `expired login` | gunicorn worker mismatch / stale cookie | confirm the stack was redeployed after commit `ab75012` |
| Portal shows `500` on `/callback/staff` | router rejected `POST /rest/ip/hotspot/user` | check `podman logs wifi-portal`; the log prints the exact payload/response |
| Casdoor page unstyled / captcha missing | walled garden missing `cdn.casbin.org` | `/ip hotspot walled-garden add server=hs-staff dst-host=cdn.casbin.org action=allow` |

---

## 9. Appendix — the AP script (`/tmp/ap.sh`)

```sh
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
```

---

## 10. Sign-off checklist

- [ ] Every AP reachable at `192.168.88.2x` from the office LAN
- [ ] Every AP still reachable at `192.168.1.1` from its own LAN ports
- [ ] `/tmp/ap-keep` present on every AP (watchdog disarmed)
- [ ] Both SSIDs visible on 2.4 GHz **and** 5 GHz per AP
- [ ] Staff client → `10.77.10.x`, `hs-staff`, 20 Mbit
- [ ] Guest client → `10.77.20.x`, `hs-guest`, 5 Mbit, 4 h
- [ ] Client-to-client on the same SSID blocked
- [ ] AP management unreachable from both SSIDs
- [ ] Staff SSO completes through `https://wifi.gadingkonseling.org`
- [ ] Temporary addresses `192.168.1.2/24` and `10.20.30.2/24` removed from the MikroTik
