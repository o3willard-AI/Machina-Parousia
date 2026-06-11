# Hosting Notes

Provider-specific guidance for running Parousia. For the generic provisioning walkthrough, see [getting-started.md](getting-started.md).

---

## AWS

### Instance sizing

Parousia runs comfortably on modest hardware:

| Instance | vCPU | RAM | Cost (approx) | Suitable for |
|----------|------|-----|---------------|-------------|
| t3.small | 2 | 2 GB | ~$15/mo | Light use, 1-2 agents |
| t3.medium | 2 | 4 GB | ~$30/mo | Standard, 3-6 agents |
| m7i-flex.large | 2 | 8 GB | ~$55/mo | Full spatial with 6 Chromium instances |

The reference deployment uses **m7i-flex.large** with 6-agent spatial browsing. Chromium instances consume ~500 MB each at idle.

### Port 25 outbound restriction

AWS blocks outbound port 25 on all new accounts by default. This means Postfix cannot deliver mail directly to recipient MX servers. You have two options:

#### Option A: Remove the restriction (1-2 day wait)

1. AWS Console → Support → Create case → Service limit increase
2. Limit type: **EC2 → Email sending limit**
3. Provide: instance ID, use case (self-hosted mail server), SPF/DKIM/DMARC status, volume estimates
4. AWS typically approves within 1-2 business days

#### Option B: Use SES as smarthost relay (hours)

Configure Postfix to relay through Amazon SES on port 587. Full walkthrough:

1. **Verify domain in SES**: Console → SES → Verified identities → Create identity → Domain → `yourdomain.com`
2. **Add DKIM CNAMEs** to your DNS (3 records provided by SES)
3. **Get SMTP credentials**: SES → SMTP settings → Create SMTP credentials
4. **Request production access**: SES → Account dashboard → Request production access. Use case: "Transactional email for AI agent platform. < 100 msg/day."
5. **Configure Postfix** as relay:

```bash
postconf -e "relayhost = [email-smtp.us-east-1.amazonaws.com]:587"
postconf -e "smtp_sasl_auth_enable = yes"
postconf -e "smtp_sasl_security_options = noanonymous"
postconf -e "smtp_sasl_password_maps = hash:/etc/postfix/sasl_passwd"
postconf -e "smtp_use_tls = yes"
postconf -e "smtp_tls_security_level = encrypt"

echo "[email-smtp.us-east-1.amazonaws.com]:587 YOUR_USER:YOUR_PASS" > /etc/postfix/sasl_passwd
chmod 600 /etc/postfix/sasl_passwd
postmap hash:/etc/postfix/sasl_passwd
systemctl restart postfix@-
```

6. **Update SPF**: `v=spf1 mx include:amazonses.com -all`

SES costs $0.10 per 1,000 emails. First 62,000/month are free when sending from EC2.

### Security groups

Your EC2 security group needs:

| Direction | Port | Source | Purpose |
|-----------|------|--------|---------|
| Inbound | 22 | Your IP | SSH |
| Inbound | 25 | 0.0.0.0/0 | SMTP (inbound mail) |
| Inbound | 80 | 0.0.0.0/0 | HTTP (Let's Encrypt) |
| Inbound | 443 | 0.0.0.0/0 | HTTPS |
| Inbound | 8080 | Your IP | REST API |
| Outbound | 25 | 0.0.0.0/0 | Direct MX delivery (if not using SES) |
| Outbound | 587 | 0.0.0.0/0 | SES relay (if using SES) |
| Outbound | 443 | 0.0.0.0/0 | HTTPS (package installs, API calls) |

### Elastic IP + reverse DNS

- Allocate an Elastic IP and attach it to your instance
- File a Support Center case for reverse DNS: set PTR to `mail.yourdomain.com`
- Without PTR, Gmail and Outlook will likely reject your mail

---

## Hetzner

### Firewall

Hetzner's cloud firewall sits outside the VM. Configure in the Hetzner Cloud Console:

| Direction | Port | Source |
|-----------|------|--------|
| Inbound | 22, 25, 80, 443, 8080 | 0.0.0.0/0 |
| Outbound | All | 0.0.0.0/0 |

### Reverse DNS

Set in the Robot admin panel or Cloud Console → server → Networking → Reverse DNS entry.

### Port 25

Hetzner does **not** block outbound port 25 by default on dedicated/VPS servers. Cloud servers may have it restricted on new accounts — contact support if blocked.

---

## DigitalOcean

### Firewall

DigitalOcean cloud firewall:

| Direction | Port | Source |
|-----------|------|--------|
| Inbound | 22, 25, 80, 443, 8080 | All IPv4/IPv6 |

### Reverse DNS

Set in the control panel → droplet → Networking → Edit reverse DNS.

### Port 25

DigitalOcean blocks outbound port 25 on new accounts. Open a support ticket to request removal. Provide your use case (self-hosted mail server with SPF/DKIM/DMARC).

---

## Linode

### Firewall

Linode cloud firewall:

| Direction | Port | Source |
|-----------|------|--------|
| Inbound | 22, 25, 80, 443, 8080 | 0.0.0.0/0 |

### Reverse DNS

Set in Cloud Manager → Network → IP Addresses → Edit RDNS.

### Port 25

Linode blocks outbound port 25 on new accounts. Open a support ticket with your use case to request removal.

---

## Google Cloud

GCP blocks outbound port 25 permanently on Compute Engine. You **must** use a third-party relay (SES, SendGrid, Mailgun) for outbound mail. See the smarthost relay configuration above.

---

## Common provider checklist

| Provider | Outbound 25 | PTR setup | Firewall notes |
|----------|------------|-----------|----------------|
| AWS | Blocked → request removal or use SES | Support case | Security groups, separate from OS firewall |
| Hetzner | Usually open | Robot/Cloud Console | Cloud firewall outside VM |
| DigitalOcean | Blocked → support ticket | Control panel | Cloud firewall |
| Linode | Blocked → support ticket | Cloud Manager | Cloud firewall |
| Google Cloud | Permanently blocked | Cloud Console → External IP | Use smarthost relay |
| Azure | Blocked → support ticket | Portal → Public IP | NSG rules |
| Vultr | Open by default | Control panel | No external firewall |
| OVH | Open by default | Control panel | No external firewall |
