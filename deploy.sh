#!/bin/bash

# PVEmanager Deployment Script
# Supports deployment with or without NGINX and SSL

set -e
set -o pipefail

# Save script path and original args for potential re-exec after docker group change
DEPLOY_SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
DEPLOY_QUOTED_ARGS="$(printf '%q ' "$@")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Detect OS
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
        OS_VERSION=$VERSION_ID
    elif [ -f /etc/redhat-release ]; then
        OS="centos"
    elif [ "$(uname)" == "Darwin" ]; then
        OS="macos"
    else
        OS="unknown"
    fi
    echo $OS
}

# Install Docker
install_docker() {
    local os=$1
    print_info "Installing Docker..."
    
    case $os in
        ubuntu|debian)
            # Remove old versions
            sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true
            
            # Install prerequisites
            sudo apt-get update
            sudo apt-get install -y \
                ca-certificates \
                curl \
                gnupg \
                lsb-release
            
            # Add Docker GPG key
            sudo mkdir -p /etc/apt/keyrings
            curl -fsSL https://download.docker.com/linux/$os/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
            
            # Add Docker repository
            echo \
                "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$os \
                $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
            
            # Install Docker
            sudo apt-get update
            sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            ;;
        centos|rhel|fedora)
            # Remove old versions
            sudo yum remove -y docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate docker-logrotate docker-engine 2>/dev/null || true
            
            # Install prerequisites
            sudo yum install -y yum-utils
            
            # Add Docker repository
            sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
            
            # Install Docker
            sudo yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            
            # Start Docker
            sudo systemctl start docker
            sudo systemctl enable docker
            ;;
        macos)
            print_error "Please install Docker Desktop for Mac from https://www.docker.com/products/docker-desktop"
            exit 1
            ;;
        *)
            print_error "Unsupported OS. Please install Docker manually."
            print_info "Visit: https://docs.docker.com/engine/install/"
            exit 1
            ;;
    esac
    
    # Add current user to docker group (activated via re-exec in check_requirements)
    if [ "$os" != "macos" ] && [ "$(id -u)" -ne 0 ]; then
        sudo usermod -aG docker "$USER"
    fi

    print_success "Docker installed successfully"
}

# Install other dependencies
install_dependencies() {
    local os=$1
    print_info "Installing additional dependencies..."
    
    case $os in
        ubuntu|debian)
            sudo apt-get update
            sudo apt-get install -y \
                git \
                curl \
                wget \
                openssl \
                jq
            ;;
        centos|rhel|fedora)
            sudo yum install -y \
                git \
                curl \
                wget \
                openssl \
                jq
            ;;
        macos)
            if command -v brew &> /dev/null; then
                brew install git curl wget openssl jq
            else
                print_warning "Homebrew not found. Installing Homebrew..."
                /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
                brew install git curl wget openssl jq
            fi
            ;;
    esac
    
    print_success "Dependencies installed"
}

check_requirements() {
    print_info "Checking requirements..."
    
    local os=$(detect_os)
    print_info "Detected OS: $os"
    
    local need_install=false
    
    # Check Docker
    if ! command -v docker &> /dev/null; then
        print_warning "Docker is not installed"
        need_install=true
    else
        print_success "Docker: $(docker --version | cut -d' ' -f3 | tr -d ',')"
    fi
    
    # Check Docker Compose
    if ! docker compose version &> /dev/null; then
        print_warning "Docker Compose plugin is not installed"
        need_install=true
    else
        print_success "Docker Compose: $(docker compose version --short)"
    fi
    
    # Check other tools
    local missing_tools=""
    for tool in git curl openssl; do
        if ! command -v $tool &> /dev/null; then
            missing_tools="$missing_tools $tool"
        fi
    done
    
    if [ -n "$missing_tools" ]; then
        print_warning "Missing tools:$missing_tools"
        need_install=true
    fi
    
    # Install if needed
    if [ "$need_install" = true ]; then
        echo ""
        print_info "Some dependencies are missing."
        read -p "Do you want to install them automatically? (y/n): " INSTALL_CHOICE
        
        if [ "$INSTALL_CHOICE" = "y" ] || [ "$INSTALL_CHOICE" = "Y" ]; then
            # Install dependencies
            install_dependencies "$os"
            
            # Install Docker if needed
            if ! command -v docker &> /dev/null; then
                install_docker "$os"
            fi
            
            # Verify installation
            if ! command -v docker &> /dev/null; then
                print_error "Docker installation failed. Please install manually."
                exit 1
            fi
            
            if ! docker compose version &> /dev/null; then
                print_error "Docker Compose installation failed. Please install manually."
                exit 1
            fi
            
            print_success "All dependencies installed successfully"
        else
            print_error "Please install missing dependencies and try again."
            echo ""
            echo "Install Docker: https://docs.docker.com/engine/install/"
            exit 1
        fi
    else
        print_success "All requirements satisfied"
    fi
    
    # Check if Docker daemon is running
    if ! docker info &> /dev/null; then
        print_warning "Docker daemon is not running. Starting..."
        sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null || true
        sleep 2

        if ! docker info &> /dev/null; then
            # Daemon may be running but the current session lacks the docker group.
            # `usermod -aG docker` was applied above but only takes effect in a new
            # login session.  Re-exec the entire script under `sg docker` so the
            # kernel picks up the supplementary GID without a logout/login cycle.
            if sudo docker info &> /dev/null && \
               getent group docker | grep -qw "$(id -un)" && \
               [ -z "$_DEPLOY_REEXEC" ]; then
                print_info "Activating docker group and restarting deploy..."
                exec sg docker -c "_DEPLOY_REEXEC=1 bash \"$DEPLOY_SELF\" $DEPLOY_QUOTED_ARGS"
            fi
            print_error "Cannot connect to Docker daemon. Please start Docker and try again."
            exit 1
        fi
    fi

    print_success "Docker daemon is running"
}

