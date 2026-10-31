#!/bin/bash

# ==============================================================================
# FILE PURPOSE:
# Master automated 24/7 VPS deployment and daemon management script for
# Telegram Promotion Tool (ProAffiliaters Lead Engine).
#
# Target Domain:
#   https://telegramtools.proaffiliaters.com -> Proxied to Port 8000 (FastAPI/Uvicorn)
#
# Major Responsibilities:
# 1. Pulling latest code changes from Git repository.
# 2. Creating and activating dedicated Python virtual environment (venv).
# 3. Installing and updating dependencies from requirements.txt.
# 4. Validating MongoDB service connectivity.
# 5. Supervising 24/7 execution via PM2 process manager ('telegram-promotion-tool').
# 6. Configuring Nginx reverse proxy (/etc/nginx/sites-available/telegramtools-proaffiliaters).
# 7. Automating Let's Encrypt SSL certificate provisioning via Certbot.
# 8. Setting up automatic boot persistence (pm2 save & startup).
# ==============================================================================

set -e # Exit immediately on unhandled command error

# Color formatting
BOLD='\033[1m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Helper log functions
log_info()    { echo -e "${BLUE}${BOLD}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}${BOLD}[SUCCESS]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}${BOLD}[WARNING]${NC} $1"; }
log_error()   { echo -e "${RED}${BOLD}[ERROR]${NC} $1"; }
log_step()    { echo -e "\n${CYAN}${BOLD}===> $1${NC}"; }

# Absolute path of the repository
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DOMAIN="telegramtools.proaffiliaters.com"
APP_PORT="8000"
PM2_APP_NAME="telegram-promotion-tool"
NGINX_SITE_CONF="/etc/nginx/sites-available/telegramtools-proaffiliaters"
NGINX_SITE_LINK="/etc/nginx/sites-enabled/telegramtools-proaffiliaters"

# ------------------------------------------------------------------------------
# Function: Configure Nginx Reverse Proxy
# ------------------------------------------------------------------------------
setup_nginx_proxy() {
  log_step "Configuring Nginx Reverse Proxy for $APP_DOMAIN"

  if ! command -v nginx > /dev/null 2>&1; then
    log_error "Nginx is not installed on this system. Run: sudo apt update && sudo apt install -y nginx"
    return 1
  fi

  log_info "Writing $NGINX_SITE_CONF..."
  sudo bash -c "cat > $NGINX_SITE_CONF" << EOF
# ==============================================================================
# Nginx Reverse Proxy Configuration for Telegram Promotion Tool
# Domain: $APP_DOMAIN
# Internal Upstream: http://127.0.0.1:$APP_PORT
# ==============================================================================

server {
    listen 80;
    server_name $APP_DOMAIN;

    client_max_body_size 50M;

    # Gzip Compression
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_proxied any;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;

    # Dashboard & API Proxy
    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;

        # WebSocket and Long-Polling headers (critical for live dashboard streaming)
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_cache_bypass \$http_upgrade;

        # Real Client IP Resolution
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Timeouts for streaming and long-lived Telegram connections
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
        proxy_send_timeout 300s;
    }

    # Static Assets Cache (Thumbnails & Promotion Banners)
    location /promo-assets/ {
        proxy_pass http://127.0.0.1:$APP_PORT/promo-assets/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        expires 7d;
        add_header Cache-Control "public, max-age=604800, immutable";
    }
}
EOF

  log_info "Enabling site symlink in /etc/nginx/sites-enabled/..."
  sudo ln -sf "$NGINX_SITE_CONF" "$NGINX_SITE_LINK"

  log_info "Testing Nginx configuration syntax..."
  if sudo nginx -t; then
    log_success "Nginx syntax is valid!"
    sudo systemctl reload nginx
    log_success "Nginx reloaded successfully!"
  else
    log_error "Nginx syntax error detected! Review logs above."
    return 1
  fi

  echo ""
  log_success "Nginx reverse proxy for $APP_DOMAIN is active!"
  echo -e "   • Domain  : ${BOLD}http://$APP_DOMAIN${NC}"
  echo -e "   • Upstream: ${BOLD}http://127.0.0.1:$APP_PORT${NC}\n"
}

# ------------------------------------------------------------------------------
# Function: Configure Let's Encrypt SSL via Certbot
# ------------------------------------------------------------------------------
setup_ssl_certbot() {
  log_step "Setting up Let's Encrypt SSL Certificate for $APP_DOMAIN"

  if ! command -v certbot > /dev/null 2>&1; then
    log_warn "Certbot not found. Attempting to install certbot and python3-certbot-nginx..."
    sudo apt update && sudo apt install -y certbot python3-certbot-nginx
  fi

  log_info "Requesting SSL certificate for $APP_DOMAIN..."
  sudo certbot --nginx -d "$APP_DOMAIN" --non-interactive --agree-tos --redirect --register-unsafely-without-email || \
  sudo certbot --nginx -d "$APP_DOMAIN" --redirect

  log_success "SSL Certificate configured successfully!"
  echo -e "   • Secure URL: ${BOLD}https://$APP_DOMAIN${NC}\n"
}

