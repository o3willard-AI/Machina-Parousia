# AWS EC2 Provisioning Guide

How to provision the Parousia mail guard on an AWS EC2 instance.

## Instance Specification

| Setting | Value |
|---------|-------|
| **Instance type** | t3.small (2 vCPU, 2 GB RAM) |
| **AMI** | Ubuntu 24.04 LTS (HVM, SSD) |
| **Root volume** | 20 GB gp3 (general purpose SSD) |
| **Region** | us-east-1 (or closest to your users) |

Cost: ~$15/month base + ~$3/month for 20 GB gp3. Elastic IP is free when attached to a running instance.

## Step 1: Launch EC2 Instance

### Via AWS Console

1. Navigate to **EC2 → Instances → Launch Instance**
2. Name: `parousia-mx`
3. AMI: **Ubuntu Server 24.04 LTS (HVM), SSD Volume Type** (64-bit x86)
4. Instance type: **t3.small**
5. Key pair: Create new (RSA, .pem) or select existing. **Save the .pem file securely** — you'll need it for SSH.
6. Network settings:
   - VPC: default (or your production VPC)
   - Auto-assign public IP: **Enable**
   - Create security group: `parousia-mx-sg`
7. Security group rules:

   | Type | Protocol | Port | Source | Purpose |
   |------|----------|------|--------|---------|
   | SSH | TCP | 22 | Your IP/32 | Admin access |
   | SMTP | TCP | 25 | 0.0.0.0/0 | Inbound mail |
   | HTTP | TCP | 80 | 0.0.0.0/0 | Let's Encrypt (optional) |
   | HTTPS | TCP | 443 | 0.0.0.0/0 | Let's Encrypt (optional) |

   > ⚠️ **Important**: The default security group blocks port 25 outbound. After launch, you must also enable outbound port 25 in the security group for Postfix to deliver mail.

8. Storage: 20 GB gp3, **Delete on termination: Yes** (for dev/staging)
9. Launch

### Via AWS CLI

```bash
# Create security group
aws ec2 create-security-group \
  --group-name parousia-mx-sg \
  --description "Parousia mail server security group"

# Add inbound rules
aws ec2 authorize-security-group-ingress \
  --group-name parousia-mx-sg \
  --protocol tcp --port 22 --cidr $(curl -s ifconfig.me)/32

aws ec2 authorize-security-group-ingress \
  --group-name parousia-mx-sg \
  --protocol tcp --port 25 --cidr 0.0.0.0/0

aws ec2 authorize-security-group-ingress \
  --group-name parousia-mx-sg \
  --protocol tcp --port 80 --cidr 0.0.0.0/0

aws ec2 authorize-security-group-ingress \
  --group-name parousia-mx-sg \
  --protocol tcp --port 443 --cidr 0.0.0.0/0

# Launch instance
aws ec2 run-instances \
  --image-id ami-0c7217cdff26c3b6d \
  --instance-type t3.small \
  --key-name your-key-pair \
  --security-groups parousia-mx-sg \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=parousia-mx}]'
```

> Replace `ami-0c7217cdff26c3b6d` with the latest Ubuntu 24.04 AMI ID for your region. Find it with:
> ```bash
> aws ec2 describe-images \
>   --owners 099720109477 \
>   --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-noble-24.04-amd64-server-*" \
>   --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text
> ```

## Step 2: Elastic IP & Reverse DNS

### Allocate and Assign Elastic IP

```bash
# Allocate
ALLOC_ID=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)

# Get instance ID
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=parousia-mx" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)

# Associate
aws ec2 associate-address \
  --instance-id $INSTANCE_ID \
  --allocation-id $ALLOC_ID

# Show the IP
aws ec2 describe-addresses --allocation-ids $ALLOC_ID \
  --query 'Addresses[0].PublicIp' --output text
```

### Request Reverse DNS (PTR Record)

AWS requires a support ticket to set reverse DNS on Elastic IPs. This is critical — without it, major providers (Gmail, Outlook) will reject your mail.

1. Go to **AWS Support → Create case → Service limit increase**
2. Select: **Elastic IP** → **Reverse DNS**
3. Fill out the form:
   - Elastic IP address: (your allocated IP)
   - Reverse DNS record: `mx.agents.yourdomain.com`
4. Reason: "This IP will be used for an outbound mail server (Postfix). We need a PTR record for deliverability."
5. Submit

> ⏳ AWS typically approves within 24 hours. Until then, your mail may be rate-limited or rejected by recipients.

## Step 3: SSH & Initial Setup

```bash
# SSH into the instance
ssh -i /path/to/your-key.pem ubuntu@<elastic-ip>

# Update system
sudo apt update && sudo apt upgrade -y

# Set hostname
sudo hostnamectl set-hostname mx.agents.yourdomain.com
```

