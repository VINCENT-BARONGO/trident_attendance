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
# Network: the cloud environment must allow codeload.github.com (Frappe's yarn packages
# include github: tarballs); the default "Trusted" list blocks it.
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
# The image ships Node 22 in /opt/node22/bin (deb.nodesource.com is blocked); Ubuntu's own
# nodejs is the fallback.
if ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 18 ? 1 : 0)' 2>/dev/null; then
  step "nodejs from apt"
  apt-get install -y -qq nodejs npm >/dev/null
fi
command -v yarn >/dev/null || npm install -g --silent yarn
# su resets PATH, so the frappe user sees only /usr/local/bin and the like.
for b in node npm npx yarn; do ln -sf "$(command -v $b)" /usr/local/bin/$b 2>/dev/null || true; done

# The session's TLS bundle sits under /root, which the frappe user cannot read: share a copy.
if [ -n "${SSL_CERT_FILE:-}" ] && [ -r "$SSL_CERT_FILE" ] && [ "${SSL_CERT_FILE#/root/}" != "$SSL_CERT_FILE" ]; then
  install -m 644 "$SSL_CERT_FILE" /opt/ca-bundle.crt
  export SSL_CERT_FILE=/opt/ca-bundle.crt CURL_CA_BUNDLE=/opt/ca-bundle.crt REQUESTS_CA_BUNDLE=/opt/ca-bundle.crt \
    NODE_EXTRA_CA_CERTS=/opt/ca-bundle.crt GIT_SSL_CAINFO=/opt/ca-bundle.crt
fi

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
  /opt/bench-cli/bin/pip install --quiet frappe-bench uv
fi
[ -x /opt/bench-cli/bin/uv ] || /opt/bench-cli/bin/pip install --quiet uv   # bench init runs `uv venv`
ln -sf /opt/bench-cli/bin/bench /usr/local/bin/bench
ln -sf /opt/bench-cli/bin/uv /usr/local/bin/uv

# --- the bench ---
if [ ! -d "$BENCH/apps/frappe" ]; then
  # The cloud proxy refuses codeload.github.com whatever the network setting says, and yarn
  # fetches "github:" packages (frappe's air-datepicker) from there. Git through github.com
  # works, so a local copy of frappe gets those packages pointed at git URLs first.
  step "frappe source ($BRANCH), github: packages via git"
  SRC=/home/frappe/frappe-src
  rm -rf "$SRC"
  $RUN "git clone --quiet --depth 1 --branch $BRANCH https://github.com/frappe/frappe.git $SRC"
  $RUN "cd $SRC && python3 - <<'EOF'
import re
lock = open('yarn.lock').read()
# Lock blocks: \"name@github:owner/repo\": ... resolved \"https://codeload.github.com/owner/repo/tar.gz/sha\"
blocks = re.findall(r'^\"?([^\s\"]+)@github:([\w.-]+/[\w.-]+)[^\n]*:\n(?:  .*\n)*?  resolved \"https://codeload\.github\.com/[\w.-]+/[\w.-]+/tar\.gz/(\w+)\"', lock, re.M)
pkg = open('package.json').read()
for name, repo, sha in blocks:
    git = f'git+https://github.com/{repo}.git#{sha}'
    lock = re.sub(rf'^\"?{re.escape(name)}@github:{re.escape(repo)}[^\n]*:$', f'\"{name}@{git}\":', lock, flags=re.M)
    lock = lock.replace(f'https://codeload.github.com/{repo}/tar.gz/{sha}', git)
    pkg = re.sub(rf'\"github:{re.escape(repo)}(#[^\"]*)?\"', f'\"{git}\"', pkg)
    print('via git:', name, git)
open('yarn.lock', 'w').write(lock)
open('package.json', 'w').write(pkg)
EOF
git -c user.name=cloud -c user.email=cloud@localhost commit --quiet -am 'github: packages via git'"
  rm -rf "$BENCH"
  $RUN "cd /home/frappe && bench init --frappe-path $SRC --frappe-branch $BRANCH --skip-redis-config-generation --skip-assets --python python3 bench"
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
