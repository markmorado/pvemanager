# 🚀 Server Panel Deployment Guide v1.0

Comprehensive guide for deploying Server Panel on a VPS with optional NGINX and SSL support.

## 📋 Prerequisites

### System Requirements
- **OS**: Ubuntu 20.04+ / Debian 11+ / CentOS 8+
- **RAM**: Minimum 2GB (4GB recommended)
- **Disk**: Minimum 10GB free space
- **CPU**: 2+ cores recommended

### Software Requirements
- Docker 24.0+
- Docker Compose 2.20+

### Installation of Docker (if not installed)

```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt-get update
sudo apt-get install docker-compose-plugin

# Verify installation
docker --version
docker compose version
```

## 🎯 Deployment Options

The deployment script supports three modes:

### 1. **Standalone Mode** (Development/Testing)
- Direct access on port 8000
- No NGINX, no SSL
- Best for: Local testing, development

### 2. **Production with NGINX** (HTTP only)
- NGINX reverse proxy
- Port 80 (HTTP)
- Rate limiting, caching
- Best for: Internal networks, no public internet exposure

### 3. **Production with NGINX and SSL** (HTTPS)
- NGINX reverse proxy
- Ports 80 (HTTP → HTTPS redirect) and 443 (HTTPS)
- Automatic SSL certificates from Let's Encrypt
- Auto-renewal of certificates
- Best for: Public production deployment

---

## 🚀 Quick Start

### Step 1: Clone Repository

```bash
git clone <repository-url>
cd server_panel
```

### Step 2: Make Deploy Script Executable

```bash
chmod +x deploy.sh
```

### Step 3: Run Deployment Script

```bash
sudo ./deploy.sh
```

The script will guide you through:
1. Checking requirements
2. Creating `.env` file with secure passwords
3. Selecting deployment mode
4. Configuring domain (for NGINX modes)
5. Obtaining SSL certificates (for SSL mode)
6. Starting services

---

## 📖 Detailed Deployment Instructions

### Option 1: Standalone Deployment

**When to use**: Development, testing, or when you have your own reverse proxy.

```bash
sudo ./deploy.sh
# Select option 1
```

**Access**: `http://your-server-ip:8000`

**Manual deployment**:
```bash
# Create .env file
cp .env.example .env
nano .env  # Edit configuration

# Start services
docker compose up -d

# View logs
docker compose logs -f
```

---

### Option 2: Production with NGINX (HTTP only)

**When to use**: Internal network, no SSL needed, or SSL terminated elsewhere.

```bash
sudo ./deploy.sh
# Select option 2
# Enter domain: panel.example.com (or server IP)
```

**Access**: `http://panel.example.com`

**What happens**:
1. Creates NGINX configuration without SSL
2. Sets up reverse proxy to FastAPI app
3. Configures rate limiting and caching
4. Starts: NGINX + FastAPI + PostgreSQL

**Manual deployment**:
```bash
# Setup NGINX config
mkdir -p nginx/conf.d
cp nginx/conf.d/serverpanel-nossl.conf.template nginx/conf.d/serverpanel.conf
sed -i 's/DOMAIN_NAME/your-domain.com/g' nginx/conf.d/serverpanel.conf

# Start services
docker compose -f compose.yml -f compose.prod.yml up -d
```

---

### Option 3: Production with NGINX and SSL (HTTPS)

**When to use**: Public production deployment with proper security.

**Requirements**:
- Valid domain name pointed to your server IP
- Port 80 and 443 accessible from internet
- Valid email address for Let's Encrypt

```bash
sudo ./deploy.sh
# Select option 3
# Enter domain: panel.example.com
# Enter email: admin@example.com
```

**Access**: `https://panel.example.com`

**What happens**:
1. Creates NGINX configuration with SSL
2. Obtains Let's Encrypt certificate via Certbot
3. Configures automatic certificate renewal
4. Sets up HTTPS with strong security headers
5. Starts: NGINX + Certbot + FastAPI + PostgreSQL

