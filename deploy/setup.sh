#!/bin/bash
# Exit on errors, unset variables, and pipeline errors.
set -euo pipefail

# 1. Check if run as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run this script with root privileges (e.g. sudo ./setup.sh)"
  exit 1
fi

PROJECT_DIR="/opt/phdownloader"
VENV_DIR="$PROJECT_DIR/venv"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

require_python_311() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
}

echo "=== 1. Installing System Dependencies ==="
apt-get update
apt-get install -y python3.11 python3.11-venv ffmpeg fail2ban curl

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Error: $PYTHON_BIN is not installed. Install Python 3.11 or set PYTHON_BIN to a Python 3.11+ executable."
  exit 1
fi
if ! require_python_311 "$PYTHON_BIN"; then
  echo "Error: $PYTHON_BIN must be Python 3.11 or newer."
  exit 1
fi

echo "=== 2. Setting up Python Virtual Environment ==="
if [ -x "$VENV_DIR/bin/python" ] && ! require_python_311 "$VENV_DIR/bin/python"; then
  backup_dir="${VENV_DIR}.python310.$(date +%Y%m%d%H%M%S)"
  echo "Existing virtual environment uses Python below 3.11; moving it to $backup_dir"
  mv "$VENV_DIR" "$backup_dir"
fi
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating virtual environment in $VENV_DIR with $PYTHON_BIN..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "=== 3. Installing Python Dependencies ==="
# Upgrade pip inside venv
"$VENV_DIR/bin/pip" install --upgrade pip
# Install requirements
if [ -f "$PROJECT_DIR/requirements.txt" ]; then
  "$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
else
  echo "Warning: requirements.txt not found in $PROJECT_DIR."
fi
"$VENV_DIR/bin/python" -c 'import sys, curl_cffi, yt_dlp; assert sys.version_info >= (3, 11); print(f"Python {sys.version.split()[0]}, yt-dlp {yt_dlp.version.__version__}, curl-cffi {curl_cffi.__version__}")'

echo "=== 4. Configuring Systemd services ==="
if [ ! -f "$PROJECT_DIR/deploy/phdownloader-api.service" ] || [ ! -f "$PROJECT_DIR/deploy/phdownloader-worker.service" ]; then
  echo "Error: API or worker systemd service template not found."
  exit 1
fi
cp "$PROJECT_DIR/deploy/phdownloader-api.service" /etc/systemd/system/phdownloader-api.service
cp "$PROJECT_DIR/deploy/phdownloader-worker.service" /etc/systemd/system/phdownloader-worker.service

# Ensure log file exists and is writable
touch /var/log/phdownloader.log
chmod 640 /var/log/phdownloader.log

# The legacy single-process service cannot process RQ jobs. Stop it before the
# API is restarted so it cannot keep the API port occupied during migration.
if systemctl cat phdownloader >/dev/null 2>&1; then
  systemctl disable --now phdownloader
fi

# Reload systemd, enable and start the API and its RQ worker.
systemctl daemon-reload
systemctl enable phdownloader-api phdownloader-worker
systemctl restart phdownloader-api phdownloader-worker

echo "=== 5. Configuring Fail2ban ==="
# Copy fail2ban filter
if [ -f "$PROJECT_DIR/deploy/fail2ban-filter.conf" ]; then
  cp "$PROJECT_DIR/deploy/fail2ban-filter.conf" /etc/fail2ban/filter.d/phdownloader.conf
  echo "Fail2ban filter installed to /etc/fail2ban/filter.d/phdownloader.conf"
else
  echo "Error: fail2ban-filter.conf not found!"
  exit 1
fi

# Copy fail2ban jail
if [ -f "$PROJECT_DIR/deploy/fail2ban-jail.conf" ]; then
  cp "$PROJECT_DIR/deploy/fail2ban-jail.conf" /etc/fail2ban/jail.d/phdownloader.conf
  echo "Fail2ban jail installed to /etc/fail2ban/jail.d/phdownloader.conf"
else
  echo "Error: fail2ban-jail.conf not found!"
  exit 1
fi

# Restart fail2ban to load new jail
systemctl restart fail2ban

echo "=== 6. Verification Status ==="
echo "Checking phdownloader API service status:"
systemctl is-active phdownloader-api || echo "phdownloader-api service is NOT active"

echo "Checking phdownloader worker service status:"
systemctl is-active phdownloader-worker || echo "phdownloader-worker service is NOT active"

echo "Checking fail2ban service status:"
systemctl is-active fail2ban || echo "fail2ban is NOT active"

echo "Checking fail2ban jail status:"
fail2ban-client status phdownloader || echo "phdownloader jail is NOT loaded in fail2ban"

echo "==========================================="
echo "Setup complete! The app should be available at http://<your-server-ip>:5008"
echo "To check app logs, run: tail -f /var/log/phdownloader.log"
echo "==========================================="
