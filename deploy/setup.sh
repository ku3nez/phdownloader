#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

# 1. Check if run as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run this script with root privileges (e.g. sudo ./setup.sh)"
  exit 1
fi

PROJECT_DIR="/opt/phdownloader"
VENV_DIR="$PROJECT_DIR/venv"

echo "=== 1. Installing System Dependencies ==="
apt-get update
apt-get install -y python3 python3-pip python3-venv ffmpeg fail2ban curl

echo "=== 2. Setting up Python Virtual Environment ==="
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment in $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
else
  echo "Virtual environment already exists."
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

echo "=== 4. Configuring Systemd Daemon ==="
# Copy service template and replace Python executable path to use the venv
if [ -f "$PROJECT_DIR/phdownloader.service" ]; then
  sed "s|/usr/bin/python3|$VENV_DIR/bin/python3|g" "$PROJECT_DIR/phdownloader.service" > /etc/systemd/system/phdownloader.service
  echo "Systemd service file installed to /etc/systemd/system/phdownloader.service"
else
  echo "Error: phdownloader.service template not found!"
  exit 1
fi

# Ensure log file exists and is writable
touch /var/log/phdownloader.log
chmod 640 /var/log/phdownloader.log

# Reload systemd, enable and start the service
systemctl daemon-reload
systemctl enable phdownloader
systemctl restart phdownloader

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
echo "Checking phdownloader service status:"
systemctl is-active phdownloader || echo "phdownloader service is NOT active"

echo "Checking fail2ban service status:"
systemctl is-active fail2ban || echo "fail2ban is NOT active"

echo "Checking fail2ban jail status:"
fail2ban-client status phdownloader || echo "phdownloader jail is NOT loaded in fail2ban"

echo "==========================================="
echo "Setup complete! The app should be available at http://<your-server-ip>:5008"
echo "To check app logs, run: tail -f /var/log/phdownloader.log"
echo "==========================================="