create_env_file() {
    # Generate random passwords and keys
    local RANDOM_DB_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-25)
    local RANDOM_SECRET_KEY=$(openssl rand -hex 32)
    # Fernet key: url-safe base64 of 32 random bytes (same format as
    # cryptography.fernet.Fernet.generate_key())
    local RANDOM_FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')

    # Ensure required directories exist
    mkdir -p logs nginx/conf.d nginx/ssl nginx/certbot/conf nginx/certbot/www
    
    # Create init.sql if it doesn't exist
    if [ ! -f init.sql ]; then
        print_info "Creating init.sql file..."
        cat > init.sql << 'EOF'
-- PVEmanager Database Initialization
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
GRANT ALL PRIVILEGES ON DATABASE pvemanager TO pvemanager;
EOF
        print_success "init.sql file created"
    fi
    
    # Check if .env.example exists
    if [ ! -f .env.example ]; then
        print_error ".env.example file not found!"
        print_info "Creating default .env.example..."
        cat > .env.example << 'EOF'
# Database Configuration
POSTGRES_PASSWORD=pvemanager_secure_password

# Timezone
TZ=Asia/Tashkent
TIMEZONE=Asia/Tashkent
EOF
    fi
    
    # Check if backend/.env.example exists
    if [ ! -f backend/.env.example ]; then
        print_error "backend/.env.example file not found!"
        print_info "Creating default backend/.env.example..."
        cat > backend/.env.example << 'EOF'
# Application
PANEL_NAME=PVEmanager
DEBUG=false
LOG_LEVEL=INFO

# Database
DB_HOST=db
DB_PORT=5432
DB_USER=pvemanager
DB_PASSWORD=pvemanager_secure_password
DB_NAME=pvemanager
DATABASE_URL=postgresql://pvemanager:pvemanager_secure_password@db:5432/pvemanager

# JWT
SECRET_KEY=your-very-secure-secret-key-change-this-in-production-minimum-32-chars
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# SSH
SSH_TIMEOUT=10
DEFAULT_SSH_USER=root
DEFAULT_SSH_PORT=22

# Security
CORS_ORIGINS=*
ALLOWED_HOSTS=localhost,127.0.0.1
EOF
    fi
    
    # Create root .env file
    if [ ! -f .env ]; then
        print_info "Creating .env file..."
        cp .env.example .env
        
        # Replace default password with random one
        sed -i "s/pvemanager_secure_password/${RANDOM_DB_PASSWORD}/g" .env
        
        print_success ".env file created"
    else
        print_info ".env file already exists"
        # Read existing password for backend .env
        RANDOM_DB_PASSWORD=$(grep "^POSTGRES_PASSWORD=" .env | cut -d'=' -f2)
    fi
    
    # Create backend/.env file
    if [ ! -f backend/.env ]; then
        print_info "Creating backend/.env file..."
        cp backend/.env.example backend/.env
        
        # Replace passwords and keys
        sed -i "s/pvemanager_secure_password/${RANDOM_DB_PASSWORD}/g" backend/.env
        sed -i "s/your-very-secure-secret-key-change-this-in-production-minimum-32-chars/${RANDOM_SECRET_KEY}/g" backend/.env
        # Fill the (empty) FERNET_KEY so sensitive fields are encrypted at rest
        sed -i "s|^FERNET_KEY=.*|FERNET_KEY=${RANDOM_FERNET_KEY}|" backend/.env

        print_success "backend/.env file created"
    else
        print_info "backend/.env file already exists"
    fi
    
    echo ""
    print_success "Environment files configured successfully"
    print_info "Note: SMTP and Telegram settings can be configured in the panel's Settings -> Notifications tab"
}

