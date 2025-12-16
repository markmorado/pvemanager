#!/bin/bash

# PVEmanager Deployment Script
# Supports deployment with or without NGINX and SSL

set -e

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
    
    # Add current user to docker group
    if [ "$os" != "macos" ]; then
        sudo usermod -aG docker $USER
        print_warning "You may need to log out and back in for docker group changes to take effect"
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
    
    # Ensure required directories exist
    mkdir -p logs nginx/conf.d nginx/ssl nginx/certbot/conf nginx/certbot/www
    
    # Create init.sql if it doesn't exist
    if [ ! -f init.sql ]; then
        print_info "Creating init.sql file..."
        cat > init.sql << 'EOF'
-- PVEmanager Database Initialization
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
GRANT ALL PRIVILEGES ON DATABASE serverpanel TO serverpanel;
EOF
        print_success "init.sql file created"
    fi
    
    # Check if .env.example exists
    if [ ! -f .env.example ]; then
        print_error ".env.example file not found!"
        print_info "Creating default .env.example..."
        cat > .env.example << 'EOF'
# Database Configuration
POSTGRES_PASSWORD=serverpanel_secure_password

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
DB_USER=serverpanel
DB_PASSWORD=serverpanel_secure_password
DB_NAME=serverpanel
DATABASE_URL=postgresql://serverpanel:serverpanel_secure_password@db:5432/serverpanel

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
        sed -i "s/serverpanel_secure_password/${RANDOM_DB_PASSWORD}/g" .env
        
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
        sed -i "s/serverpanel_secure_password/${RANDOM_DB_PASSWORD}/g" backend/.env
        sed -i "s/your-very-secure-secret-key-change-this-in-production-minimum-32-chars/${RANDOM_SECRET_KEY}/g" backend/.env
        
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
        cp nginx/conf.d/serverpanel.conf.template nginx/conf.d/serverpanel.conf
        print_info "Using SSL configuration"
    else
        # Copy non-SSL template
        cp nginx/conf.d/serverpanel-nossl.conf.template nginx/conf.d/serverpanel.conf
        print_info "Using non-SSL configuration"
    fi
    
    # Replace domain name
    sed -i "s/DOMAIN_NAME/${domain}/g" nginx/conf.d/serverpanel.conf
    
    print_success "NGINX configuration created for domain: ${domain}"
}

obtain_ssl_certificate() {
    local domain=$1
    local email=$2
    
    print_info "Obtaining SSL certificate for ${domain}..."
    
    # Create certbot directories with proper permissions
    mkdir -p nginx/certbot/conf nginx/certbot/www
    chmod -R 755 nginx/certbot
    
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
    
    # Request certificate using standalone certbot (more reliable)
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
        -d "${domain}"
    
    local result=$?
    
    if [ $result -eq 0 ] && [ -d "nginx/certbot/conf/live/${domain}" ]; then
        print_success "SSL certificate obtained successfully"
        return 0
    else
        print_error "Failed to obtain SSL certificate"
        print_warning "Continuing with HTTP only..."
        return 1
    fi
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
    
    # Build images locally first
    print_info "Building Docker images..."
    docker compose -f compose.yml build
    
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
            ssl_success=true
            
            # Stop nginx to reconfigure with SSL
            docker compose -f compose.yml -f compose.prod.yml stop nginx
            
            # Setup SSL config
            setup_nginx_config "$domain" true
            
            print_info "Starting NGINX with SSL..."
            docker compose -f compose.yml -f compose.prod.yml up -d nginx
            
            # Start certbot renewal service
            print_info "Starting certbot renewal service..."
            docker compose -f compose.yml -f compose.prod.yml --profile ssl up -d certbot
        else
            # Already running with non-SSL config, just continue
            print_warning "Continuing with HTTP only (no SSL)..."
            ssl_success=false
        fi
    else
        setup_nginx_config "$domain" false
        
        print_info "Starting services without SSL..."
        
        # Start in correct order
        docker compose -f compose.yml -f compose.prod.yml up -d --build db
        print_info "Waiting for database..."
        sleep 10
        
        docker compose -f compose.yml -f compose.prod.yml up -d --build app
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
    
    # Build and start containers
    print_info "Building and starting containers..."
    docker compose -f compose.yml up -d --build
    
    print_success "Standalone deployment completed"
}

