#!/usr/bin/env bash
# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

set -e
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

echo "=== Oreon Build Service deploy ==="

if ! command -v psql &>/dev/null; then
  echo "PostgreSQL client not found. Install postgresql and postgresql-server."
  if command -v dnf &>/dev/null; then
    echo "Run: sudo dnf install postgresql-server pesign rpmrebuild file && sudo postgresql-setup --initdb"
  elif command -v yum &>/dev/null; then
    echo "Run: sudo yum install postgresql-server pesign rpmrebuild file && sudo postgresql-setup --initdb"
  elif command -v apt-get &>/dev/null; then
    echo "Run: sudo apt-get install postgresql postgresql-contrib"
  fi
  exit 1
fi

for svc in postgresql-16 postgresql-15 postgresql-14 postgresql; do
  if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${svc}.service"; then
    if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
      echo "Starting $svc..."
      sudo systemctl start "$svc" || true
      sudo systemctl enable "$svc" 2>/dev/null || true
    fi
    break
  fi
done

do_pg_hba() {
  local path="$1"
  if ! sudo test -f "$path" 2>/dev/null; then return 1; fi
  if ! sudo grep -qE '(127\.0\.0\.1/32|::1/128).*ident' "$path" 2>/dev/null; then return 0; fi
  echo "Switching localhost from ident to scram-sha-256 in $path..."
  sudo sed -i.bak \
    -e 's/\(127\.0\.0\.1\/32\)[[:space:]]*ident/\1 scram-sha-256/' \
    -e 's/\(::1\/128\)[[:space:]]*ident/\1 scram-sha-256/' \
    "$path"
  for svc in postgresql-16 postgresql-15 postgresql-14 postgresql; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then sudo systemctl reload "$svc"; break; fi
  done
  return 0
}
PG_HBA=$(sudo -u postgres psql -t -A -c "SHOW hba_file;" 2>/dev/null | tr -d '\r\n' | xargs)
if [ -n "$PG_HBA" ] && sudo test -f "$PG_HBA" 2>/dev/null; then
  do_pg_hba "$PG_HBA" || true
else
  for path in /var/lib/pgsql/16/data/pg_hba.conf /var/lib/pgsql/15/data/pg_hba.conf /var/lib/pgsql/data/pg_hba.conf; do
    do_pg_hba "$path" && break
  done
fi

DB_PASSWORD="${OREON_DB_PASSWORD:-}"
if [ -z "$DB_PASSWORD" ]; then
  DB_PASSWORD=$(openssl rand -base64 24 2>/dev/null | tr -dc 'a-zA-Z0-9' | head -c 24)
  echo "Generated DB password (saved to .env): ${DB_PASSWORD:0:4}..."
fi

sudo -u postgres psql -c "CREATE ROLE oreon WITH LOGIN PASSWORD '$DB_PASSWORD';" 2>/dev/null || \
  sudo -u postgres psql -c "ALTER ROLE oreon PASSWORD '$DB_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE oreon_build OWNER oreon;" 2>/dev/null || true
sudo -u postgres psql -d oreon_build -c "GRANT ALL ON SCHEMA public TO oreon; GRANT CREATE ON SCHEMA public TO oreon;"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi
export DATABASE_URL="postgresql+asyncpg://oreon:${DB_PASSWORD}@localhost:5432/oreon_build"
if grep -q '^DATABASE_URL=' .env; then
  sed -i.bak "s|^DATABASE_URL=.*|DATABASE_URL=$DATABASE_URL|" .env
else
  echo "DATABASE_URL=$DATABASE_URL" >> .env
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
  echo "Created .venv"
fi
source .venv/bin/activate
pip install -e . -q
pip install psycopg2-binary -q 2>/dev/null || true

OREON_TESTING_REPO="${OREON_TESTING_REPO:-https://repo-us.oreonhq.com/oreon-11-rp1/testing/x86_64/}"
if command -v dnf &>/dev/null; then
  echo "Installing cargo DynamicBuildRequires tools from Oreon testing..."
  if ! sudo dnf install -y \
      --repofrompath=oreon-testing,"$OREON_TESTING_REPO" \
      --repo=oreon-testing \
      cargo-rpm-macros cargo2rpm cargo 2>/dev/null; then
    echo "WARNING: could not install cargo-rpm-macros/cargo2rpm/cargo."
    echo "  On the API/planner host run:"
    echo "  sudo dnf install -y --repofrompath=oreon-testing,${OREON_TESTING_REPO} --repo=oreon-testing cargo-rpm-macros cargo2rpm cargo"
    echo "  Without those, cargo %generate_buildrequires stays incomplete and chains fail closed."
  fi
fi
if ! command -v cargo2rpm &>/dev/null || ! command -v cargo &>/dev/null; then
  echo "WARNING: cargo2rpm and/or cargo missing on PATH (needed for cargo DynamicBuildRequires)."
fi

if ! rpm -q go-vendor-tools &>/dev/null; then
  echo "NOTE: go-vendor-tools not installed. golang generate falls back to static BRs."
  echo "  Optional: sudo dnf install -y --repofrompath=oreon-testing,${OREON_TESTING_REPO} --repo=oreon-testing go-vendor-tools"
fi

export DATABASE_URL
alembic upgrade head
echo "Migrations applied."

echo ""
echo "!!! Deploy done !!!"