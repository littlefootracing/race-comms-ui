#!/bin/bash
# Push pi/ to the car's Raspberry Pi and (re)run its setup.
# Usage (from this folder on the Mac):  ./deploy-pi.sh [user@host]
# Reads the Pi password from .pi-password (gitignored — never committed) for sudo,
# and for SSH login too if sshpass is installed (brew install sshpass). An SSH key works without it.
set -e
PI="${1:-jfalicea@1662.local}"
cd "$(dirname "$0")"

SSH=(ssh -o StrictHostKeyChecking=accept-new)
PW=""
if [ -f .pi-password ]; then
  PW="$(head -n1 .pi-password)"
  command -v sshpass >/dev/null && SSH=(sshpass -f .pi-password "${SSH[@]}")
fi

echo "== Copying pi/ to $PI:~/race_comms/pi/"
"${SSH[@]}" "$PI" 'mkdir -p ~/race_comms/pi'
rsync -av -e "${SSH[*]}" --exclude startline.json --exclude racebox.json --exclude __pycache__ pi/ "$PI:~/race_comms/pi/"

echo "== Running setup on the Pi"
"${SSH[@]}" -t "$PI" "SUDO_PASS=$(printf %q "$PW") bash ~/race_comms/pi/install.sh"
