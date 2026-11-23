#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
#  Stake AutoBot: Server Installer v1.4
#  Sets up on Linux servers with apt package manager
#  Sets up: Docker, FlareSolverr, venv, systemd, stakectl
# ─────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="$HOME/stake-bot"
SERVICE_NAME="stake"
LANE_SERVICE_NAME="stake-lane"
TG_SERVICE_NAME="stake-tg"
SYSTEMD_DIR="$HOME/.config/systemd/user"
STAKECTL="$HOME/.local/bin/stakectl"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Stake AutoBot: Server Installer v1.4            ║"
echo "║   CLI Bot + Telegram Bot                           ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. System dependencies ─────────────────────────────
echo "[1/8] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    sqlite3 tmux curl

# ── 2. Docker ────────────────────────────────────────
echo "[2/8] Setting up Docker..."
if command -v docker &>/dev/null; then
    echo "   Docker already installed: $(docker --version)"
else
    echo "   Installing Docker..."
    sudo apt-get install -y docker.io
    sudo usermod -aG docker "$(whoami)"
    echo "   Docker installed. NOTE: You may need to log out and back in for group changes."
fi

# ── 3. FlareSolverr (Cloudflare bypass) ──────────────
echo "[3/8] Setting up FlareSolverr..."
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^flaresolverr$'; then
    echo "   FlareSolverr already running."
elif docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^flaresolverr$'; then
    echo "   Starting existing FlareSolverr container..."
    docker start flaresolverr
else
    echo "   Pulling and starting FlareSolverr..."
    docker run -d --name flaresolverr \
        -p 127.0.0.1:8191:8191 \
        --restart unless-stopped \
        ghcr.io/flaresolverr/flaresolverr:v3.5.2
fi
# Verify
if curl -s http://localhost:8191/v1 &>/dev/null; then
    echo "   FlareSolverr OK at http://localhost:8191"
else
    echo "   WARNING: FlareSolverr not responding yet (may still be starting)."
    echo "   Check with: curl http://localhost:8191/v1"
fi

# ── 4. Bot directory ───────────────────────────────────
echo "[4/9] Setting up bot directory..."
mkdir -p "$INSTALL_DIR"
cp stake.py         "$INSTALL_DIR/"
cp requirements.txt "$INSTALL_DIR/"
# Copy TG bot module
mkdir -p "$INSTALL_DIR/core" "$INSTALL_DIR/tg"
cp core/__init__.py  "$INSTALL_DIR/core/"
cp core/strategy.py  "$INSTALL_DIR/core/"
cp core/database.py  "$INSTALL_DIR/core/"
cp core/engine.py    "$INSTALL_DIR/core/"
cp tg/__init__.py    "$INSTALL_DIR/tg/"
cp tg/config.py      "$INSTALL_DIR/tg/"
cp tg/database.py    "$INSTALL_DIR/tg/"
cp tg/engine.py      "$INSTALL_DIR/tg/"
cp tg/formatter.py   "$INSTALL_DIR/tg/"
cp tg/handlers.py    "$INSTALL_DIR/tg/"
cp tg/bot.py         "$INSTALL_DIR/tg/"

# Copy .env
if [ -f ".env" ]; then
    cp .env "$INSTALL_DIR/.env"
    echo "   Copied .env to $INSTALL_DIR/.env"
elif [ -f ".env.example" ]; then
    cp .env.example "$INSTALL_DIR/.env"
    echo "   Created $INSTALL_DIR/.env from .env.example"
fi

# ── 5. Python virtual environment ─────────────────────
echo "[5/9] Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 6. Systemd user services (runs as your user, no root) ─
echo "[6/9] Setting up systemd services..."
mkdir -p "$SYSTEMD_DIR"

# CLI bot service
cat > "$SYSTEMD_DIR/${SERVICE_NAME}.service" << SERVICEEOF
[Unit]
Description=Stake AutoBot: Multi-Game Auto-Betting Engine
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=0
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/stake.py --resume --daemon
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

# Graceful shutdown
KillSignal=SIGTERM
TimeoutStopSec=15

# Environment
Environment=HOME=${HOME}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
SERVICEEOF

# Parallel lanes 2..N: same unit as lane 1 plus --lane %i (stakectl lanes N)
cat > "$SYSTEMD_DIR/${LANE_SERVICE_NAME}@.service" << LANESERVICEEOF
[Unit]
Description=Stake AutoBot - parallel lane %i (CLI)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=0
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/stake.py --resume --daemon --lane %i
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

KillSignal=SIGTERM
TimeoutStopSec=15

Environment=HOME=${HOME}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
LANESERVICEEOF

# Telegram bot service
cat > "$SYSTEMD_DIR/${TG_SERVICE_NAME}.service" << TGSERVICEEOF
[Unit]
Description=Stake Telegram Bot: Multi-Game Auto-Betting via Telegram
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 -m tg.bot
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

# Graceful shutdown
KillSignal=SIGTERM
TimeoutStopSec=15

# Environment
Environment=HOME=${HOME}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=${INSTALL_DIR}
EnvironmentFile=-${HOME}/.stake_tg_env