setup_nginx_config() {
    local domain=$1
    local use_ssl=$2
    
    print_info "Setting up NGINX configuration..."
    
    # Create directories
    mkdir -p nginx/conf.d nginx/ssl nginx/certbot/conf nginx/certbot/www
    
    if [ "$use_ssl" = true ]; then
        # Copy SSL template
        cp nginx/conf.d/pvemanager.conf.template nginx/conf.d/pvemanager.conf
        print_info "Using SSL configuration"
    else
        # Copy non-SSL template
        cp nginx/conf.d/pvemanager-nossl.conf.template nginx/conf.d/pvemanager.conf
        print_info "Using non-SSL configuration"
    fi
    
    # Replace domain name
    sed -i "s/DOMAIN_NAME/${domain}/g" nginx/conf.d/pvemanager.conf

    # Public URL for links in e-mails (password reset). Without it the backend
    # falls back to request headers, which an attacker controls.
    set_panel_url "$domain" "$use_ssl"
    
    print_success "NGINX configuration created for domain: ${domain}"
}

# Write PANEL_URL=<scheme>://<domain> into backend/.env (replace or append).
set_panel_url() {
    local domain=$1
    local use_ssl=$2
    local scheme="http"
    [ "$use_ssl" = true ] && scheme="https"
    [ -f backend/.env ] || return 0
    if grep -q '^PANEL_URL=' backend/.env; then
        sed -i "s|^PANEL_URL=.*|PANEL_URL=${scheme}://${domain}|" backend/.env
    else
        printf '\nPANEL_URL=%s://%s\n' "$scheme" "$domain" >> backend/.env
    fi
    print_info "PANEL_URL set to ${scheme}://${domain}"
}

# Check whether a usable certificate for the domain is already on disk.
#
# The check has to run as root inside a container: certbot creates
# /etc/letsencrypt/{live,archive,accounts} owned by root with mode 0700, so a
# plain `[ -d nginx/certbot/conf/live/$domain ]` from the (unprivileged) deploy
# user always fails with EACCES — even when the certificate is perfectly valid.
certificate_exists() {
    local domain=$1

    docker run --rm \
        -v "$(pwd)/nginx/certbot/conf:/etc/letsencrypt" \
        --entrypoint sh \
        certbot/certbot -c \
        "[ -s /etc/letsencrypt/live/${domain}/fullchain.pem ] && [ -s /etc/letsencrypt/live/${domain}/privkey.pem ]" \
        >/dev/null 2>&1
}

obtain_ssl_certificate() {
    local domain=$1
    local email=$2

    print_info "Obtaining SSL certificate for ${domain}..."

    # Create certbot directories. Only the ACME webroot needs relaxing — the
    # rest belongs to certbot (root, 0700 on purpose: it holds private keys),
    # and chmod -R here would only spam errors it cannot act on anyway.
    mkdir -p nginx/certbot/conf nginx/certbot/www
    chmod 755 nginx/certbot nginx/certbot/www 2>/dev/null || true

    # Wait for nginx to be fully ready
    print_info "Waiting for nginx to be ready..."
    local max_wait=30
    local waited=0
    while [ $waited -lt $max_wait ]; do
        if curl -s -o /dev/null -w "%{http_code}" "http://localhost/.well-known/acme-challenge/test" 2>/dev/null | grep -q "404\|200"; then
            print_success "NGINX is ready for certificate challenge"
            break
        fi
        sleep 2
        waited=$((waited + 2))
    done
    
    if [ $waited -ge $max_wait ]; then
        print_warning "NGINX may not be fully ready, continuing anyway..."
    fi
    
    # Request certificate via the webroot nginx is already serving.
    # --keep-until-expiring makes an existing, still-valid certificate a plain
    # success instead of an ambiguous "no action taken" result.
    print_info "Requesting SSL certificate from Let's Encrypt..."
    docker run --rm \
        -v "$(pwd)/nginx/certbot/conf:/etc/letsencrypt" \
        -v "$(pwd)/nginx/certbot/www:/var/www/certbot" \
        certbot/certbot certonly \
        --webroot \
        --webroot-path=/var/www/certbot \
        --email "${email}" \
        --agree-tos \
        --no-eff-email \
        --non-interactive \
        --keep-until-expiring \
        -d "${domain}"

    local result=$?

    # The certificate on disk is the source of truth, not certbot's exit code:
    # a certificate that is present and not yet due for renewal is a success
    # even on the runs where certbot reports it had nothing to do.
    if certificate_exists "$domain"; then
        if [ $result -ne 0 ]; then
            print_warning "certbot exited with code ${result}, but a valid certificate for ${domain} is present"
        fi
        print_success "SSL certificate is available for ${domain}"
        return 0
    fi

    print_error "Failed to obtain SSL certificate (certbot exit code: ${result})"
    print_warning "Check that ${domain} resolves to this server and that port 80 is reachable"
    print_warning "Continuing with HTTP only..."
    return 1
}

