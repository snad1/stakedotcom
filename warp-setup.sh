#!/bin/bash
# warp-setup.sh — Install Cloudflare WARP as a LOCAL PROXY (not full-tunnel).
#
# Why proxy mode and not full-tunnel:
#   Full-tunnel WARP captures ALL outbound traffic, including SSH responses,
#   which can lock you out of the server. Proxy mode runs WARP as a SOCKS5
#   server on 127.0.0.1:40000 — only programs that explicitly opt in route
#   through WARP. SSH and everything else keep working unchanged.
#
# Usage:
#   1. sudo bash warp-setup.sh
#   2. After it finishes, run the bot with:
#        python3 stake.py --proxy socks5://127.0.0.1:40000
#      (the wizard's Proxy URL prompt also accepts this value)
#
# Why this might bypass CF:
#   Cloudflare WARP gives you a Cloudflare-owned exit IP. CF's bot scoring
#   often treats traffic between two Cloudflare endpoints (WARP -> CF-protected
#   site) more leniently than datacenter-IP traffic.
#
# Free, no signup, no email. Survives reboots once configured.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash $0"
    exit 1
fi

echo "=== Cloudflare WARP setup (proxy mode — SSH-safe) ==="

if ! command -v lsb_release >/dev/null 2>&1; then
    apt update -qq && apt install -y lsb-release
fi
CODENAME=$(lsb_release -cs)

if ! command -v warp-cli >/dev/null 2>&1; then
    echo "Installing cloudflare-warp..."
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
        | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $CODENAME main" \
        > /etc/apt/sources.list.d/cloudflare-client.list
    apt update -qq
    apt install -y cloudflare-warp
fi

# Make sure WARP is disconnected before we reconfigure
warp-cli disconnect 2>/dev/null || true

if ! warp-cli account 2>/dev/null | grep -q "Account type"; then
    echo "Registering free WARP account..."
    warp-cli registration new
fi

# CRITICAL: switch to proxy mode BEFORE connecting, so WARP never captures all traffic.
# In proxy mode, WARP listens on 127.0.0.1:40000 as SOCKS5 — opt-in only.
echo "Setting WARP to proxy mode (SOCKS5 on 127.0.0.1:40000)..."
warp-cli mode proxy

echo "Connecting WARP tunnel (proxy mode)..."
warp-cli connect
sleep 3

echo
echo "WARP status:"
warp-cli status || true
echo
echo "Server's public IP (unchanged — direct path still active):"
curl -s --max-time 10 https://ifconfig.me || echo "(could not reach ifconfig.me)"
echo

echo
echo "Exit IP via WARP proxy (this is what stake.com will see):"
curl -s --max-time 10 --proxy socks5h://127.0.0.1:40000 https://ifconfig.me || echo "(WARP proxy not reachable yet — may take a few seconds)"
echo

echo
echo "=== Done ==="
echo "SSH is unaffected. Run the bot with:"
echo "  python3 stake.py --proxy socks5://127.0.0.1:40000"
echo
echo "To stop WARP:    warp-cli disconnect"
echo "To start WARP:   warp-cli connect"
echo "Status:          warp-cli status"