show_deployment_info() {
    local mode=$1
    local domain=$2
    local use_ssl=$3
    
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
        echo "📍 Access URL: http://localhost:8000"
        echo "🔒 SSL: Not configured (standalone mode)"
    fi
    
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
    
    # Ask deployment mode
    echo ""
    print_info "Select deployment mode:"
    echo "  1) Standalone (without NGINX, direct port 8000)"
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
    
    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            cp .env.example .env
            sed -i "s/serverpanel_secure_password/${RANDOM_DB_PASSWORD}/g" .env 2>/dev/null || true
        else
            create_default_env "${RANDOM_DB_PASSWORD}"
        fi
    fi
    
    if [ ! -f backend/.env ]; then
        if [ -f backend/.env.example ]; then
            cp backend/.env.example backend/.env
            sed -i "s/serverpanel_secure_password/${RANDOM_DB_PASSWORD}/g" backend/.env 2>/dev/null || true
            sed -i "s/your-very-secure-secret-key-change-this-in-production-minimum-32-chars/${RANDOM_SECRET_KEY}/g" backend/.env 2>/dev/null || true
        else
            create_default_backend_env "${RANDOM_DB_PASSWORD}" "${RANDOM_SECRET_KEY}"
        fi
    fi
    
    deploy_standalone
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
    
    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            cp .env.example .env
            sed -i "s/serverpanel_secure_password/${RANDOM_DB_PASSWORD}/g" .env 2>/dev/null || true
        else
            create_default_env "${RANDOM_DB_PASSWORD}"
        fi
    fi
    
    if [ ! -f backend/.env ]; then
        if [ -f backend/.env.example ]; then
            cp backend/.env.example backend/.env
            sed -i "s/serverpanel_secure_password/${RANDOM_DB_PASSWORD}/g" backend/.env 2>/dev/null || true
            sed -i "s/your-very-secure-secret-key-change-this-in-production-minimum-32-chars/${RANDOM_SECRET_KEY}/g" backend/.env 2>/dev/null || true
        else
            create_default_backend_env "${RANDOM_DB_PASSWORD}" "${RANDOM_SECRET_KEY}"
        fi
    fi
    
    deploy_with_nginx "$domain" "$use_ssl" "$email"
    ssl_result=$?
    
    if [ "$use_ssl" = true ] && [ $ssl_result -eq 0 ]; then
        show_deployment_info "nginx" "$domain" true
    else
        show_deployment_info "nginx" "$domain" false
    fi
}

create_default_env() {
    local db_password="${1:-serverpanel_secure_password}"
    cat > .env << EOF
POSTGRES_PASSWORD=${db_password}
TZ=Asia/Tashkent
TIMEZONE=Asia/Tashkent
EOF
}

create_default_backend_env() {
    local db_password="${1:-serverpanel_secure_password}"
    local secret_key="${2:-your-very-secure-secret-key-change-this-in-production-minimum-32-chars}"
    cat > backend/.env << EOF
PANEL_NAME=PVEmanager
DEBUG=false
LOG_LEVEL=INFO
DB_HOST=db
DB_PORT=5432
DB_USER=serverpanel
DB_PASSWORD=${db_password}
DB_NAME=serverpanel
DATABASE_URL=postgresql://serverpanel:${db_password}@db:5432/serverpanel
SECRET_KEY=${secret_key}
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
SSH_TIMEOUT=10
DEFAULT_SSH_USER=root
DEFAULT_SSH_PORT=22
CORS_ORIGINS=*
ALLOWED_HOSTS=localhost,127.0.0.1
EOF
}

show_help() {
    echo "PVEmanager Deployment Tool v1.0"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Interactive mode (default):"
    echo "  $0                          Run interactive deployment wizard"
    echo ""
    echo "Quick deploy options:"
    echo "  $0 --standalone             Deploy without NGINX (port 8000)"
    echo "  $0 --nginx <domain>         Deploy with NGINX (HTTP only)"
    echo "  $0 --nginx <domain> <email> Deploy with NGINX and SSL (HTTPS)"
    echo ""
    echo "Other options:"
    echo "  $0 --help                   Show this help message"
    echo "  $0 --status                 Show service status"
    echo "  $0 --stop                   Stop all services"
    echo "  $0 --restart                Restart all services"
    echo "  $0 --logs                   Show live logs"
    echo ""
    echo "Examples:"
    echo "  $0 --standalone"
    echo "  $0 --nginx example.com"
    echo "  $0 --nginx example.com admin@example.com"
}

# Parse command line arguments
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
        *)
            print_error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
fi

# Run main function (interactive mode)
main