**Manual deployment**:
```bash
# Setup NGINX config with SSL
mkdir -p nginx/conf.d nginx/certbot/conf nginx/certbot/www
cp nginx/conf.d/serverpanel.conf.template nginx/conf.d/serverpanel.conf
sed -i 's/DOMAIN_NAME/your-domain.com/g' nginx/conf.d/serverpanel.conf

# Start NGINX first (for Let's Encrypt challenge)
docker compose -f compose.yml -f compose.prod.yml up -d nginx

# Obtain SSL certificate
docker compose -f compose.yml -f compose.prod.yml run --rm certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    --email admin@example.com \
    --agree-tos \
    --no-eff-email \
    -d your-domain.com

# Start all services
docker compose -f compose.yml -f compose.prod.yml up -d
```

---

## 🔧 Configuration

### Environment Variables

Edit `.env` file:

```bash
# Database password
POSTGRES_PASSWORD=your_secure_password_here

# Timezone
TZ=Asia/Tashkent

# For production with NGINX (optional)
DOMAIN=panel.example.com
EMAIL=admin@example.com
```

### NGINX Configuration

NGINX configs are located in `nginx/` directory:

```
nginx/
├── nginx.conf                              # Main NGINX config
├── conf.d/
│   ├── serverpanel.conf                   # Active config (generated)
│   ├── serverpanel.conf.template          # SSL template
│   └── serverpanel-nossl.conf.template    # Non-SSL template
├── ssl/                                    # Custom SSL certificates (if any)
└── certbot/
    ├── conf/                               # Let's Encrypt certificates
    └── www/                                # ACME challenge files
```

**Customizing NGINX**:
1. Edit `nginx/nginx.conf` for global settings
2. Edit `nginx/conf.d/serverpanel.conf` for site-specific settings
3. Restart NGINX: `docker compose restart nginx`

---

## 🔐 SSL Certificate Management

### Automatic Renewal

Certbot container automatically renews certificates every 12 hours.

**Check renewal**:
```bash
docker compose exec certbot certbot renew --dry-run
```

### Manual Renewal

```bash
docker compose run --rm certbot renew
docker compose restart nginx
```

### Add More Domains

```bash
docker compose run --rm certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    --email admin@example.com \
    --agree-tos \
    -d panel.example.com \
    -d www.panel.example.com
```

### Use Custom SSL Certificate

If you have your own SSL certificate (from a commercial CA or internal PKI), follow these steps:

#### Step 1: Prepare Certificate Files

You will need:
- **Certificate file** (`your-domain.crt`) — Your SSL certificate
- **Private key** (`your-domain.key`) — Private key (keep secure!)
- **CA Bundle** (optional) — Intermediate certificates if required

```bash
# Create SSL directory
mkdir -p nginx/ssl

# Copy your certificate files
cp /path/to/your-domain.crt nginx/ssl/
cp /path/to/your-domain.key nginx/ssl/

# If you have a CA bundle, concatenate it with your certificate
cat /path/to/your-domain.crt /path/to/ca-bundle.crt > nginx/ssl/your-domain-fullchain.crt
```

#### Step 2: Set Correct Permissions

```bash
# Secure the private key
chmod 600 nginx/ssl/your-domain.key
chmod 644 nginx/ssl/your-domain.crt
```

#### Step 3: Update NGINX Configuration

Edit `nginx/conf.d/serverpanel.conf`:

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    # Custom SSL certificate
    ssl_certificate /etc/nginx/ssl/your-domain.crt;
    ssl_certificate_key /etc/nginx/ssl/your-domain.key;
    
    # If using fullchain with CA bundle:
    # ssl_certificate /etc/nginx/ssl/your-domain-fullchain.crt;
    
    # Recommended SSL settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384';
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    
    # HSTS (optional, recommended for production)
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # ... rest of configuration
}
```

#### Step 4: Restart NGINX

```bash
docker compose restart nginx
```

#### Step 5: Verify Certificate

```bash
# Check certificate is loaded correctly
docker compose exec nginx nginx -t

# Test SSL from command line
openssl s_client -connect your-domain.com:443 -servername your-domain.com
```

#### Using Wildcard Certificates

For wildcard certificates (`*.example.com`):

```bash
# Place wildcard certificate
cp wildcard.example.com.crt nginx/ssl/
cp wildcard.example.com.key nginx/ssl/

# Update NGINX config
ssl_certificate /etc/nginx/ssl/wildcard.example.com.crt;
ssl_certificate_key /etc/nginx/ssl/wildcard.example.com.key;
```

#### Using PFX/P12 Format (Windows/IIS certificates)

If you have a PFX file, convert it first:

```bash
# Extract certificate
openssl pkcs12 -in certificate.pfx -clcerts -nokeys -out nginx/ssl/your-domain.crt

