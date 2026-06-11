# Getting Started — Provisioning a Parousia Host

This guide covers provisioning a host for Parousia from scratch. It's **provider-agnostic** — any VPS, dedicated server, or bare-metal host with a public IP will work. For provider-specific notes (AWS SES, Hetzner firewall, etc.), see [docs/hosting.md](hosting.md).

---

## What you need before starting

- A **domain name** you control DNS for (e.g., `yourdomain.com`)
- A **host with a publicly routable static IP address** (VPS, dedicated server, EC2, etc.)
- **Root/sudo access** to the host
- Ability to open **inbound ports**: 25 (SMTP), 80 (HTTP), 443 (HTTPS), 8080 (REST)
- MCP transport is **stdio** — agents spawn the MCP server as a subprocess over SSH or local execution. No network port needed for MCP.

---

## Supported operating systems

Parousia uses standard Linux paths and system tools. It has been **tested on Ubuntu 24.04** and should work on any modern systemd-based Linux distribution.

| Distribution | Status | Notes |
|-------------|--------|-------|
| Ubuntu 24.04 | ✅ Tested | Primary development target |
| Ubuntu 22.04 | ✅ Compatible | Older Python — use `python3.10` or newer |
| Debian 12 | ✅ Compatible | Package names same as Ubuntu |
| RHEL 9 / Rocky 9 | ✅ Compatible | Different package names (see table below) |
| Fedora 40+ | ✅ Compatible | Different package names |
| Arch Linux | ⚠️ Untested | Should work — adjust package names |
| Alpine Linux | ❌ Unsupported | musl libc breaks some Python wheels |
| macOS | ❌ Unsupported | No systemd, different filesystem layout |

### Package names by distribution

| Software | Ubuntu / Debian | RHEL / Fedora | Arch |
|----------|----------------|---------------|------|
| Postfix | `postfix` | `postfix` | `postfix` |
| Redis | `redis-server` | `redis` | `redis` |
| Chromium | `chromium-browser` | `chromium` | `chromium` |
| OpenDKIM | `opendkim opendkim-tools` | `opendkim` | `opendkim` |
| SASL auth | `libsasl2-modules` | `cyrus-sasl-plain` | `libsasl2` |
| Certbot | `certbot` | `certbot` | `certbot` |

---

## Step 1: DNS records

Before installing anything, set up your DNS. These records tell the world where your mail server lives and who's authorized to send mail for your domain.

Create these records at your DNS provider (Cloudflare, Hostinger, Route53, etc.):

| Type | Name | Value | Priority | TTL |
|------|------|-------|----------|-----|
| **A** | `mail` | `<your-host-public-ip>` | — | 3600 |
| **MX** | `@` | `mail.yourdomain.com` | 10 | 3600 |
| **TXT** | `@` | `v=spf1 mx -all` | — | 3600 |

> **Temporary:** The SPF record above (`v=spf1 mx -all`) authorizes only your MX server to send mail. When you set up DKIM (Step 7) and DMARC, you'll update it. If using SES as a smarthost relay, you'll add `include:amazonses.com`.

Verify DNS propagation:
```bash
dig MX yourdomain.com +short
# Should return: 10 mail.yourdomain.com.

dig A mail.yourdomain.com +short
# Should return your host IP
```

---

## Step 2: Host setup

### 2a. SSH in and update

```bash
ssh root@<your-host-ip>
# or: ssh -i your-key.pem ubuntu@<your-host-ip>

# Set hostname
hostnamectl set-hostname mail.yourdomain.com

# Update system (Ubuntu/Debian)
apt update && apt upgrade -y

# or RHEL/Fedora:
# dnf update -y
```

### 2b. Open firewall ports

```bash
# ufw (Ubuntu/Debian)
ufw allow 22/tcp
ufw allow 25/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8080/tcp
ufw enable

# firewalld (RHEL/Fedora)
# firewall-cmd --permanent --add-port={25,80,443,8080}/tcp
# firewall-cmd --reload
```

> ⚠️ **Important:** Some hosting providers block port 25 by default (AWS, Google Cloud, Azure). You may need to request removal of this restriction. See [docs/hosting.md](hosting.md) for provider-specific instructions.