# Resilient Docker build: try with cache + --pull first; on failure retry once
# without cache. Returns non-zero only if both attempts fail.
# Usage: build_images_resilient "-f compose.yml [-f compose.prod.yml]"
build_images_resilient() {
    local compose_args="$1"
    print_info "Building Docker images (parallel, with pull)..."
    if DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1 \
        docker compose $compose_args build --parallel --pull; then
        return 0
    fi

    print_warning "Build failed. Retrying without cache (this can take a few minutes)..."
    # Free space from any half-baked layers from the failed build
    docker builder prune -f >/dev/null 2>&1 || true
    if DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1 \
        docker compose $compose_args build --parallel --pull --no-cache; then
        print_success "Rebuild without cache succeeded"
        return 0
    fi

    print_error "Docker build failed twice. Showing recent build context:"
    docker compose $compose_args config --services || true
    return 1
}

# Detect a stale Postgres data volume whose stored password no longer matches
# the freshly generated one in .env. This is the most common reason for
# repeated deploys to fail with backend stuck in a restart loop.
check_stale_db_volume() {
    local volume_name
    # Compose v2 prefixes volumes with the project (folder) name.
    # The volume key in compose.yml is `db_data`, so the real name is
    # <project>_db_data (NOT _postgres_data).
    volume_name="$(basename "$(pwd)")_db_data"

    if ! docker volume inspect "$volume_name" >/dev/null 2>&1; then
        return 0  # no volume yet, fresh install
    fi

    local env_password
    env_password=$(grep "^POSTGRES_PASSWORD=" .env 2>/dev/null | cut -d'=' -f2)
    [ -z "$env_password" ] && return 0

    print_warning "Existing Postgres volume detected: $volume_name"
    print_warning "If it was initialized with a different password, the backend will fail to connect."
    if [ "$RESET_DATA" = "true" ]; then
        print_warning "--reset-data given: removing old Postgres volume..."
        docker compose -f compose.yml down -v 2>/dev/null || true
        docker volume rm "$volume_name" 2>/dev/null || true
        print_success "Old volume removed"
    else
        print_info "If deploy fails on the 'app' / backend container, re-run with: ./deploy.sh --reset-data"
    fi
}

# Wait for the nginx container to settle into a running state and accept its
# configuration. A bad ssl_certificate path makes nginx exit at startup, so
# "up -d" returning 0 is not on its own proof that the site is being served.
verify_nginx_running() {
    local max_wait=60
    local waited=0

    while [ $waited -lt $max_wait ]; do
        # Use --format to get the raw container state ("running") instead of
        # the human-readable STATUS column ("Up Xs (health: starting)") which
        # does NOT contain the word "running" and only shows "healthy" after
        # the first healthcheck interval (30s) — longer than the old 20s wait.
        local state
        state=$(docker compose -f compose.yml -f compose.prod.yml ps nginx --format '{{.State}}' 2>/dev/null)
        if [ "$state" = "running" ]; then
            if docker compose -f compose.yml -f compose.prod.yml exec -T nginx nginx -t >/dev/null 2>&1; then
                return 0
            fi
        fi
        sleep 2
        waited=$((waited + 2))
    done

    return 1
}

deploy_with_nginx() {
    local domain=$1
    local use_ssl=$2
    local email=$3
    local ssl_success=false
    
    print_info "Deploying with NGINX..."
    
    # Clean up any orphaned containers and networks first
    print_info "Cleaning up previous deployment..."
    docker compose -f compose.yml -f compose.prod.yml down --remove-orphans 2>/dev/null || true
    docker network prune -f 2>/dev/null || true

    check_stale_db_volume
    
    # Build images locally first (parallel + pull base images, retry on failure)
    if ! build_images_resilient "-f compose.yml -f compose.prod.yml"; then
        print_error "Cannot continue without successfully built images"
        exit 1
    fi
    
    if [ "$use_ssl" = true ]; then
        # Setup HTTP config first for certificate challenge
        setup_nginx_config "$domain" false
        
        # First start db, app and nginx for SSL certificate challenge
        print_info "Starting services for SSL certificate challenge..."
        docker compose -f compose.yml -f compose.prod.yml up -d --build db
        
        # Wait for database to be ready
        print_info "Waiting for database to be ready..."
        sleep 10
        
        docker compose -f compose.yml -f compose.prod.yml up -d --build app
        
        # Wait for app to be healthy
        print_info "Waiting for application to be ready..."
        local max_wait=60
        local waited=0
        while [ $waited -lt $max_wait ]; do
            if docker compose -f compose.yml -f compose.prod.yml ps app | grep -q "healthy"; then
                print_success "Application is healthy"
                break
            fi
            sleep 5
            waited=$((waited + 5))
            print_info "Waiting... ($waited/$max_wait seconds)"
        done
        
        # Start nginx
        docker compose -f compose.yml -f compose.prod.yml up -d nginx
        sleep 5
        
        # Try to obtain SSL certificate
        if obtain_ssl_certificate "$domain" "$email"; then
            # Stop nginx to reconfigure with SSL
            docker compose -f compose.yml -f compose.prod.yml stop nginx

            # Setup SSL config
            setup_nginx_config "$domain" true

            print_info "Starting NGINX with SSL..."
            docker compose -f compose.yml -f compose.prod.yml up -d nginx

            if verify_nginx_running; then
                ssl_success=true
                print_success "NGINX is serving HTTPS for ${domain}"

                # Start certbot renewal service
                print_info "Starting certbot renewal service..."
                docker compose -f compose.yml -f compose.prod.yml --profile ssl up -d certbot
            else
                # A crash-looping nginx takes the whole panel offline, which is
                # worse than no HTTPS — roll back to the HTTP-only config.
                print_error "NGINX failed to start with the SSL configuration:"
                docker compose -f compose.yml -f compose.prod.yml logs --tail 20 nginx || true
                print_warning "Rolling back to the HTTP-only configuration..."
                docker compose -f compose.yml -f compose.prod.yml stop nginx
                setup_nginx_config "$domain" false
                docker compose -f compose.yml -f compose.prod.yml up -d nginx
                ssl_success=false
            fi
        else
            # Already running with non-SSL config, just continue
            print_warning "Continuing with HTTP only (no SSL)..."
            ssl_success=false
        fi
    else
        setup_nginx_config "$domain" false
        
        print_info "Starting services without SSL..."
        
        # Start in correct order
        docker compose -f compose.yml -f compose.prod.yml up -d db
        print_info "Waiting for database..."
        sleep 10

        docker compose -f compose.yml -f compose.prod.yml up -d app
        print_info "Waiting for application..."
        sleep 10
        
        docker compose -f compose.yml -f compose.prod.yml up -d nginx
    fi
    
    print_success "Deployment with NGINX completed"
    
    # Return SSL status for show_deployment_info
    if [ "$ssl_success" = true ]; then
        return 0
    else
        return 1
    fi
}