[Install]
WantedBy=default.target
TGSERVICEEOF

# Watchdog: restart TG bot if heartbeat file is stale (catches stuck-but-alive)
cat > "$SYSTEMD_DIR/${TG_SERVICE_NAME}-watchdog.service" << WDSERVICEEOF
[Unit]
Description=Stake Telegram Bot: Stall Watchdog
After=${TG_SERVICE_NAME}.service

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'f=/tmp/${TG_SERVICE_NAME}.heartbeat; if [ ! -f "\$f" ] || [ \$(( \$(date +%%s) - \$(stat -c %%Y "\$f") )) -gt 180 ]; then echo "Heartbeat stale, restarting ${TG_SERVICE_NAME}"; systemctl --user restart ${TG_SERVICE_NAME}.service; fi'
WDSERVICEEOF

cat > "$SYSTEMD_DIR/${TG_SERVICE_NAME}-watchdog.timer" << WDTIMEREOF
[Unit]
Description=Stake Telegram Bot: Stall Watchdog Timer (every 2 min)

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
Unit=${TG_SERVICE_NAME}-watchdog.service

[Install]
WantedBy=timers.target
WDTIMEREOF

# Enable lingering so user services run after logout
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || true

systemctl --user daemon-reload
# Boot-enable only (no --now): a bot stopped by hand stays stopped until the next boot.
systemctl --user enable ${SERVICE_NAME} 2>/dev/null || true
systemctl --user enable --now ${TG_SERVICE_NAME}-watchdog.timer 2>/dev/null || true
echo "   Services installed: ${SERVICE_NAME}.service, ${LANE_SERVICE_NAME}@.service, ${TG_SERVICE_NAME}.service, ${TG_SERVICE_NAME}-watchdog.timer"

# ── 7. Management scripts ─────────────────────────────
echo "[7/9] Creating management commands..."
mkdir -p "$HOME/.local/bin"

install -m 0755 stakectl "$STAKECTL"

# ── 8. TG bot env file placeholder ─────────────────────
echo "[8/9] Checking Telegram bot config..."
if [ -f "$HOME/.stake_tg_env" ]; then
    echo "   Telegram bot token already configured."
else
    echo "   No TG token yet. Set with: stakectl tg setup"
fi

# ── 9. Quick-run scripts (kept for compatibility) ─────
echo "[9/9] Creating helper scripts..."

cat > "$INSTALL_DIR/run.sh" << 'EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$DIR/venv/bin/python3" "$DIR/stake.py" "$@"
EOF
chmod +x "$INSTALL_DIR/run.sh"

cat > "$INSTALL_DIR/run-tg.sh" << 'EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a; source "$DIR/.env" 2>/dev/null; set +a
cd "$DIR" && "$DIR/venv/bin/python3" -m tg.bot "$@"
EOF
chmod +x "$INSTALL_DIR/run-tg.sh"

# ── Done ──────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════"
echo "  Installation complete!"
echo "════════════════════════════════════════════════════"
echo ""
echo "  FIRST TIME SETUP:"
echo "    stakectl setup          Configure tokens, strategy, etc."
echo ""
echo "  DAILY USE:"
echo "    stakectl start          Start bot as background daemon"
echo "    stakectl stop           Stop the bot"
echo "    stakectl monitor        Attach live TUI to running daemon"
echo "    stakectl status         Quick status snapshot"
echo "    stakectl logs           Stream live logs"
echo "    stakectl stats          View all-time statistics"
echo "    stakectl session ID     Full stats + streak distribution"
echo ""
echo "  SHORTCUTS:"
echo "    stakectl interactive    Start daemon + attach monitor"
echo "    stakectl tmux           Monitor in detachable tmux session"
echo ""
echo "  TELEGRAM BOT:"
echo "    stakectl tg setup       Set bot token"
echo "    stakectl tg start       Start Telegram bot"
echo "    stakectl tg stop        Stop Telegram bot"
echo "    stakectl tg restart     Restart Telegram bot"
echo "    stakectl tg status      Check Telegram bot status"
echo "    stakectl tg logs        Stream Telegram bot logs"
echo ""
echo "  Both bots run as systemd user services: they will:"
echo "    - Auto-restart on crashes (after 10s)"
echo "    - Keep running after you log out (lingering enabled)"
echo "    - Log to journalctl (stakectl logs)"
echo ""
echo "  CLOUDFLARE BYPASS:"
echo "    FlareSolverr is running on http://localhost:8191"
echo "    The bot auto-detects it when direct connections fail."
echo "    CF cookies are cached at ~/.stake_cf_cookies.json (30-min TTL)"
echo ""
echo "  FILES:"
echo "    Bot:        $INSTALL_DIR/"
echo "    Config:     ~/.stake_autobot.json"
echo "    Database:   ~/.stake_autobot.db"
echo "    Logs:       ~/.stake_logs/stake.log"
echo "    CF cookies: ~/.stake_cf_cookies.json"
echo "    Service:    $SYSTEMD_DIR/${SERVICE_NAME}.service"
echo ""