# ------------------------------------------------------------------------------
# Function: Configure Optional Systemd Service (alternative to PM2)
# ------------------------------------------------------------------------------
setup_systemd_service() {
  log_step "Configuring Native Systemd Service for 24/7 Autopilot"

  VENV_PYTHON="$ROOT_DIR/venv/bin/python"
  CURRENT_USER=$(whoami)

  SERVICE_FILE="/etc/systemd/system/telegram-promotion-tool.service"
  log_info "Writing $SERVICE_FILE..."

  sudo bash -c "cat > $SERVICE_FILE" << EOF
[Unit]
Description=Telegram Promotion Tool & Lead Extraction Engine (ProAffiliaters)
After=network.target mongod.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$ROOT_DIR
EnvironmentFile=$ROOT_DIR/.env
ExecStart=$VENV_PYTHON src/main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable telegram-promotion-tool
  sudo systemctl restart telegram-promotion-tool
  log_success "Systemd service 'telegram-promotion-tool' enabled and started!"
  sudo systemctl status telegram-promotion-tool --no-pager
}

# ------------------------------------------------------------------------------
# Function: Show Application Health & Status
# ------------------------------------------------------------------------------
show_status() {
  log_step "Checking System & Process Status"
  
  echo -e "${BOLD}--- PM2 Status ---${NC}"
  if command -v pm2 > /dev/null 2>&1; then
    pm2 status "$PM2_APP_NAME" || true
  else
    log_warn "PM2 is not installed."
  fi

  echo -e "\n${BOLD}--- Port $APP_PORT Listener ---${NC}"
  if command -v ss > /dev/null 2>&1; then
    ss -tlpn | grep ":$APP_PORT" || echo "Port $APP_PORT is not listening."
  elif command -v netstat > /dev/null 2>&1; then
    netstat -tlpn | grep ":$APP_PORT" || echo "Port $APP_PORT is not listening."
  fi

  echo -e "\n${BOLD}--- MongoDB Status ---${NC}"
  if command -v systemctl > /dev/null 2>&1; then
    systemctl is-active --quiet mongod && log_success "MongoDB is active." || log_warn "MongoDB is inactive or not managed by systemd."
  fi

  echo -e "\n${BOLD}--- Nginx Status ---${NC}"
  if command -v systemctl > /dev/null 2>&1; then
    systemctl is-active --quiet nginx && log_success "Nginx is active." || log_warn "Nginx is inactive."
  fi
}

# ------------------------------------------------------------------------------
# Parse Command Line Arguments
# ------------------------------------------------------------------------------
MODE="deploy"

if [ "$1" == "--quick" ]; then
  MODE="quick"
elif [ "$1" == "--setup-nginx" ]; then
  setup_nginx_proxy
  exit 0
elif [ "$1" == "--setup-ssl" ]; then
  setup_ssl_certbot
  exit 0
elif [ "$1" == "--setup-systemd" ]; then
  setup_systemd_service
  exit 0
elif [ "$1" == "--setup-all" ]; then
  MODE="all"
elif [ "$1" == "--status" ]; then
  show_status
  exit 0
elif [ "$1" == "--logs" ]; then
  if command -v pm2 > /dev/null 2>&1; then
    pm2 logs "$PM2_APP_NAME"
  else
    log_error "PM2 not found. Cannot stream logs."
  fi
  exit 0
fi

echo -e "${BOLD}${CYAN}"
echo "========================================================"
echo "    🚀 ProAffiliaters Telegram Promotion Tool VPS Deploy "
echo "    Domain: https://$APP_DOMAIN                         "
echo "    Target: $ROOT_DIR                                   "
echo "    Mode  : $MODE                                       "
echo "========================================================"
echo -e "${NC}"

# ------------------------------------------------------------------------------
# STEP 1: Git Pull Latest Changes (if git repo)
# ------------------------------------------------------------------------------
log_step "Step 1: Synchronizing latest codebase from Git"
cd "$ROOT_DIR"

if [ -d ".git" ]; then
  CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
  log_info "Active Git branch: $CURRENT_BRANCH"

  # Discard volatile runtime lockfiles if tracked
  git checkout -- *.session-journal 2>/dev/null || true
  git checkout -- *.pid 2>/dev/null || true

  git pull origin "$CURRENT_BRANCH"
  log_success "Codebase synchronized with origin/$CURRENT_BRANCH."
else
  log_warn "No .git directory found in $ROOT_DIR. Skipping git pull."
fi

# ------------------------------------------------------------------------------
# STEP 2: Configure Environment (.env)
# ------------------------------------------------------------------------------
log_step "Step 2: Checking Environment Configuration (.env)"
if [ ! -f "$ROOT_DIR/.env" ]; then
  if [ -f "$ROOT_DIR/.env.example" ]; then
    log_info "Creating .env from .env.example..."
    cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
    log_warn "Created new .env file. Ensure your TELEGRAM_API_ID and MONGODB_URI are configured."
  else
    log_warn "No .env or .env.example found. Running with environment defaults."
  fi
