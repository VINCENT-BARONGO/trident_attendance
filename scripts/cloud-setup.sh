#!/usr/bin/env bash
# Test bench for Claude Code cloud sessions (Ubuntu 24.04, runs as root).
#
# Run it inside a session from the repo root:  bash scripts/cloud-setup.sh
# It builds a Frappe v15 bench with ERPNext and HRMS at /home/frappe/bench (user "frappe"),
# MariaDB from apt, the image's Redis, a test site test.localhost, and this checkout
# installed as trident_attendance. The first run takes 15-25 minutes; run it in the
# background and follow /tmp/cloud-setup.log. A second run only re-copies this checkout
# into the bench and migrates, so edit, then run it again, then test:
#
#   su frappe -c "cd /home/frappe/bench && bench --site test.localhost run-tests --app trident_attendance"
#
# FRAPPE_BRANCH=version-16 builds a v16 bench instead (needs Python 3.14 and Node 24,
# plus the newsletter app; not tried here yet).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
BRANCH=${FRAPPE_BRANCH:-version-15}
BENCH=/home/frappe/bench
SITE=test.localhost
DB_ROOT_PW=123   # a scratch MariaDB inside the session VM, nothing else
RUN="su frappe -s /bin/bash -c"
step() { echo "== $(date +%T) $*"; }

# --- system packages ---
if ! command -v mariadb >/dev/null || ! command -v node >/dev/null || ! command -v pkg-config >/dev/null; then
  step "apt packages"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    mariadb-server mariadb-client libmariadb-dev pkg-config git curl cron \
    python3-dev python3-venv python3-pip build-essential xvfb libfontconfig >/dev/null
fi
if ! node -e 'process.exit(+process.versions.node.split(".")[0] < 18)' 2>/dev/null; then
  step "node 20"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
command -v yarn >/dev/null || npm install -g --silent yarn

# --- MariaDB, set up the way Frappe wants it ---
cat > /etc/mysql/mariadb.conf.d/99-frappe.cnf <<'EOF'
[mysqld]
character-set-client-handshake = FALSE
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci
[mysql]
default-character-set = utf8mb4
EOF
service mariadb restart >/dev/null 2>&1 || service mariadb start
for _ in $(seq 30); do mariadb-admin ping --silent 2>/dev/null && break; mariadb-admin -uroot -p"$DB_ROOT_PW" ping --silent 2>/dev/null && break; sleep 1; done
if mariadb -uroot -e 'select 1' >/dev/null 2>&1; then
  mariadb -uroot -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '$DB_ROOT_PW'; FLUSH PRIVILEGES;"
fi
mariadb -uroot -p"$DB_ROOT_PW" -e 'select 1' >/dev/null

# --- bench user and CLI ---
id frappe >/dev/null 2>&1 || useradd -m -s /bin/bash frappe
if [ ! -x /opt/bench-cli/bin/bench ]; then
  step "bench CLI"
  python3 -m venv /opt/bench-cli
  /opt/bench-cli/bin/pip install --quiet frappe-bench
fi
ln -sf /opt/bench-cli/bin/bench /usr/local/bin/bench

# --- the bench ---
if [ ! -d "$BENCH/apps/frappe" ]; then
  step "bench init ($BRANCH)"
  rm -rf "$BENCH"
  $RUN "cd /home/frappe && bench init --frappe-branch $BRANCH --skip-redis-config-generation --skip-assets --python python3 bench"
fi
cd "$BENCH"
for app in erpnext hrms; do
  if [ ! -d "apps/$app" ]; then
    step "get-app $app"
    $RUN "cd $BENCH && bench get-app --branch $BRANCH --skip-assets $app"
  fi
done
if [ "$BRANCH" != version-15 ] && [ ! -d apps/newsletter ]; then
  $RUN "cd $BENCH && bench get-app --branch $BRANCH --skip-assets newsletter"
fi

# This checkout, copied in fresh every run (the bench must not write into the repo).
step "copy trident_attendance from $REPO"
mkdir -p apps/trident_attendance
cp -a "$REPO/." apps/trident_attendance/
rm -rf apps/trident_attendance/.git
chown -R frappe: apps/trident_attendance
$RUN "cd $BENCH && ./env/bin/pip install --quiet -e apps/trident_attendance"
grep -qx trident_attendance sites/apps.txt || echo trident_attendance >> sites/apps.txt

# --- Redis: cache and queue on the ports bench expects ---
redis-cli -p 13000 ping >/dev/null 2>&1 || redis-server --port 13000 --daemonize yes --save '' >/dev/null
redis-cli -p 11000 ping >/dev/null 2>&1 || redis-server --port 11000 --daemonize yes --save '' >/dev/null
$RUN "cd $BENCH && bench set-config -g redis_cache redis://127.0.0.1:13000 && bench set-config -g redis_queue redis://127.0.0.1:11000 && bench set-config -g redis_socketio redis://127.0.0.1:13000" >/dev/null

# --- the test site ---
if [ ! -d "sites/$SITE" ]; then
  step "new-site $SITE"
  $RUN "cd $BENCH && bench new-site $SITE --mariadb-root-password $DB_ROOT_PW --admin-password admin"
  step "install erpnext hrms"
  $RUN "cd $BENCH && bench --site $SITE install-app erpnext && bench --site $SITE install-app hrms"
  [ -d apps/newsletter ] && $RUN "cd $BENCH && bench --site $SITE install-app newsletter"
  $RUN "cd $BENCH && bench --site $SITE set-config allow_tests true"
fi
if ! $RUN "cd $BENCH && bench --site $SITE list-apps" | grep -q trident_attendance; then
  step "install trident_attendance"
  $RUN "cd $BENCH && bench --site $SITE install-app trident_attendance"
else
  step "migrate"
  $RUN "cd $BENCH && bench --site $SITE migrate --skip-search-index" >/dev/null
fi

step "frappe test bench ready: $($RUN "cd $BENCH && bench version" | tr '\n' ' ')"