deploy_standalone() {
    print_info "Deploying standalone (without NGINX)..."
    
    # Clean up any previous deployment
    print_info "Cleaning up previous deployment..."
    docker compose -f compose.yml down --remove-orphans 2>/dev/null || true
    docker network prune -f 2>/dev/null || true

    check_stale_db_volume
    
    # Build images (parallel + pull, with one no-cache retry on failure)
    if ! build_images_resilient "-f compose.yml"; then
        print_error "Cannot continue without successfully built images"
        exit 1
    fi

    print_info "Starting containers..."
    docker compose -f compose.yml up -d
    
    print_success "Standalone deployment completed"
}

get_server_ip() {
    local ip
    # Try to get the primary non-loopback IP
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)
    if [ -z "$ip" ]; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi
    if [ -z "$ip" ]; then
        ip="localhost"
    fi
    echo "$ip"
}

show_deployment_info() {
    local mode=$1
    local domain=$2
    local use_ssl=$3

    # Get admin credentials
    local admin_user="admin"
    local admin_password
    admin_password=$(grep "^ADMIN_PASSWORD=" backend/.env 2>/dev/null | cut -d'=' -f2)
    if [ -z "$admin_password" ]; then
        admin_password="admin123"
    fi

    echo ""
    echo "=========================================="
    print_success "🎉 Deployment completed successfully!"
    echo "=========================================="
    echo ""

    if [ "$mode" = "nginx" ]; then
        if [ "$use_ssl" = true ]; then
            echo "📍 Access URL: https://${domain}"
        else
            echo "📍 Access URL: http://${domain}"
        fi
        echo "🔒 SSL: ${use_ssl}"
    else
        local server_ip
        server_ip=$(get_server_ip)
        echo "📍 Access URL: http://${server_ip}:3001"
        echo "🔒 SSL: Not configured (standalone mode)"
    fi

    echo ""
    echo "👤 Admin credentials:"
    echo "   Login:    ${admin_user}"
    echo "   Password: ${admin_password}"
    echo ""
    echo "📊 Service Status:"
    docker compose ps
    echo ""
    echo "📝 View logs: docker compose logs -f"
    echo "🛑 Stop services: docker compose down"
    echo ""
}

# Main deployment logic
main() {
    echo "=========================================="
    echo "  PVEmanager Deployment Tool v1.0"
    echo "=========================================="
    echo ""
    
    check_requirements
    create_env_file
    install_update_watchdog
    install_pve_cli --auto
    
    # Ask deployment mode
    echo ""
    print_info "Select deployment mode:"
    echo "  1) Standalone (without NGINX, direct port 3001)"
    echo "  2) Production with NGINX (HTTP only)"
    echo "  3) Production with NGINX and SSL (HTTPS)"
    echo ""
    read -p "Enter your choice (1-3): " MODE_CHOICE
    
    case $MODE_CHOICE in
        1)
            deploy_standalone
            show_deployment_info "standalone"
            ;;
        2)
            read -p "Enter your domain name or server IP: " DOMAIN
            deploy_with_nginx "$DOMAIN" false
            show_deployment_info "nginx" "$DOMAIN" false
            ;;
        3)
            read -p "Enter your domain name: " DOMAIN
            read -p "Enter your email for Let's Encrypt: " EMAIL
            
            if [[ ! "$EMAIL" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
                print_error "Invalid email address"
                exit 1
            fi
            
            deploy_with_nginx "$DOMAIN" true "$EMAIL"
            ssl_result=$?
            if [ $ssl_result -eq 0 ]; then
                show_deployment_info "nginx" "$DOMAIN" true
            else
                show_deployment_info "nginx" "$DOMAIN" false
            fi
            ;;
        *)
            print_error "Invalid choice"
            exit 1
            ;;
    esac
}