# Extract private key
openssl pkcs12 -in certificate.pfx -nocerts -nodes -out nginx/ssl/your-domain.key
```

#### Certificate Renewal Reminder

Unlike Let's Encrypt, custom certificates don't auto-renew. Set a calendar reminder to renew before expiration:

```bash
# Check certificate expiration date
openssl x509 -enddate -noout -in nginx/ssl/your-domain.crt
```

---

## 📊 Managing Services

### View Status

```bash
docker compose ps
```

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f app
docker compose logs -f nginx
docker compose logs -f db
```

### Restart Services

```bash
# All services
docker compose restart

# Specific service
docker compose restart app
docker compose restart nginx
```

### Stop Services

```bash
docker compose down
```

### Update and Restart

```bash
# Pull latest changes
git pull

# Rebuild and restart
docker compose down
docker compose up -d --build
```

---

## 🔍 Troubleshooting

### Port Already in Use

```bash
# Check what's using port 80/443
sudo lsof -i :80
sudo lsof -i :443

# Stop conflicting service
sudo systemctl stop apache2  # or nginx
```

### SSL Certificate Failed

**Issue**: Let's Encrypt challenge failed

**Solution**:
1. Ensure domain points to your server IP
2. Ensure ports 80 and 443 are accessible
3. Check firewall rules:
```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw reload
```

### Database Connection Error

```bash
# Check database logs
docker compose logs db

# Recreate database
docker compose down -v
docker compose up -d
```

### NGINX Configuration Error

```bash
# Test configuration
docker compose exec nginx nginx -t

# View error details
docker compose logs nginx
```

---

## 🔒 Security Best Practices

### 1. Change Default Passwords

Edit `.env` and change:
- `POSTGRES_PASSWORD`
- Admin password in application

### 2. Configure Firewall

```bash
sudo ufw enable
sudo ufw allow 22/tcp   # SSH
sudo ufw allow 80/tcp   # HTTP
sudo ufw allow 443/tcp  # HTTPS
sudo ufw reload
```

### 3. Enable Fail2Ban (Optional)

```bash
sudo apt-get install fail2ban
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

### 4. Regular Updates

```bash
# Update system
sudo apt-get update && sudo apt-get upgrade

# Update Docker images
docker compose pull
docker compose up -d
```

---

## 📈 Performance Optimization

### 1. Increase Worker Processes

Edit `compose.yml`:
```yaml
environment:
  - UVICORN_WORKERS=4  # Increase from 1
```

### 2. Adjust PostgreSQL Settings

Edit `compose.yml` PostgreSQL command section for your hardware.

### 3. Enable NGINX Caching

Already configured in production compose file.

---

## 🔄 Backup and Restore

### Backup Database

```bash
docker compose exec db pg_dump -U serverpanel serverpanel > backup_$(date +%Y%m%d).sql
```

### Restore Database

```bash
docker compose exec -T db psql -U serverpanel serverpanel < backup_20251127.sql
```

### Backup All Data

```bash
# Create backup directory
mkdir -p backups

# Backup database volume
docker run --rm \
  -v serverpanel_db_data:/data \
  -v $(pwd)/backups:/backup \
  alpine tar czf /backup/db_data_$(date +%Y%m%d).tar.gz /data

# Backup SSL certificates
tar czf backups/ssl_$(date +%Y%m%d).tar.gz nginx/certbot/conf
```

---

## 📞 Support

- **Documentation**: See other .md files in repository
- **Issues**: Open GitHub issue
- **Logs**: Always include logs when reporting issues

---

## 📝 Quick Reference

### Deployment Modes

| Mode | Ports | SSL | Use Case |
|------|-------|-----|----------|
| Standalone | 8000 | ❌ | Development |
| NGINX HTTP | 80 | ❌ | Internal network |
| NGINX HTTPS | 80, 443 | ✅ | Production |

### Common Commands

```bash
# Deploy
sudo ./deploy.sh

# View logs
docker compose logs -f

# Restart
docker compose restart

# Stop
docker compose down

# Update
git pull && docker compose up -d --build

# Backup
docker compose exec db pg_dump -U serverpanel serverpanel > backup.sql
```

---

**Version**: 1.9.0  
**Last Updated**: 2025-12-13
