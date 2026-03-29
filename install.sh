#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
#  Stake AutoBot — Server Installer v1.2
#  Sets up on Linux servers with apt package manager
#  Sets up: Docker, FlareSolverr, venv, systemd, stakectl
# ─────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="$HOME/stake-bot"
SERVICE_NAME="stake"
TG_SERVICE_NAME="stake-tg"
SYSTEMD_DIR="$HOME/.config/systemd/user"
STAKECTL="$HOME/.local/bin/stakectl"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Stake AutoBot — Server Installer v1.3            ║"
echo "║   CLI Bot + Telegram Bot                           ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. System dependencies ─────────────────────────────
echo "[1/8] Installing system packages…"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    sqlite3 tmux curl

# ── 2. Docker ────────────────────────────────────────
echo "[2/8] Setting up Docker…"
if command -v docker &>/dev/null; then
    echo "   Docker already installed: $(docker --version)"
else
    echo "   Installing Docker…"
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$(whoami)"
    echo "   Docker installed. NOTE: You may need to log out and back in for group changes."
fi

# ── 3. FlareSolverr (Cloudflare bypass) ──────────────
echo "[3/8] Setting up FlareSolverr…"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^flaresolverr$'; then
    echo "   FlareSolverr already running."
elif docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^flaresolverr$'; then
    echo "   Starting existing FlareSolverr container…"
    docker start flaresolverr
else
    echo "   Pulling and starting FlareSolverr…"
    docker run -d --name flaresolverr \
        -p 8191:8191 \
        --restart unless-stopped \
        ghcr.io/flaresolverr/flaresolverr:latest
fi
# Verify
if curl -s http://localhost:8191/v1 &>/dev/null; then
    echo "   FlareSolverr OK at http://localhost:8191"
else
    echo "   WARNING: FlareSolverr not responding yet (may still be starting)."
    echo "   Check with: curl http://localhost:8191/v1"
fi

# ── 4. Bot directory ───────────────────────────────────
echo "[4/9] Setting up bot directory…"
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

# Copy shared/ library (cross-bot shared modules)
if [ -d "../shared" ]; then
    rm -rf "$INSTALL_DIR/shared"
    cp -r ../shared "$INSTALL_DIR/"
    echo "   shared/ library copied"
else
    echo "   shared/ not copied locally (pip package used instead)"
fi

# Copy .env
if [ -f ".env" ]; then
    cp .env "$INSTALL_DIR/.env"
    echo "   Copied .env to $INSTALL_DIR/.env"
elif [ -f ".env.example" ]; then
    cp .env.example "$INSTALL_DIR/.env"
    echo "   Created $INSTALL_DIR/.env from .env.example"
fi

# ── 5. Python virtual environment ─────────────────────
echo "[5/9] Creating Python virtual environment…"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 6. Systemd user services (runs as your user, no root) ─
echo "[6/9] Setting up systemd services…"
mkdir -p "$SYSTEMD_DIR"

# CLI bot service
cat > "$SYSTEMD_DIR/${SERVICE_NAME}.service" << SERVICEEOF
[Unit]
Description=Stake AutoBot — Multi-Game Auto-Betting Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
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

# Telegram bot service
cat > "$SYSTEMD_DIR/${TG_SERVICE_NAME}.service" << TGSERVICEEOF
[Unit]
Description=Stake Telegram Bot — Multi-Game Auto-Betting via Telegram
After=network-online.target
Wants=network-online.target

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

# Enable lingering so user services run after logout
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || true

systemctl --user daemon-reload
echo "   Services installed: ${SERVICE_NAME}.service, ${TG_SERVICE_NAME}.service"

# ── 7. Management scripts ─────────────────────────────
echo "[7/9] Creating management commands…"
mkdir -p "$HOME/.local/bin"

# stakectl — single command to manage everything
cat > "$STAKECTL" << 'CTLEOF'
#!/usr/bin/env bash
# Stake AutoBot — Management Command
set -euo pipefail

INSTALL_DIR="$HOME/stake-bot"
SERVICE="stake"
TG_SERVICE="stake-tg"
PYTHON="$INSTALL_DIR/venv/bin/python3"
BOT="$INSTALL_DIR/stake.py"
TG_ENV="$HOME/.stake_tg_env"