# Quick deploy functions (non-interactive)
quick_deploy_standalone() {
    print_info "Quick deploy: Standalone mode"
    check_requirements
    
    # Create env files silently
    mkdir -p logs nginx/conf.d nginx/ssl nginx/certbot/conf nginx/certbot/www
    
    local RANDOM_DB_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-25)
    local RANDOM_SECRET_KEY=$(openssl rand -hex 32)
    local RANDOM_FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')

    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            cp .env.example .env
            sed -i "s/pvemanager_secure_password/${RANDOM_DB_PASSWORD}/g" .env 2>/dev/null || true
        else
            create_default_env "${RANDOM_DB_PASSWORD}"
        fi
    fi
    
    if [ ! -f backend/.env ]; then
        if [ -f backend/.env.example ]; then
            cp backend/.env.example backend/.env
            sed -i "s/pvemanager_secure_password/${RANDOM_DB_PASSWORD}/g" backend/.env 2>/dev/null || true
            sed -i "s/your-very-secure-secret-key-change-this-in-production-minimum-32-chars/${RANDOM_SECRET_KEY}/g" backend/.env 2>/dev/null || true
            sed -i "s|^FERNET_KEY=.*|FERNET_KEY=${RANDOM_FERNET_KEY}|" backend/.env 2>/dev/null || true
        else
            create_default_backend_env "${RANDOM_DB_PASSWORD}" "${RANDOM_SECRET_KEY}" "${RANDOM_FERNET_KEY}"
        fi
    fi
    
    deploy_standalone
    install_pve_cli --auto
    show_deployment_info "standalone"
}

quick_deploy_nginx() {
    local domain=$1
    local email=$2
    local use_ssl=false
    
    if [ -z "$domain" ]; then
        print_error "Domain is required. Usage: ./deploy.sh --nginx <domain> [email]"
        exit 1
    fi
    
    if [ -n "$email" ]; then
        use_ssl=true
    fi
    
    print_info "Quick deploy: NGINX mode (domain: $domain, ssl: $use_ssl)"
    check_requirements
    
    # Create env files silently
    mkdir -p logs nginx/conf.d nginx/ssl nginx/certbot/conf nginx/certbot/www
    
    local RANDOM_DB_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-25)
    local RANDOM_SECRET_KEY=$(openssl rand -hex 32)
    local RANDOM_FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')

    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            cp .env.example .env
            sed -i "s/pvemanager_secure_password/${RANDOM_DB_PASSWORD}/g" .env 2>/dev/null || true
        else
            create_default_env "${RANDOM_DB_PASSWORD}"
        fi
    fi
    
    if [ ! -f backend/.env ]; then
        if [ -f backend/.env.example ]; then
            cp backend/.env.example backend/.env
            sed -i "s/pvemanager_secure_password/${RANDOM_DB_PASSWORD}/g" backend/.env 2>/dev/null || true
            sed -i "s/your-very-secure-secret-key-change-this-in-production-minimum-32-chars/${RANDOM_SECRET_KEY}/g" backend/.env 2>/dev/null || true
            sed -i "s|^FERNET_KEY=.*|FERNET_KEY=${RANDOM_FERNET_KEY}|" backend/.env 2>/dev/null || true
        else
            create_default_backend_env "${RANDOM_DB_PASSWORD}" "${RANDOM_SECRET_KEY}" "${RANDOM_FERNET_KEY}"
        fi
    fi
    
    deploy_with_nginx "$domain" "$use_ssl" "$email"
    ssl_result=$?
    
    install_pve_cli --auto

    if [ "$use_ssl" = true ] && [ $ssl_result -eq 0 ]; then
        show_deployment_info "nginx" "$domain" true
    else
        show_deployment_info "nginx" "$domain" false
    fi
}

create_default_env() {
    local db_password="${1:-pvemanager_secure_password}"
    cat > .env << EOF
POSTGRES_PASSWORD=${db_password}
TZ=Asia/Tashkent
TIMEZONE=Asia/Tashkent
EOF
}

create_default_backend_env() {
    local db_password="${1:-pvemanager_secure_password}"
    local secret_key="${2:-your-very-secure-secret-key-change-this-in-production-minimum-32-chars}"
    local fernet_key="${3:-$(openssl rand -base64 32 | tr '+/' '-_')}"
    cat > backend/.env << EOF
PANEL_NAME=PVEmanager
DEBUG=false
LOG_LEVEL=INFO
DB_HOST=db
DB_PORT=5432
DB_USER=pvemanager
DB_PASSWORD=${db_password}
DB_NAME=pvemanager
DATABASE_URL=postgresql://pvemanager:${db_password}@db:5432/pvemanager
SECRET_KEY=${secret_key}
FERNET_KEY=${fernet_key}
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
SSH_TIMEOUT=10
DEFAULT_SSH_USER=root
DEFAULT_SSH_PORT=22
CORS_ORIGINS=*
ALLOWED_HOSTS=localhost,127.0.0.1
EOF
}