### Configure Firewall (ufw)

```bash
sudo ufw allow 22/tcp     # SSH
sudo ufw allow 25/tcp     # SMTP
sudo ufw allow 80/tcp     # HTTP (optional, for certbot)
sudo ufw allow 443/tcp    # HTTPS (optional, for certbot)
sudo ufw enable
sudo ufw status verbose
```

## Step 4: Port 25 Restriction Removal

AWS blocks outbound port 25 on new accounts by default. You must request removal.

1. Go to **AWS Support → Create case → Service limit increase**
2. Select: **EC2** → **Email sending limit**
3. Request: "Remove port 25 restriction for EC2 instance <instance-id>. This instance will run Postfix for a self-hosted mail server. We have proper SPF/DKIM/DMARC configured and will not send spam."
4. AWS reviews within 1-2 business days.

### Fallback: Cloudflare Email Routing (while waiting)

If port 25 isn't open yet, use Cloudflare Email Routing as a temporary inbound path:

1. In Cloudflare dashboard → **Email → Email Routing**
2. Add your domain, configure DNS records as prompted
3. Create a catch-all rule → forward to your agent's webhook URL
4. This gives you inbound mail while waiting for AWS port 25 approval
5. Once port 25 is open, switch DNS MX records to your EC2 IP

## Step 5: Install Dependencies

```bash
# Core packages
sudo apt install -y postfix redis-server opendkim opendkim-tools python3-pip python3-venv

# Configure Postfix (basic)
sudo postconf -e "myhostname = mx.agents.yourdomain.com"
sudo postconf -e "mydomain = agents.yourdomain.com"
sudo postconf -e "myorigin = \$mydomain"
sudo postconf -e "inet_interfaces = all"
sudo postconf -e "inet_protocols = ipv4"

# Restart Postfix
sudo systemctl restart postfix
sudo systemctl enable postfix

# Redis
sudo systemctl enable redis-server
sudo systemctl start redis-server
```

## Step 6: Clone and Install parousia-guard

```bash
# Clone the repo
git clone https://github.com/o3willard-AI/Parousia.git /opt/parousia
cd /opt/parousia

# Install with pip
sudo pip install --break-system-packages -e .

# Generate config
sudo parousia-guard setup --config

# Setup Postfix aliases
sudo parousia-guard setup --postfix

# Generate DKIM keys and get DNS records
parousia-guard setup --dkim
# → Copy the output to your DNS provider (Hostinger, Cloudflare, etc.)

# Validate installation
parousia-guard validate
```

## Step 7: DNS Configuration (Hostinger or Cloudflare)

Add these records to your domain's DNS:

```
;; MX Record
@  IN  MX  10  mx.agents.yourdomain.com.

;; A Record
mx.agents.yourdomain.com.  IN  A  <elastic-ip>

;; SPF Record
@  IN  TXT  "v=spf1 mx -all"

;; DKIM Record (from parousia-guard setup --dkim output)
default._domainkey  IN  TXT  "v=DKIM1; k=rsa; p=<public-key>"

;; DMARC Record
_dmarc  IN  TXT  "v=DMARC1; p=quarantine; rua=mailto:postmaster@agents.yourdomain.com"
```

## Step 8: Verify Everything

```bash
# Check all services
sudo systemctl status postfix redis-server

# Run parousia validation
parousia-guard validate

# Send a test email
parousia-guard test --to you@yourdomain.com

# Check mail queue
parousia-guard status

# Test from outside: send email to agent@agents.yourdomain.com
# and verify it appears in the guard logs
```

## Step 9: Production Hardening

```bash
# Enable automatic security updates
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

# Configure log rotation
sudo tee /etc/logrotate.d/parousia <<EOF
/var/log/mail.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    postrotate
        systemctl reload rsyslog > /dev/null 2>&1 || true
    endscript
}
EOF

# Set up a cron job to monitor parousia health
sudo tee /etc/cron.d/parousia-health <<EOF
*/5 * * * * root /usr/local/bin/parousia-guard validate > /dev/null 2>&1 || echo "Parousia health check failed" | mail -s "ALERT" postmaster@agents.yourdomain.com
EOF
```

## Quick Reference

| What | Command |
|------|---------|
| Check health | `parousia-guard validate` |
| Show status | `parousia-guard status` |
| Send test email | `parousia-guard test --to user@example.com` |
| View mail queue | `mailq` |
| Postfix logs | `tail -f /var/log/mail.log` |
| Restart Postfix | `sudo systemctl restart postfix` |
| Regenerate DKIM | `parousia-guard setup --dkim --rotate` |
