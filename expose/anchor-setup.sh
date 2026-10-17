#!/usr/bin/env bash
# Run this ON the free Oracle Cloud "Always Free" VM (Ubuntu). It turns that VM
# into the public anchor: DuckDNS points at IT, and Caddy forwards traffic down
# a Tailscale tunnel to your laptop. Your home IP is never exposed.
#
# You provide 3 things (edit below or pass as env vars):
set -euo pipefail

DOMAIN="${DOMAIN:-HOST.duckdns.org}"        # your duckdns subdomain (full)
DUCKDNS_TOKEN="${DUCKDNS_TOKEN:?set DUCKDNS_TOKEN=... (from duckdns.org)}"
LAPTOP_TS_IP="${LAPTOP_TS_IP:?set LAPTOP_TS_IP=... (your laptop/VM tailscale IP, 100.x.y.z)}"
BACKEND_PORT="${BACKEND_PORT:-80}"               # port your app listens on, on the laptop side

echo "== 1. Tailscale (join your tailnet) =="
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up   # opens a login URL — approve this VM into your tailnet

echo "== 2. DuckDNS -> THIS vm's public IP (hides home IP) =="
sudo mkdir -p /opt/duckdns
sub="${DOMAIN%%.duckdns.org}"
cat <<EOF | sudo tee /opt/duckdns/update.sh >/dev/null
#!/usr/bin/env bash
# updates duckdns to this machine's current public IPv4 + IPv6
curl -s "https://www.duckdns.org/update?domains=${sub}&token=${DUCKDNS_TOKEN}&ip=&ipv6=" -o /opt/duckdns/duck.log
EOF
sudo chmod +x /opt/duckdns/update.sh
sudo /opt/duckdns/update.sh
( sudo crontab -l 2>/dev/null; echo "*/5 * * * * /opt/duckdns/update.sh" ) | sudo crontab -

echo "== 3. Caddy (auto-HTTPS reverse proxy -> laptop over tailscale) =="
sudo apt-get update -y && sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update -y && sudo apt-get install -y caddy
cat <<EOF | sudo tee /etc/caddy/Caddyfile >/dev/null
# public entrypoint -> your laptop's app, tunneled over tailscale
${DOMAIN} {
    reverse_proxy ${LAPTOP_TS_IP}:${BACKEND_PORT}
}
EOF
sudo systemctl restart caddy

echo "== 4. Open Oracle's firewall for 80/443 =="
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT || true
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT || true
sudo netfilter-persistent save 2>/dev/null || true

echo
echo "DONE. Also add ingress rules for TCP 80 & 443 in the Oracle console"
echo "(VCN -> Security List). Then https://${DOMAIN} -> your laptop, home IP hidden."
