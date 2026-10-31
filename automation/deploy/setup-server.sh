#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 Vultr server for the MedChain automation stack.
# Run as root:   bash setup-server.sh
set -euo pipefail

echo "==> Updating packages"
apt-get update -y && apt-get upgrade -y
apt-get install -y ca-certificates curl unzip ufw

echo "==> Installing Docker (official script)"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "==> Firewall: allow only SSH, HTTP, HTTPS"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> 2 GB swap (helps n8n + image processing on small servers)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

mkdir -p /opt/medchain
echo
echo "Done. Docker: $(docker --version)"
echo "Next: copy the code to /opt/medchain (see automation/deploy/README.md)."
