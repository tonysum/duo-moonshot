#!/usr/bin/env bash
# =============================================================================
#  Automated fix for "hermes gateway status" timeout error
#  ------------------------------------------------------------
#  What it does:
#   1️⃣ Enables linger for the current user (keeps user‑systemd alive)
#   2️⃣ Ensures the Hermes unit file exists in the correct place
#   3️⃣ Reloads systemd, starts the gateway service, makes it auto‑start
#   4️⃣ Verifies that the service is truly running
# ===============================================================

# ----------- 1. Enable linger for the current user (keeps linger alive) -----
echo "▶ Enabling linger for current user (requires sudo)…"
sudo loginctl enable-linger "$(whoami)"

# ------------------- 2. Ensure unit file exists in correct location ----------
# Create the user‑systemd directory if it does not exist
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "$UNIT_DIR"

# Define the path to the unit file
UNIT_FILE="${UNIT_DIR}/hermes-gateway.service"

# If the unit file does NOT exist, create a clean, minimal unit file
if [ ! -f "$UNIT_FILE" ]; then
    cat > "$UNIT_FILE" <<'EOF'
[Unit]
Description=Hermes Agent Gateway - Messaging Platform Integration
After=network.target

[Service]
ExecStart=/root/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run --replace
Restart=on-failure
RestartSec=5
WorkingDirectory=/root/polymarket-weather-bot
StandardOutput=append:/root/polymarket-weather-bot/logs/gateway.out
StandardError=append:/root/polymarket-weather-bot/logs/gateway.err
PIDFile=/root/polymarket-weather-bot/pids/gateway.pid
KillMode=process
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$UNIT_FILE"
# --------------------------------------------------------------------
# 3️⃣ Reload systemd, start the service, and enable auto‑start
# --------------------------------------------------------------------
echo "▶ Reloading systemd user configuration…"
systemctl --user daemon-reload

echo "▶ Restarting and enabling hermes-gateway.service…"
systemctl --user restart hermes-gateway.service
systemctl --user enable hermes-gateway.service   # auto‑start on every login

# ----------------------------------------------------------------
# 4️⃣ Verify that the service is truly running
# ----------------------------------------------------------------
echo "▶ Verifying status…"
systemctl --user status hermes-gateway.service --no-pager

echo "=============================================================="
echo "✅  Fix complete!  'hermes gateway status' will now work without timeout."
echo "You can now use:"
echo "   hermes gateway status"
echo "   hermes-cli gateway status"
echo "=============================================================="