# Install the host-side update watchdog (systemd service)
install_update_watchdog() {
    local project_dir
    project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local service_name="pvemanager-update"
    local env_file="/etc/pvemanager-update.env"
    local service_file="/etc/systemd/system/${service_name}.service"
    local watchdog_script="$project_dir/update_host.sh"
    local run_user="${SUDO_USER:-$USER}"

    print_info "Installing update watchdog (systemd service)..."
    print_info "  Project directory : $project_dir"
    print_info "  Running as user   : $run_user"

    # Make sure the watchdog script is executable
    chmod +x "$watchdog_script"

    # Check that the user is in the docker group (root always has access, skip check)
    if [ "$run_user" != "root" ] && ! id -nG "$run_user" | grep -qw docker; then
        print_warning "User '$run_user' is not in the 'docker' group."
        print_warning "The watchdog needs docker access. Run:"
        print_warning "  sudo usermod -aG docker $run_user  (then log out/in)"
    fi

    # Write environment file (stores PROJECT_DIR so systemd can pass it to the script)
    if [ "$(id -u)" -eq 0 ]; then
        cat > "$env_file" <<EOF
PROJECT_DIR=$project_dir
EOF
        print_success "Wrote $env_file"

        # Write the systemd unit
        cat > "$service_file" <<EOF
[Unit]
Description=PVEmanager host-side update watchdog
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=$run_user
WorkingDirectory=$project_dir
EnvironmentFile=$env_file
ExecStart=$watchdog_script
Restart=always
RestartSec=5
StandardOutput=append:$project_dir/logs/update_host.log
StandardError=append:$project_dir/logs/update_host.log

[Install]
WantedBy=multi-user.target
EOF
        print_success "Wrote $service_file"

        systemctl daemon-reload
        systemctl enable  "$service_name"
        systemctl restart "$service_name"

        print_success "pvemanager-update.service is active and enabled"
    else
        # Not root — generate the files locally and show the user what to run with sudo
        print_warning "Not running as root. Generating systemd files locally..."

        mkdir -p "$project_dir/systemd"
        local local_env="$project_dir/systemd/pvemanager-update.env"
        local local_svc="$project_dir/systemd/pvemanager-update.service"

        cat > "$local_env" <<EOF
PROJECT_DIR=$project_dir
EOF

        cat > "$local_svc" <<EOF
[Unit]
Description=PVEmanager host-side update watchdog
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=$run_user
WorkingDirectory=$project_dir
EnvironmentFile=$env_file
ExecStart=$watchdog_script
Restart=always
RestartSec=5
StandardOutput=append:$project_dir/logs/update_host.log
StandardError=append:$project_dir/logs/update_host.log

[Install]
WantedBy=multi-user.target
EOF
        print_success "Files generated in $project_dir/systemd/"
        print_info "Run the following commands with sudo to install the watchdog:"
        echo ""
        echo "  sudo cp \"$local_env\" \"$env_file\""
        echo "  sudo cp \"$local_svc\" \"$service_file\""
        echo "  sudo systemctl daemon-reload"
        echo "  sudo systemctl enable $service_name"
        echo "  sudo systemctl start $service_name"
        echo ""
    fi
}

