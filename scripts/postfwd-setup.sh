#!/bin/bash
# Install and configure postfwd for Tier 2 SMTP-level rate limiting.
# Run as root: sudo bash scripts/postfwd-setup.sh
set -e

echo "=== Installing postfwd ==="
if ! command -v postfwd &>/dev/null; then
    apt-get update -qq
    apt-get install -y postfwd
fi

echo "=== Deploying rules ==="
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RULES_SRC="$SCRIPT_DIR/../config/postfwd/rules.cf"
cp "$RULES_SRC" /etc/postfix/postfwd.cf
chmod 644 /etc/postfix/postfwd.cf

echo "=== Configuring systemd ==="
cat > /etc/systemd/system/postfwd.service <<'SYSTEMD'
[Unit]
Description=Postfix Policy Daemon (postfwd) — Tier 2 rate limiting
After=network.target postfix.service

[Service]
Type=simple
ExecStart=/usr/sbin/postfwd -f /etc/postfix/postfwd.cf --inet=127.0.0.1:10040 --user=nobody --group=nogroup
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable postfwd
systemctl start postfwd

echo "=== Configuring Postfix to use postfwd ==="
postconf -e "smtpd_recipient_restrictions = check_policy_service inet:127.0.0.1:10040, permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination"

echo "=== Reloading Postfix ==="
systemctl reload postfix

echo ""
echo "✓ postfwd Tier 2 rate limiting active."
echo "  Rules: /etc/postfix/postfwd.cf"
echo "  Policy service: 127.0.0.1:10040"
echo "  Logs: journalctl -u postfwd -f"
