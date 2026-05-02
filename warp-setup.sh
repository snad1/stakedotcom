#!/bin/bash
# warp-setup.sh — Install Cloudflare WARP on this server.
# WARP is a free Cloudflare service that routes all traffic through their network.
# Since stake.com is itself behind Cloudflare, CF often treats WARP-originated
# traffic to its own customers more leniently than datacenter IPs.
#
# Free, no signup, no email. Just install and connect.
#
# Usage:    sudo bash warp-setup.sh
# Disconnect:  warp-cli disconnect
# Reconnect:   warp-cli connect
# Status:      warp-cli status
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash $0"
    exit 1
fi

echo "=== Cloudflare WARP setup ==="

if ! command -v lsb_release >/dev/null 2>&1; then
    apt update -qq && apt install -y lsb-release
fi

CODENAME=$(lsb_release -cs)
echo "Distro codename: $CODENAME"

if ! command -v warp-cli >/dev/null 2>&1; then
    echo "Installing cloudflare-warp package..."
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
        | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $CODENAME main" \
        > /etc/apt/sources.list.d/cloudflare-client.list
    apt update -qq
    apt install -y cloudflare-warp
fi

echo
echo "Original public IP:"
curl -s --max-time 10 https://ifconfig.me || echo "(could not reach ifconfig.me)"
echo

if ! warp-cli account 2>/dev/null | grep -q "Account type"; then
    echo "Registering with Cloudflare WARP (free, no email)..."
    warp-cli registration new
fi

# Default mode is "warp" which proxies all traffic. Make sure we're in this mode.
warp-cli mode warp 2>/dev/null || true

echo "Connecting WARP tunnel..."
warp-cli connect
sleep 3

echo
echo "New exit IP (should be a Cloudflare IP, not your VPS IP):"
curl -s --max-time 10 https://ifconfig.me || echo "(could not reach ifconfig.me)"
echo
echo
echo "If the new IP is different from your original VPS IP, WARP is active."
echo "Now re-run the stake bot — Cloudflare may let it through."
echo
echo "Status:    warp-cli status"
echo "Stop:      warp-cli disconnect"
echo "Restart:   warp-cli connect"
