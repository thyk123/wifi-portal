# wifi-portal

Staff Wi-Fi login portal for **Gading Counseling & Empowerment Center**.

Bridges **Casdoor OIDC** to a **MikroTik HotSpot**: the user signs in with their
SSO account, the portal creates/updates a hotspot user via the RouterOS REST API,
and auto-submits the hotspot login form. Per-user speed limits come from the
hotspot user profile (`profile-staff`).

The **guest** hotspot does not use this service — it runs the router's own
themed trial login page.

## Flow

```
phone ──► hotspot walled garden
        └─► GET /hotspot?ip=…&mac=…
              └─► GET /hotspot/sso  ──► auth.gadingkonseling.org (Casdoor)
                    └─► GET /callback/staff?code=…&state=…
                          ├─ token exchange + userinfo
                          ├─ RouterOS REST: upsert hotspot user
                          └─ bridge.html ──► POST http://10.77.10.1/login
```

## Routes

| Route | Purpose |
|---|---|
| `GET /` | health probe → `{"ok": true}` |
| `GET /hotspot` | branded staff login page (one SSO button) |
| `GET /hotspot/sso` | starts the Casdoor OIDC authorization code flow |
| `GET /callback/staff` | Casdoor redirect target; creates hotspot user, bridges login |

`/hotspot` and `/hotspot/sso` return **403** for any client IP outside the staff
subnet / `hs-staff` hotspot.

## Required environment

See `.env.example`. The container refuses to boot with a clear message if any of
`SSO_URL`, `CASDOOR_CLIENT_ID`, `CASDOOR_CLIENT_SECRET`, `PORTAL_URL`,
`ROUTER_URL`, `ROUTER_USER`, `ROUTER_PASS` is missing.

`ROUTER_URL` should stay `https://10.10.10.1` (the portal-to-router hop is inside
the office LAN). `STAFF_LOGIN_URL` is what the end user's browser posts to — point
it at `https://hotspot.<domain>/login` once the router has a certificate.

## Deploy on Portainer (git repository)

1. Push this repository to a **public** git host.
2. Portainer → **Stacks** → **Add stack** → **Repository**
   - Repository URL: your repo URL
   - Compose path: `docker-compose.yml`
   - Build method: **Repository** (Portainer builds the image from `Dockerfile`)
3. **Environment variables** → paste the contents of `.env.example` with the
   real Casdoor client id / secret and router password.
4. **Deploy the stack**.

The code is baked into the image at build time — there is no volume to populate
and no file to paste into a container.

Verify:

```sh
curl -s http://127.0.0.1:8585/     # {"ok":true}
podman logs --tail 20 wifi-portal
```

## Deploy without Portainer

```sh
docker compose up -d --build
```

or with podman:

```sh
podman build -t wifi-portal:latest .
podman run -d --name wifi-portal --restart=unless-stopped \
  -p 8585:8585 --env-file .env wifi-portal:latest
```

## Reverse proxy

`wifi.<domain>` (public, Cloudflare) → `host.containers.internal:8585`.
RouterOS cannot import ECDSA certificates, so use an RSA key for the
`hotspot.<domain>` proxy host.

```caddyfile
wifi.example.org {
    tls internal
    reverse_proxy host.containers.internal:8585
}
```

## Security notes

- `.env` is git-ignored — **never commit `CASDOOR_CLIENT_SECRET` or `ROUTER_PASS`**.
- The Casdoor app must allow `https://wifi.<domain>/callback/staff` as a redirect URL.
- The portal talks to RouterOS with `verify=False` (self-signed cert on the LAN).
  Keep that hop inside the office VLAN; do not expose the RouterOS REST API to the internet.
- OIDC `state` values are held in process memory: keep `--workers` at 2 or fewer,
  or move `STATE` to Redis before scaling out.
- The staff hotspot must only be reachable with `login-by=http-pap,cookie`; the
  portal is the only thing that should be able to create users.
