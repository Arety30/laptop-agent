#!/usr/bin/env bash
# Runs INSIDE the laptop's KVM VM (via vm/vmctl.sh ssh). Puts Tailscale in the VM
# — NOT on the host — so your laptop's WiFi/networking is never touched. The VM
# then forwards public traffic (arriving from the Oracle anchor over Tailscale)
# to your host's Apache on :80.
#
# Usage (from the host):
#   TS_AUTHKEY=tskey-xxxx bash expose/vm-tailscale.sh
set -euo pipefail

TS_AUTHKEY="${TS_AUTHKEY:?paste your tailscale auth key: TS_AUTHKEY=tskey-...}"
HOST_APP="${HOST_APP:-10.0.2.2:80}"     # host reachable from QEMU user-net at 10.0.2.2
VMCTL="$(dirname "$0")/../vm/vmctl.sh"

_vm() { bash "$VMCTL" ssh "$1"; }        # run a command in the VM over ssh

echo "== install tailscale + socat in the VM =="
_vm "command -v tailscale >/dev/null || (curl -fsSL https://tailscale.com/install.sh | sudo sh)"
_vm "command -v socat >/dev/null || (sudo apt-get update -y && sudo apt-get install -y socat)"

echo "== join the tailnet (non-interactive, uses your auth key) =="
_vm "sudo tailscale up --authkey='${TS_AUTHKEY}' --hostname=aretytech-anchor --ssh"

echo "== forward VM:80 -> host Apache (${HOST_APP}) so the anchor can reach your apps =="
_vm "sudo bash -c 'cat >/etc/systemd/system/anchor-proxy.service' <<UNIT
[Unit]
Description=forward :80 to host apache
After=network.target
[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:80,fork,reuseaddr TCP:${HOST_APP}
Restart=always
[Install]
WantedBy=multi-user.target
UNIT"
_vm "sudo systemctl daemon-reload && sudo systemctl enable --now anchor-proxy"

echo
echo "== VM tailnet IP (this is LAPTOP_TS_IP for the Oracle anchor) =="
_vm "tailscale ip -4"
echo "Give that 100.x.y.z to anchor-setup.sh as LAPTOP_TS_IP."