usage() {
    echo ""
    echo "  stakectl — Stake AutoBot Manager"
    echo ""
    echo "  SETUP:"
    echo "    stakectl setup       Run wizard to configure (saves config, doesn't start)"
    echo ""
    echo "  RUNNING:"
    echo "    stakectl start       Start bot as background daemon"
    echo "    stakectl stop        Stop the bot"
    echo "    stakectl restart     Restart the bot"
    echo ""
    echo "  MONITORING:"
    echo "    stakectl monitor     Attach live TUI to running daemon (Q=detach, S=stop)"
    echo "    stakectl status      Show bot status snapshot (non-interactive)"
    echo "    stakectl logs        Stream live logs"
    echo "    stakectl logs-full   Show last 200 log lines"
    echo ""
    echo "  SHORTCUTS:"
    echo "    stakectl interactive Start daemon + attach monitor in one command"
    echo "    stakectl tmux        Run monitor in a detachable tmux session"
    echo ""
    echo "  DATA:"
    echo "    stakectl stats       Show all-time session statistics"
    echo "    stakectl session ID  Full stats + streak distribution for a session"
    echo "    stakectl presets     List saved presets"
    echo "    stakectl update      Update bot from current directory"
    echo ""
    echo "  TELEGRAM BOT:"
    echo "    stakectl tg setup    Set Telegram bot token"
    echo "    stakectl tg start    Start Telegram bot service"
    echo "    stakectl tg stop     Stop Telegram bot service"
    echo "    stakectl tg restart  Restart Telegram bot service"
    echo "    stakectl tg status   Show Telegram bot status"
    echo "    stakectl tg logs     Stream Telegram bot logs"
    echo "    stakectl tg logs-full Show last 200 TG bot log lines"
    echo ""
    echo "  CLOUDFLARE:"
    echo "    stakectl flaresolverr           Show FlareSolverr status"
    echo "    stakectl flaresolverr start     Start FlareSolverr container"
    echo "    stakectl flaresolverr stop      Stop FlareSolverr container"
    echo "    stakectl flaresolverr restart   Restart FlareSolverr container"
    echo "    stakectl flaresolverr logs      Stream FlareSolverr logs"
    echo ""
}

cmd_setup() {
    echo "Running setup wizard…"
    "$PYTHON" "$BOT" --setup-only
}

cmd_start() {
    # check if config exists
    if [ ! -f "$HOME/.stake_autobot.json" ]; then
        echo "No config found. Run 'stakectl setup' first."
        exit 1
    fi
    systemctl --user start "$SERVICE"
    sleep 1
    if systemctl --user is-active --quiet "$SERVICE"; then
        echo "Bot started successfully."
        echo "  Check status:  stakectl status"
        echo "  View logs:     stakectl logs"
    else
        echo "Failed to start. Check logs: stakectl logs-full"
        exit 1
    fi
}

cmd_stop() {
    systemctl --user stop "$SERVICE"
    echo "Bot stopped."
    "$PYTHON" "$BOT" --status 2>/dev/null || true
}

cmd_restart() {
    systemctl --user restart "$SERVICE"
    sleep 1
    echo "Bot restarted."
    "$PYTHON" "$BOT" --status 2>/dev/null || true
}

cmd_status() {
    echo ""
    echo "── Service Status ──"
    systemctl --user status "$SERVICE" --no-pager 2>/dev/null || echo "  Service not running"
    echo ""
    echo "── Session Status ──"
    "$PYTHON" "$BOT" --status 2>/dev/null || echo "  No active session"
}

cmd_logs() {
    journalctl --user -u "$SERVICE" -f --no-pager
}

cmd_logs_full() {
    journalctl --user -u "$SERVICE" -n 200 --no-pager
}

cmd_monitor() {
    "$PYTHON" "$BOT" --monitor
}

cmd_interactive() {
    if [ ! -f "$HOME/.stake_autobot.json" ]; then
        echo "No config found. Run 'stakectl setup' first."
        exit 1
    fi
    # Start daemon if not running
    if ! systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
        systemctl --user start "$SERVICE"
        sleep 1
        if ! systemctl --user is-active --quiet "$SERVICE"; then
            echo "Failed to start daemon. Check: stakectl logs-full"
            exit 1
        fi
        echo "Daemon started."
    fi
    # Attach monitor
    "$PYTHON" "$BOT" --monitor
}

