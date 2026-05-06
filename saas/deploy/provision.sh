#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 24.04 droplet for the Lichess AI Coach SaaS.
# Run as root (or via `sudo bash provision.sh`).
set -euo pipefail

DOMAIN="${DOMAIN:?Set DOMAIN, e.g. DOMAIN=chesscoach.gg}"
APP_USER="coach"
APP_DIR="/opt/lichess-ai-coach"
DB_NAME="coach"
DB_USER="coach"
DB_PASS="${DB_PASS:-$(openssl rand -hex 24)}"

echo "==> Updating apt"
apt-get update -y
apt-get full-upgrade -y

echo "==> Installing system packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip python3-dev \
    postgresql postgresql-contrib \
    git ufw \
    fonts-dejavu-core tzdata ca-certificates curl \
    debian-keyring debian-archive-keyring apt-transport-https

echo "==> Installing Caddy"
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | tee /etc/apt/trusted.gpg.d/caddy-stable.asc >/dev/null
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
apt-get update -y
apt-get install -y caddy

echo "==> Configuring firewall"
ufw allow OpenSSH || true
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> Creating app user"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Configuring Postgres"
sudo -u postgres psql <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '$DB_USER') THEN
    CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';
  END IF;
END \$\$;
SELECT 'CREATE DATABASE $DB_NAME OWNER $DB_USER'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$DB_NAME')\gexec
SQL

echo "==> Cloning app. Override REPO_URL/BRANCH if needed."
REPO_URL="${REPO_URL:-https://github.com/charlesbueso/lichess-ai-coach.git}"
BRANCH="${BRANCH:-main}"
if [ ! -d "$APP_DIR/.git" ]; then
  sudo -u "$APP_USER" git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  cd "$APP_DIR" && sudo -u "$APP_USER" git fetch && sudo -u "$APP_USER" git checkout "$BRANCH" && sudo -u "$APP_USER" git pull
fi

echo "==> Creating venv + installing deps"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/saas/requirements.txt"

echo "==> Applying DB schema"
sudo -u postgres psql -d "$DB_NAME" -f "$APP_DIR/saas/migrations/0001_init.sql"

echo "==> Writing /etc/coach.env (FILL IN SECRETS)"
if [ ! -f /etc/coach.env ]; then
  cat > /etc/coach.env <<EOF
DATABASE_URL=postgresql://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME

DISCORD_TOKEN=
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=

LICHESS_CONTACT=
GROQ_API_KEY=
GROQ_MODEL=llama-3.1-8b-instant

STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRICE_ID=

BASE_URL=https://$DOMAIN
SESSION_SECRET=$(openssl rand -hex 32)
HTTP_HOST=127.0.0.1
HTTP_PORT=8000

GAMES_PER_DAY_PER_TENANT=20
ASKS_PER_GAME=10
POLL_INTERVAL_MINUTES=10

SENTRY_DSN=
POSTHOG_KEY=
POSTHOG_HOST=https://us.i.posthog.com

APP_NAME=Lichess AI Coach
SUPPORT_EMAIL=support@$DOMAIN

LICHESS_USERNAME=__saas__
DISCORD_CHANNEL_ID=0
EOF
  chmod 600 /etc/coach.env
  chown root:root /etc/coach.env
fi

echo "==> Installing systemd unit"
install -m 644 "$APP_DIR/saas/deploy/lichess-coach-saas.service" /etc/systemd/system/lichess-coach-saas.service
systemctl daemon-reload
systemctl enable lichess-coach-saas

echo "==> Installing Caddyfile"
sed "s/{\$DOMAIN}/$DOMAIN/g" "$APP_DIR/saas/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "==> Installing nightly Postgres backup"
install -m 755 "$APP_DIR/saas/deploy/pg_backup.sh" /usr/local/bin/pg_backup_coach.sh
mkdir -p /var/backups/coach
cat > /etc/cron.d/coach-pg-backup <<'EOF'
30 3 * * * postgres /usr/local/bin/pg_backup_coach.sh >>/var/log/pg_backup_coach.log 2>&1
EOF

cat <<EOF

================================================================
Provisioning complete.

DB password (write it down):  $DB_PASS

Next steps:
  1. Edit /etc/coach.env and fill in DISCORD_*, GROQ_API_KEY,
     STRIPE_*, SENTRY_DSN, POSTHOG_KEY, LICHESS_CONTACT.
  2. Point DNS A record for $DOMAIN at this droplet's IP.
  3. systemctl start lichess-coach-saas
  4. journalctl -u lichess-coach-saas -f

Webhook URL to register in Stripe:
   https://$DOMAIN/stripe/webhook

Discord OAuth redirect URI to register:
   https://$DOMAIN/discord/callback
================================================================
EOF