# Install pve CLI tool to /usr/local/bin/pve
# Usage:
#   install_pve_cli              — интерактивный режим (--install-cli): печатает help в конце
#   install_pve_cli --auto       — авто-установка во время deploy: тихая, не падает без root
install_pve_cli() {
    local mode="${1:-manual}"
    local project_dir
    project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local pve_source="$project_dir/pve"
    local install_path="/usr/local/bin/pve"

    if [ ! -f "$pve_source" ]; then
        if [ "$mode" = "--auto" ]; then
            print_warning "CLI скрипт 'pve' не найден рядом с deploy.sh — пропускаю авто-установку CLI."
            return 0
        fi
        print_error "CLI скрипт не найден: $pve_source"
        print_info "Убедитесь, что файл 'pve' находится в той же директории, что и deploy.sh"
        return 1
    fi

    # Идемпотентность: если уже установлен и указывает на ту же директорию — пропускаем
    if [ "$mode" = "--auto" ] && [ -f "$install_path" ]; then
        if grep -q "^PVE_DIR=\"$project_dir\"" "$install_path" 2>/dev/null; then
            print_info "pve CLI уже установлен ($install_path → $project_dir)"
            return 0
        fi
    fi

    # Prepare the patched script in a temp file
    local tmp_cli
    tmp_cli=$(mktemp /tmp/pve-cli-XXXXXX)
    if ! sed "s|PVE_DIR=\"/opt/pvemanager\"|PVE_DIR=\"$project_dir\"|g" \
            "$pve_source" > "$tmp_cli"; then
        rm -f "$tmp_cli"
        print_error "Не удалось подготовить pve CLI"
        return 1
    fi
    chmod 755 "$tmp_cli"

    # Try to install to /usr/local/bin (global PATH)
    local installed=false
    if [ "$(id -u)" -eq 0 ]; then
        cp "$tmp_cli" "$install_path" && installed=true
    else
        # Not root — try sudo (password may already be cached from docker ops)
        if sudo cp "$tmp_cli" "$install_path" 2>/dev/null && \
           sudo chmod 755 "$install_path" 2>/dev/null; then
            installed=true
        fi
    fi

    if [ "$installed" = true ]; then
        rm -f "$tmp_cli"
        print_success "pve CLI установлен в $install_path"
        print_info "Теперь вы можете использовать команду 'pve' из любого места."
        if [ "$mode" != "--auto" ]; then
            echo ""
            "$install_path" help
        fi
        return 0
    fi

    # Fallback: install to ~/.local/bin (works without root)
    local user_bin="${HOME}/.local/bin"
    local user_install_path="$user_bin/pve"
    mkdir -p "$user_bin"
    cp "$tmp_cli" "$user_install_path"
    rm -f "$tmp_cli"

    print_success "pve CLI установлен в $user_install_path"

    # Ensure ~/.local/bin is in PATH
    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$user_bin"; then
        local shell_rc=""
        if [ -f "$HOME/.bashrc" ]; then
            shell_rc="$HOME/.bashrc"
        elif [ -f "$HOME/.profile" ]; then
            shell_rc="$HOME/.profile"
        fi

        if [ -n "$shell_rc" ] && ! grep -q '\.local/bin' "$shell_rc" 2>/dev/null; then
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$shell_rc"
            print_info "Добавлен ~/.local/bin в PATH ($shell_rc)"
        fi

        export PATH="$user_bin:$PATH"
        print_warning "Для текущей сессии PATH обновлён. Для новых сессий выполните:"
        print_info "  source $shell_rc"
    fi

    if [ "$mode" != "--auto" ]; then
        echo ""
        "$user_install_path" help
    fi
}

show_help() {
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Interactive mode (default):"
    echo "  $0                          Run interactive deployment wizard"
    echo ""
    echo "Quick deploy options:"
    echo "  $0 --standalone             Deploy without NGINX (port 3001)"
    echo "  $0 --nginx <domain>         Deploy with NGINX (HTTP only)"
    echo "  $0 --nginx <domain> <email> Deploy with NGINX and SSL (HTTPS)"
    echo ""
    echo "Other options:"
    echo "  $0 --help                   Show this help message"
    echo "  $0 --status                 Show service status"
    echo "  $0 --stop                   Stop all services"
    echo "  $0 --restart                Restart all services"
    echo "  $0 --logs                   Show live logs"
    echo "  $0 --watchdog               Install/reinstall the host-side update watchdog (systemd)"
    echo "  $0 --reset-data             Wipe existing Postgres volume before deploy (DANGER: data loss)"
    echo "  sudo $0 --install-cli       Install 'pve' CLI tool to /usr/local/bin/pve"
    echo ""
    echo "Examples:"
    echo "  $0 --standalone"
    echo "  $0 --nginx example.com"
    echo "  $0 --nginx example.com admin@example.com"
    echo "  sudo $0 --watchdog"
    echo "  sudo $0 --install-cli"
}

# Parse command line arguments
# --reset-data is a modifier flag that may appear in any position; strip it here
# so the remaining arguments are interpreted normally.
RESET_DATA="false"
NEW_ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--reset-data" ]; then
        RESET_DATA="true"
    else
        NEW_ARGS+=("$arg")
    fi
done
set -- "${NEW_ARGS[@]}"
export RESET_DATA

if [ $# -gt 0 ]; then
    case "$1" in
        --help|-h)
            show_help
            exit 0
            ;;
        --standalone)
            quick_deploy_standalone
            exit 0
            ;;
        --nginx)
            quick_deploy_nginx "$2" "$3"
            exit 0
            ;;
        --status)
            docker compose -f compose.yml -f compose.prod.yml ps 2>/dev/null || docker compose ps
            exit 0
            ;;
        --stop)
            print_info "Stopping services..."
            docker compose -f compose.yml -f compose.prod.yml down 2>/dev/null || docker compose down
            print_success "Services stopped"
            exit 0
            ;;
        --restart)
            print_info "Restarting services..."
            docker compose -f compose.yml -f compose.prod.yml restart 2>/dev/null || docker compose restart
            print_success "Services restarted"
            exit 0
            ;;
        --logs)
            docker compose -f compose.yml -f compose.prod.yml logs -f 2>/dev/null || docker compose logs -f
            exit 0
            ;;
        --watchdog)
            install_update_watchdog
            exit 0
            ;;
        --install-cli)
            install_pve_cli
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
fi

# Run main function (interactive mode)
main