else
  log_success ".env configuration file is present."
fi

# ------------------------------------------------------------------------------
# STEP 3: Setup Python Virtual Environment & Install Dependencies
# ------------------------------------------------------------------------------
log_step "Step 3: Setting up Python Environment"

# Discover Python 3 binary
PYTHON_BIN=$(command -v python3 || command -v python || echo "")
if [ -z "$PYTHON_BIN" ]; then
  log_error "Python 3 is not installed on this VPS! Run: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi
log_info "Using host Python: $($PYTHON_BIN --version) at $PYTHON_BIN"

# Create virtualenv if not present
VENV_DIR="$ROOT_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
  log_info "Creating Python virtual environment in $VENV_DIR..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  log_success "Virtual environment created."
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

if [ "$MODE" != "quick" ]; then
  log_info "Upgrading pip and installing production requirements..."
  "$VENV_PIP" install --upgrade pip --quiet
  "$VENV_PIP" install -r "$ROOT_DIR/requirements.txt" --quiet
  log_success "Python dependencies verified and up to date."
else
  log_info "Quick mode selected: Skipping pip install."
fi

# ------------------------------------------------------------------------------
# STEP 4: Verify MongoDB Service
# ------------------------------------------------------------------------------
log_step "Step 4: Checking MongoDB Service"
if command -v systemctl > /dev/null 2>&1; then
  if systemctl is-active --quiet mongod; then
    log_success "MongoDB service (mongod) is running."
  else
    log_warn "MongoDB service is inactive. Attempting to start..."
    sudo systemctl start mongod || log_warn "Could not start mongod via systemctl. Ensure MongoDB or Docker Mongo is running."
  fi
else
  log_info "systemctl not available, skipping mongod daemon check."
fi

# ------------------------------------------------------------------------------
# STEP 5: Process Supervision via PM2 (24/7 Autopilot)
# ------------------------------------------------------------------------------
log_step "Step 5: Managing 24/7 Process in PM2 ('$PM2_APP_NAME')"

if ! command -v pm2 > /dev/null 2>&1; then
  log_warn "PM2 is not installed. Attempting to install globally via npm..."
  if command -v npm > /dev/null 2>&1; then
    sudo npm install -g pm2
    log_success "PM2 installed successfully."
  else
    log_error "npm is not installed. Cannot install PM2 automatically."
    log_info "You can use systemd instead by running: ./deploy.sh --setup-systemd"
    exit 1
  fi
fi

# Restart existing or launch new PM2 process
if pm2 describe "$PM2_APP_NAME" > /dev/null 2>&1; then
  log_info "Restarting existing PM2 process '$PM2_APP_NAME' with updated environment..."
  pm2 restart "$PM2_APP_NAME" --update-env
  log_success "PM2 process '$PM2_APP_NAME' restarted."
else
  log_info "Starting new PM2 process '$PM2_APP_NAME' using $VENV_PYTHON..."
  pm2 start src/main.py --name "$PM2_APP_NAME" --interpreter "$VENV_PYTHON"
  log_success "PM2 process '$PM2_APP_NAME' launched."
fi

# Save PM2 process list for automatic reboot recovery
pm2 save
log_success "PM2 process list saved."

# ------------------------------------------------------------------------------
# STEP 6: Optional Full Setup (Nginx + SSL)
# ------------------------------------------------------------------------------
if [ "$MODE" == "all" ]; then
  setup_nginx_proxy
  setup_ssl_certbot
fi

# ------------------------------------------------------------------------------
# Summary & Next Steps
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}${GREEN}========================================================"
echo "    🎉 24/7 VPS DEPLOYMENT COMPLETED SUCCESSFULLY!      "
echo "========================================================${NC}\n"

pm2 status "$PM2_APP_NAME"

echo -e "\n${CYAN}${BOLD}🌐 Access Points & Monitoring:${NC}"
echo -e "   • Public URL   : ${BOLD}https://$APP_DOMAIN${NC}"
echo -e "   • Local Server : ${BOLD}http://127.0.0.1:$APP_PORT${NC}"
echo -e "   • Live Logs    : ${BOLD}pm2 logs $PM2_APP_NAME${NC}  (or ./deploy.sh --logs)"
echo -e "   • Process Info : ${BOLD}pm2 show $PM2_APP_NAME${NC}"
echo ""

if [ ! -f "$NGINX_SITE_LINK" ]; then
  echo -e "${YELLOW}${BOLD}⚠️  Next Step: Configure Nginx Domain Routing${NC}"
  echo -e "   To link ${BOLD}$APP_DOMAIN${NC} to Port $APP_PORT and generate an SSL certificate, run:"
  echo -e "   ${BOLD}./deploy.sh --setup-nginx${NC}"
  echo -e "   ${BOLD}./deploy.sh --setup-ssl${NC}\n"
fi