### 2c. Set up reverse DNS (PTR)

Your host's IP must have a PTR record pointing to `mail.yourdomain.com`. Without this, Gmail and Outlook will likely reject your mail.

The process varies by provider:
- **AWS:** File a Support Center case → Elastic IP → Reverse DNS
- **Hetzner:** Set in the Robot admin panel under the IP settings
- **DigitalOcean:** Set in the Networking → Domains section
- **Linode:** Set in the Network → IP Addresses → Edit RDNS
- **Self-hosted/colo:** Ask your ISP or IP block provider

Verify:
```bash
dig -x <your-host-ip> +short
# Should return: mail.yourdomain.com.
```

---

## Step 3: Install system packages

```bash
# Ubuntu / Debian
apt install -y postfix redis-server opendkim opendkim-tools libsasl2-modules certbot

# RHEL / Fedora
# dnf install -y postfix redis opendkim cyrus-sasl-plain certbot

# Arch
# pacman -S postfix redis opendkim libsasl2 certbot
```

> During Postfix install, select **"Internet Site"** when prompted. The system mail name should be `yourdomain.com`.

### Start Redis

```bash
systemctl enable --now redis-server   # Ubuntu/Debian
# systemctl enable --now redis        # RHEL/Fedora
```

---

## Step 4: Configure Postfix

Postfix is the SMTP server — it receives inbound mail from the internet and relays outbound mail to the world.

### 4a. Base configuration

```bash
# Set core identity
postconf -e "myhostname = mail.yourdomain.com"
postconf -e "mydomain = yourdomain.com"
postconf -e "myorigin = \$mydomain"
postconf -e "inet_interfaces = all"
postconf -e "inet_protocols = ipv4"

# Do NOT include $mydomain in mydestination — we use pipe transport, not local delivery
postconf -e "mydestination = \$myhostname, localhost.\$mydomain, localhost"

# Anti-spam defaults
postconf -e "smtpd_relay_restrictions = permit_mynetworks, reject_unauth_destination"
postconf -e "smtpd_recipient_restrictions = permit_mynetworks, reject_unauth_destination"
postconf -e "smtpd_recipient_limit = 50"
postconf -e "default_destination_rate_delay = 3s"
postconf -e "default_destination_concurrency_limit = 2"
```

### 4b. Pipe transport (routes agent mail to Parousia)

Inbound mail for `@yourdomain.com` should be piped to the Parousia guard script instead of delivered to a local mailbox.

```bash
# Tell Postfix to accept mail for your domain and relay it through the pipe transport
postconf -e "relay_domains = yourdomain.com"
postconf -e "relay_transport = parousia"
```

Add the pipe service to `/etc/postfix/master.cf`:
```bash
cat >> /etc/postfix/master.cf << 'EOF'
parousia  unix  -  n  n  -  -  pipe  flags=R user=parousia argv=/opt/parousia/parousia_pipe.py
EOF
```

Create the `parousia` system user:
```bash
useradd -r -s /bin/false parousia
```

### 4c. Create mailname and aliases

```bash
echo "yourdomain.com" > /etc/mailname
newaliases
```

### 4d. Restart Postfix

```bash
# Ubuntu 24.04 note: the default 'postfix' service is a dummy.
# Use 'postfix@-' instead.
systemctl enable --now postfix@-
# or on older distros:
# systemctl enable --now postfix
```

Verify Postfix is listening:
```bash
ss -tlnp | grep :25
# Should show: 0.0.0.0:25 ... master
```

---

## Step 5: Install Parousia

### 5a. Clone and install

```bash
mkdir -p /opt/parousia
git clone https://github.com/o3willard-AI/Machina-Parousia.git /opt/parousia
cd /opt/parousia

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 5b. Create the pipe script

Postfix's pipe transport will invoke this script for every inbound email:
```bash
cat > /opt/parousia/parousia_pipe.py << 'PYEOF'
#!/opt/parousia/.venv/bin/python3
"""Postfix pipe handler — parses MIME from stdin and POSTs to REST ingest."""
import sys, json, requests
from email.parser import BytesParser
from email import policy

