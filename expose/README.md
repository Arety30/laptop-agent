# Expose a service publicly without leaking your home IP ($0)

Right now `HOST.duckdns.org` → **REDACTED_IP (your home IP)**. This makes
it point at a free public anchor instead, and tunnels traffic home over Tailscale.
Your home network's IP is never public. Cost: $0.

```
internet ──▶ HOST.duckdns.org ──▶ Oracle free VM (public IP)
                                          └─ Caddy (auto-HTTPS)
                                               └─ Tailscale tunnel ──▶ your laptop :80
```

## The one thing only you can do
I can't create cloud accounts or log into Tailscale for you, and I won't touch
your WiFi. So **you** do these; everything else is scripted:

### 1. Free Oracle "Always Free" VM (~5 min)
- oracle.com/cloud/free → sign up → create a VM instance (Ubuntu, Ampere/Always Free).
- In the VM's **VCN → Security List**, add ingress rules: TCP **80** and **443** from `0.0.0.0/0`.
- Note its public IP and SSH in.

### 2. Tailscale on your laptop (keeps WiFi intact — it just adds a tunnel)
Do this yourself (I'm not touching your networking):
```bash
sudo tailscale up          # approve your laptop in the browser
tailscale ip -4            # note this 100.x.y.z — it's LAPTOP_TS_IP
```
> Safer option (WiFi fully untouched): run Tailscale **inside the VM** instead —
> see `vm/` — so the tunnel lives in the sandbox, not the host.

### 3. DuckDNS token
duckdns.org → sign in → copy your **token**.

### 4. Run the anchor script ON the Oracle VM
```bash
scp expose/anchor-setup.sh ubuntu@<oracle-ip>:~
ssh ubuntu@<oracle-ip>
DUCKDNS_TOKEN=xxxx LAPTOP_TS_IP=100.x.y.z BACKEND_PORT=80 bash anchor-setup.sh
```
Done — `https://HOST.duckdns.org` now resolves to Oracle, HTTPS-terminated
there, and forwarded to your laptop's `:80` over Tailscale. Home IP hidden.

## Security (important)
- The reverse proxy points **only** at your laptop's `:80` (Apache → your web apps
  `:8100` / `:8000`). It does **NOT** expose the bot's task endpoint `:8765`
  (which runs shell commands) — keep it that way.
- Whatever you expose is now on the public internet with HTTPS. Don't put anything
  behind it that executes commands or holds secrets without auth.

## Simpler alternative (if you don't need the duckdns name)
Since Tailscale is installed, `tailscale funnel 80` gives a public
`https://<machine>.<tailnet>.ts.net` URL that tunnels to your laptop — no Oracle
VM, no Caddy, home IP hidden. You just don't get to use `HOST.duckdns.org`.
