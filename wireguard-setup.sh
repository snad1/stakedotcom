#!/bin/bash
# wireguard-setup.sh — Set up WireGuard on this server so the bot exits via a clean residential IP.
#
# Usage:
#   1. Get a WireGuard config from any provider with residential exits:
#        - Mullvad (https://mullvad.net) — has residential exits in most countries
#        - ProtonVPN (https://protonvpn.com)
#        - IVPN (https://ivpn.net)
#      Download a .conf file (e.g. wg0.conf).
#   2. Copy it to this server and run:
#        sudo bash wireguard-setup.sh /path/to/wg0.conf
#   3. Verify the new exit IP:
#        curl ifconfig.me
#      It should now show the VPN's IP, not the server's IP.
#   4. Re-run the stake bot — Cloudflare should now let it through.
#
# To stop the VPN:    sudo wg-quick down wg0
# To start the VPN:   sudo wg-quick up wg0
# To auto-start:      sudo systemctl enable wg-quick@wg0
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash $0 /path/to/wg0.conf"
    exit 1
fi

CONF="${1:-}"
if [ -z "$CONF" ] || [ ! -f "$CONF" ]; then
    echo "Usage: sudo bash $0 /path/to/wg0.conf"
    echo "Download a WireGuard config from Mullvad / ProtonVPN / IVPN first."
    exit 1
fi

echo "Installing WireGuard..."
apt update -qq
apt install -y wireguard resolvconf

echo "Copying config to /etc/wireguard/wg0.conf..."
mkdir -p /etc/wireguard
cp "$CONF" /etc/wireguard/wg0.conf
chmod 600 /etc/wireguard/wg0.conf

echo "Bringing tunnel up..."
wg-quick down wg0 2>/dev/null || true
wg-quick up wg0

echo
echo "Original public IP was: (whatever your VPS provider gave you)"
echo "New exit IP:"
curl -s --max-time 10 https://ifconfig.me || echo "(could not reach ifconfig.me)"
echo
echo
echo "If the new IP is different from your VPS IP, the tunnel is live."
echo "Re-run your stake bot now."
echo
echo "To enable on boot:    sudo systemctl enable wg-quick@wg0"
echo "To stop manually:     sudo wg-quick down wg0"