cmd_tmux() {
    SESSION="stake"
    # Ensure daemon is running
    if ! systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
        if [ ! -f "$HOME/.stake_autobot.json" ]; then
            echo "No config found. Run 'stakectl setup' first."
            exit 1
        fi
        systemctl --user start "$SERVICE"
        sleep 1
    fi
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "tmux session '$SESSION' already running."
        echo "  Attach:  tmux attach -t $SESSION"
    else
        tmux new-session -d -s "$SESSION" "$PYTHON $BOT --monitor"
        echo "Started monitor in tmux session '$SESSION'"
        echo "  Attach:  tmux attach -t $SESSION"
        echo "  Detach:  Ctrl+B then D"
    fi
}

cmd_stats() {
    "$PYTHON" "$BOT" --stats
}

cmd_session() {
    local sid="${1:-}"
    if [ -z "$sid" ]; then
        echo "Usage: stakectl session <ID>"
        echo "  Get full stats for a specific session (streaks, distribution, bets)"
        exit 1
    fi
    "$PYTHON" "$BOT" --session-bets "$sid"
}

cmd_presets() {
    "$PYTHON" "$BOT" --list-presets
}

cmd_update() {
    # Pull latest from git if inside a repo
    if git rev-parse --is-inside-work-tree &>/dev/null; then
        echo "Pulling latest from git…"
        git pull --ff-only || { echo "Git pull failed. Resolve manually."; exit 1; }
    fi
    if [ -f "stake.py" ]; then
        cp stake.py "$INSTALL_DIR/stake.py"
        cp requirements.txt "$INSTALL_DIR/" 2>/dev/null
        [ -f ".env.example" ] && cp .env.example "$INSTALL_DIR/"
        # Update deps if requirements changed
        "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" 2>/dev/null || true
        # Update TG bot files if present
        if [ -d "core" ] && [ -d "tg" ]; then
            mkdir -p "$INSTALL_DIR/core" "$INSTALL_DIR/tg"
            cp core/*.py "$INSTALL_DIR/core/"
            cp tg/*.py   "$INSTALL_DIR/tg/"
            # Update shared/ library
            if [ -d "../shared" ]; then
                rm -rf "$INSTALL_DIR/shared"
                cp -r ../shared "$INSTALL_DIR/"
                echo "Bot + TG bot + shared/ updated."
            else
                echo "Bot + TG bot files updated (shared/ not found)."
            fi
            # Auto-restart TG bot gracefully (sessions auto-resume)
            if systemctl --user is-active --quiet "$TG_SERVICE" 2>/dev/null; then
                echo "Restarting Telegram bot (sessions will auto-resume)…"
                systemctl --user restart "$TG_SERVICE"
                sleep 2
                if systemctl --user is-active --quiet "$TG_SERVICE"; then
                    echo "Telegram bot restarted. Active sessions resumed."
                else
                    echo "WARNING: TG bot failed to restart. Check: stakectl tg logs-full"
                fi
            fi
            # CLI bot still needs manual restart
            if systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
                echo "CLI bot still running. Restart with: stakectl restart"
            fi
        else
            echo "Bot updated. Restart with: stakectl restart"
        fi
    else
        echo "No stake.py in current directory."
        exit 1
    fi
}

cmd_flaresolverr() {
    local action="${1:-status}"
    case "$action" in
        status)
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^flaresolverr$'; then
                echo "FlareSolverr: RUNNING"
                curl -s http://localhost:8191/v1 2>/dev/null | head -c 200 || echo "  (not responding)"
            else
                echo "FlareSolverr: NOT RUNNING"
                echo "  Start with: stakectl flaresolverr start"
            fi
            ;;
        start)
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^flaresolverr$'; then
                echo "FlareSolverr already running."
            elif docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^flaresolverr$'; then
                docker start flaresolverr
                echo "FlareSolverr started."
            else
                docker run -d --name flaresolverr -p 8191:8191 --restart unless-stopped ghcr.io/flaresolverr/flaresolverr:latest
                echo "FlareSolverr started."
            fi
            ;;
        stop)
            docker stop flaresolverr 2>/dev/null && echo "FlareSolverr stopped." || echo "Not running."
            ;;
        restart)
            docker restart flaresolverr 2>/dev/null && echo "FlareSolverr restarted." || echo "Not running."
            ;;
        logs)
            docker logs -f flaresolverr
            ;;
        *)
            echo "Usage: stakectl flaresolverr [status|start|stop|restart|logs]"
            ;;
    esac
}

cmd_tg() {
    local action="${1:-}"
    case "$action" in
        setup)
            echo "Enter your Telegram bot token (from @BotFather):"
            read -r token
            if [ -z "$token" ]; then
                echo "No token provided."
                exit 1
            fi
            echo "STAKE_TG_TOKEN=$token" > "$TG_ENV"
            chmod 600 "$TG_ENV"
            echo "Token saved to $TG_ENV"
            echo "Start with: stakectl tg start"
            ;;
        start)
            if [ ! -f "$TG_ENV" ]; then
                echo "No TG token configured. Run 'stakectl tg setup' first."
                exit 1
            fi
            systemctl --user start "$TG_SERVICE"
            sleep 1
            if systemctl --user is-active --quiet "$TG_SERVICE"; then
                echo "Telegram bot started."
                echo "  Check status:  stakectl tg status"
                echo "  View logs:     stakectl tg logs"
            else
                echo "Failed to start. Check logs: stakectl tg logs-full"
                exit 1
            fi
            ;;
        stop)
            systemctl --user stop "$TG_SERVICE"
            echo "Telegram bot stopped."
            ;;
        restart)
            systemctl --user restart "$TG_SERVICE"
            sleep 1
            if systemctl --user is-active --quiet "$TG_SERVICE"; then
                echo "Telegram bot restarted."
            else
                echo "Failed to restart. Check: stakectl tg logs-full"
            fi
            ;;
        status)
            echo ""
            echo "── Telegram Bot Status ──"
            systemctl --user status "$TG_SERVICE" --no-pager 2>/dev/null || echo "  Service not running"
            ;;
        logs)
            journalctl --user -u "$TG_SERVICE" -f --no-pager
            ;;
        logs-full)
            journalctl --user -u "$TG_SERVICE" -n 200 --no-pager
            ;;
        env)
            ENV_FILE="$INSTALL_DIR/.env"
            if [ ! -f "$ENV_FILE" ]; then
                if [ -f "$INSTALL_DIR/.env.example" ]; then
                    cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
                else
                    echo "# Stake AutoBot — Environment" > "$ENV_FILE"
                    echo "STAKE_TG_TOKEN=" >> "$ENV_FILE"
                fi
            fi
            ${EDITOR:-nano} "$ENV_FILE"
            # Sync TG token to systemd env file
            TOKEN=$(grep -E "^STAKE_TG_TOKEN=" "$ENV_FILE" | cut -d= -f2- | tr -d ' ')
            if [ -n "$TOKEN" ]; then
                echo "STAKE_TG_TOKEN=$TOKEN" > "$TG_ENV"
                chmod 600 "$TG_ENV"
                echo "Token synced to $TG_ENV"
            fi
            ;;
        *)
            echo "Usage: stakectl tg [setup|start|stop|restart|status|logs|logs-full|env]"
            ;;
    esac
}

# ── Dispatch ──────────────────────────────────────────
case "${1:-}" in
    setup)        cmd_setup ;;
    start)        cmd_start ;;
    stop)         cmd_stop ;;
    restart)      cmd_restart ;;
    status)       cmd_status ;;
    monitor)      cmd_monitor ;;
    logs)         cmd_logs ;;
    logs-full)    cmd_logs_full ;;
    interactive)  cmd_interactive ;;
    tmux)         cmd_tmux ;;
    stats)        cmd_stats ;;
    session)      cmd_session "${2:-}" ;;
    presets)      cmd_presets ;;
    update)       cmd_update ;;
    tg)           cmd_tg "${2:-}" ;;
    flaresolverr) cmd_flaresolverr "${2:-}" ;;
    *)            usage ;;
esac
CTLEOF
chmod +x "$STAKECTL"

# ── 8. TG bot env file placeholder ─────────────────────
echo "[8/9] Checking Telegram bot config…"
if [ -f "$HOME/.stake_tg_env" ]; then
    echo "   Telegram bot token already configured."
else
    echo "   No TG token yet. Set with: stakectl tg setup"
fi

# ── 9. Quick-run scripts (kept for compatibility) ─────
echo "[9/9] Creating helper scripts…"

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
echo "  Both bots run as systemd user services — they will:"
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