def main():
    raw = sys.stdin.buffer.read()
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    sender = msg.get('From', '')
    recipient = msg.get('To', '')
    subject = msg.get('Subject', '')

    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                body = part.get_content()
                break
    else:
        body = msg.get_content()

    payload = {
        'sender': sender,
        'recipient': recipient,
        'subject': subject,
        'body': str(body)[:65536],
        'raw_mime': raw.decode('utf-8', errors='replace')[:262144],
    }

    try:
        r = requests.post('http://127.0.0.1:8080/ingest', json=payload, timeout=10)
        print(f'Ingest: {r.status_code}')
    except Exception as e:
        print(f'Ingest error: {e}', file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
PYEOF

chmod +x /opt/parousia/parousia_pipe.py
```

### 5c. Generate config

```bash
parousia-guard setup --config
```

Edit `/etc/parousia/config.yaml`:
```yaml
domain: yourdomain.com

server:
  rest_host: "127.0.0.1"
  rest_port: 8080
  mcp_host: "0.0.0.0"
  mcp_port: 8081       # stdio transport only — no network port needed

agents:
  # Add your agents here. These are the local parts before @yourdomain.com
  hermes:
    rate_limit_per_hour: 100
  mr-krabs:
    rate_limit_per_hour: 100

spatial:
  enabled: true
  # chromium_path: "/usr/bin/chromium-browser"  # uncomment if not auto-detected
  max_instances: 6
```

### 5d. Set up Postfix aliases

```bash
parousia-guard setup --postfix
```

This adds agent entries to `/etc/aliases` so mail to `hermes@yourdomain.com` is piped to the guard script.

### 5e. Install Playwright + Chromium

```bash
source /opt/parousia/.venv/bin/activate
pip install playwright
playwright install chromium
playwright install-deps chromium
```

### 5f. Create directories

```bash
mkdir -p /var/lib/parousia/{browsers,temporal,dkim}
chown -R parousia:parousia /var/lib/parousia
```

---

## Step 6: Systemd service

Create a service so Parousia starts on boot and restarts on failure:

```bash
cat > /etc/systemd/system/parousia-guard.service << 'EOF'
[Unit]
Description=Parousia Guard — Agentic Mail Server
After=network.target redis-server.service postfix@-.service
Wants=redis-server.service postfix@-.service

[Service]
Type=simple
User=parousia
WorkingDirectory=/opt/parousia
ExecStart=/opt/parousia/.venv/bin/python -m parousia.cli.main serve
Restart=always
RestartSec=5
MemoryMax=6G
Environment="PATH=/opt/parousia/.venv/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now parousia-guard
```

Verify:
```bash
systemctl status parousia-guard
curl http://127.0.0.1:8080/health
# Should return: {"status":"ok","redis":true,...}
```

---

## Step 7: TLS certificates

TLS encrypts SMTP connections between mail servers. Let's Encrypt provides free certificates.

```bash
parousia-guard setup --tls --domain mail.yourdomain.com --email admin@yourdomain.com
```

This runs certbot, writes `/etc/postfix/tls.conf`, includes it in Postfix's `main.cf`, and reloads Postfix.

Verify TLS:
```bash
echo "EHLO test" | openssl s_client -starttls smtp -connect localhost:25 -quiet 2>/dev/null | head -3
# Should show: 250-STARTTLS
```

---

## Step 8: DKIM signing

DKIM cryptographically signs outbound mail so recipients can verify it came from your domain.

```bash
parousia-guard setup --dkim
```

The command outputs DNS records. Add these to your DNS provider:

| Type | Name | Value |
|------|------|-------|
| TXT | `default._domainkey` | `v=DKIM1; k=rsa; p=<public-key>` |

Also update your SPF and add DMARC at your DNS provider:

| Type | Name | Value |
|------|------|-------|
| TXT | `@` | `v=spf1 mx -all` |
| TXT | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:admin@yourdomain.com` |

Verify DKIM:
```bash
parousia-guard validate
```

---

## Step 9: Outbound mail delivery

Parousia's `send_email` tool sends mail through `localhost:25` (Postfix). Postfix then delivers to the recipient's mail server.

### Option A: Direct MX delivery (default)

Postfix looks up the recipient's MX record and delivers directly. This requires **outbound port 25** to be open on your host. If your provider blocks outbound port 25 (AWS, Google Cloud, Azure do by default), you'll need to either:

- Request removal of the outbound port 25 restriction (provider support ticket)
- Use Option B (smarthost relay)

### Option B: Smarthost relay through SES

Configure Postfix to relay outbound mail through Amazon SES on port 587 (always open). Your Postfix sends to SES, SES delivers to the recipient.

```bash
# Install SASL auth
apt install -y libsasl2-modules   # Ubuntu/Debian
# dnf install -y cyrus-sasl-plain  # RHEL/Fedora

# Get SMTP credentials from AWS SES console → SMTP settings

# Configure relay
postconf -e "relayhost = [email-smtp.us-east-1.amazonaws.com]:587"
postconf -e "smtp_sasl_auth_enable = yes"
postconf -e "smtp_sasl_security_options = noanonymous"
postconf -e "smtp_sasl_password_maps = hash:/etc/postfix/sasl_passwd"
postconf -e "smtp_use_tls = yes"
postconf -e "smtp_tls_security_level = encrypt"

# Create credentials file
echo "[email-smtp.us-east-1.amazonaws.com]:587 YOUR_SMTP_USER:YOUR_SMTP_PASS" > /etc/postfix/sasl_passwd
chmod 600 /etc/postfix/sasl_passwd
postmap hash:/etc/postfix/sasl_passwd

systemctl restart postfix@-
```

Full SES setup (domain verification, production access, SPF update): see [docs/hosting.md](hosting.md#amazon-ses).

---

## Step 10: Verify everything

```bash
# 1. Services running
systemctl status postfix@- redis-server parousia-guard

# 2. Parousia health
curl http://127.0.0.1:8080/health

# 3. Postfix listening
ss -tlnp | grep -E ':25|:587'

# 4. DNS
dig MX yourdomain.com +short
dig A mail.yourdomain.com +short
dig -x <your-ip> +short

# 5. Send a test email from the host
python3 -c "
import smtplib
from email.mime.text import MIMEText
msg = MIMEText('Parousia test')
msg['From'] = 'test@yourdomain.com'
msg['To'] = 'you@personal-email.com'
msg['Subject'] = 'Parousia provisioning test'
with smtplib.SMTP('localhost', 25, timeout=10) as s:
    s.sendmail('test@yourdomain.com', ['you@personal-email.com'], msg.as_string())
print('Sent — check your inbox (and spam folder)')
"

# 6. Send a test inbound email
# From your personal email, send a message to agent@yourdomain.com
# Then check:
curl http://127.0.0.1:8080/health
```

---

## Next steps

- Read the [capability guides](capabilities/) for each tool with usage examples
- Read the [architecture doc](architecture.md) to understand component interactions
- Set up [postfwd](hosting.md#postfwd) for additional SMTP-level rate limiting
- Configure [the human-in-the-loop approval queue](capabilities/email.md#approval-queue)

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Postfix won't start | `journalctl -u postfix@- -n 20`. Likely missing `smtpd_relay_restrictions` or `aliases.db`. |
| Postfix shows `active (exited)` | Ubuntu 24.04: you started `postfix.service` (dummy). Use `postfix@-.service`. |
| Inbound mail bounces (550) | `mydestination` includes `$mydomain`. Remove it — pipe transport needs domain in `relay_domains`. |
| Outbound mail stuck in queue | `mailq`. Outbound port 25 blocked by provider. Use SES smarthost relay. |
| `parousia_pipe.py` import error | The pipe runs as user `parousia`. Use `from email.parser import BytesParser` not `email.parser`. |
| SMTP test hangs | Test with Python `smtplib`, not `nc`/`telnet`. SMTP requires `\r\n` line endings. |
| Gmail rejects mail | Missing PTR record. Set reverse DNS for your IP. Also check SPF and DKIM. |
