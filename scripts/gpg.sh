#!/usr/bin/env bash
# Oreon Build Service - helper script to create a GPG key for RPM/repo signing.
# Usage (no sudo): ./scripts/gpg.sh "Oreon Build Signing <signing@example.com>"

set -euo pipefail

NAME_EMAIL="${1:-}"

if [ -z "$NAME_EMAIL" ]; then
  echo "Usage: $0 \"Name <email@example.com>\""
  exit 1
fi

GNUPGHOME="${GPG_HOME:-$HOME/.gnupg-oreon}"
EXPORT_DIR="${2:-./signing-export}"

echo "Using GNUPGHOME=${GNUPGHOME}"
mkdir -p "${GNUPGHOME}"
chmod 700 "${GNUPGHOME}"

if ! command -v gpg &>/dev/null; then
  echo "gpg not found. Install gnupg2 first (e.g. sudo dnf install gnupg2)."
  exit 1
fi

cat >"${GNUPGHOME}/gen-oreon-key.batch" <<EOF
Key-Type: RSA
Key-Length: 4096
Subkey-Type: RSA
Subkey-Length: 4096
Name-Real: ${NAME_EMAIL%%<*}
Name-Email: ${NAME_EMAIL##*<}
Expire-Date: 0
%no-protection
%commit
EOF

echo "Generating GPG key..."
gpg --batch --generate-key "${GNUPGHOME}/gen-oreon-key.batch"

echo "Listing keys:"
gpg --list-keys

KEY_ID=$(gpg --list-keys --with-colons | awk -F: '/^uid:/ {print $10; exit}')
echo "Suggested SIGNING_KEY_ID: ${KEY_ID}"

mkdir -p "${EXPORT_DIR}"
chmod 700 "${EXPORT_DIR}"
gpg --armor --export "${KEY_ID}" > "${EXPORT_DIR}/RPM-GPG-KEY-oreon-build.pub"

echo
echo "Public key exported to: ${EXPORT_DIR}/RPM-GPG-KEY-oreon-build.pub"
echo "Set in .env:"
echo "  SIGNING_KEY_ID=${KEY_ID}"
echo "  GPG_HOME=${GNUPGHOME}"

