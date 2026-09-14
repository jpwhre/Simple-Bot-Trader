#!/usr/bin/env bash
# Simple Bot Trader — signed installer (Linux / macOS).
#
# Downloads the latest (or a pinned) signed release, verifies its sha256 AND
# its Ed25519 signature against the public key below, unpacks it, and runs the
# bundled setup.sh. The private key that signs releases lives ONLY on the dev
# machine, so a tampered / MITM'd / re-uploaded asset cannot install.
#
#   curl -fsSL https://raw.githubusercontent.com/jpwhre/Simple-Bot-Trader/main/install.sh | bash
#   bash install.sh --dir ~/bot                  # custom install directory
#   bash install.sh --tag v0.0.1                 # pin to an exact release
#   bash install.sh --update                     # refresh an existing install
set -euo pipefail

# ==== SHIP-TIME CONFIG (matches sbt/auto_update.py) ====
OWNER="jpwhre"
REPO="Simple-Bot-Trader"
ASSET_PREFIX="simple-bot-trader-"
API="https://api.github.com/repos/${OWNER}/${REPO}"
PUBKEY_HEX="7debae06d485554131770feefffc9b706dcf2e8f609f68444656e653f4e759b5"

DEFAULT_DIR="${HOME}/Simple-Bot-Trader"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { printf "${GREEN}[OK]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
fail() { printf "${RED}[FAIL]${NC} %s\n" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
usage: install.sh [options]

  --dir <path>   install directory            (default: ~/Simple-Bot-Trader)
  --tag <ver>    pin to an exact release tag  (default: latest release)
  --update       replace app files of an existing install in place
  --force        alias of --update
  -h | --help    show this help
USAGE
}

DIR="$DEFAULT_DIR"
TAG=""
UPDATE="no"
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)   shift; DIR="${1}" ;;
    --tag)   shift; TAG="${1}" ;;
    --update|--force) UPDATE="yes" ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1 (see --help)" ;;
  esac
  shift
done
case "$DIR" in
  "~"|"~"/*) DIR="${DIR/#\~/$HOME}" ;;
esac

# ==== prerequisites ====
command -v curl    >/dev/null 2>&1 || fail "curl is required."
command -v openssl >/dev/null 2>&1 || fail "openssl is required (Ed25519 verification)."
command -v perl    >/dev/null 2>&1 || fail "perl is required."
if ! command -v unzip >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
  fail "unzip (or python3) is required to unpack the release."
fi

# openssl path supports Ed25519 only on genuine OpenSSL (1.1.1+); macOS (LibreSSL)
# falls back to a python3+cryptography verifier below.
USE_OSSL=no
RAWIN=""
if openssl version 2>/dev/null | awk '{print $1}' | grep -qi '^openssl'; then
  _maj="$(openssl version | awk '{print $2}' | cut -d. -f1)"
  if [ "${_maj:-0}" -ge 1 ]; then
    USE_OSSL=yes
    [ "${_maj:-0}" -ge 3 ] && RAWIN="-rawin"
  fi
fi

hex2bin() { perl -e 'print pack("H*", <STDIN>)'; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# verify_ed25519 <message-file> <signature-binary> <pubkey-hex>  ->  0 ok / 1 fail
verify_ed25519() {
  local msg="$1" sigbin="$2" pubhex="$3" pkf der
  if [ "$USE_OSSL" = "yes" ]; then
    pkf="$TMP/pub.pem"
    der="$(printf '%s' "302a300506032b6570032100${pubhex}" | hex2bin | base64)"
    { printf -- '-----BEGIN PUBLIC KEY-----\n'; printf '%s\n' "$der"; printf -- '-----END PUBLIC KEY-----\n'; } > "$pkf"
    if openssl pkeyutl -verify -pubin -inkey "$pkf" -sigfile "$sigbin" -in "$msg" $RAWIN >/dev/null 2>&1; then
      return 0
    fi
  fi
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import cryptography' >/dev/null 2>&1; then
    if python3 - "$msg" "$sigbin" "$pubhex" <<'PY' >/dev/null 2>&1; then
import sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(sys.argv[3]))
msg = open(sys.argv[1], 'rb').read()
sig = open(sys.argv[2], 'rb').read()
try:
    pk.verify(sig, msg)
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
      return 0
    fi
  fi
  if command -v python3 >/dev/null 2>&1; then
    local pv="$TMP/venv"
    if [ ! -x "$pv/bin/python" ]; then
      warn "setting up a one-time verification helper (cryptography)..."
      python3 -m venv "$pv" >/dev/null 2>&1 || return 1
      "$pv/bin/pip" install -q cryptography >/dev/null 2>&1 || return 1
    fi
    if "$pv/bin/python" - "$msg" "$sigbin" "$pubhex" <<'PY' >/dev/null 2>&1; then
import sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(sys.argv[3]))
msg = open(sys.argv[1], 'rb').read()
sig = open(sys.argv[2], 'rb').read()
try:
    pk.verify(sig, msg)
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
      return 0
    fi
  fi
  return 1
}

# ==== resolve the release ====
if [ -n "$TAG" ]; then
  RELEASE="$(curl -fsSL --retry 3 "${API}/releases/tags/${TAG}" 2>/dev/null || true)"
else
  RELEASE="$(curl -fsSL --retry 3 "${API}/releases/latest" 2>/dev/null || true)"
  TAG="$(printf '%s' "$RELEASE" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1 || true)"
fi
[ -n "$TAG" ] || fail "no release found for ${OWNER}/${REPO} (is the repo published / a release tagged?)."

DIGEST="$(printf '%s' "$RELEASE" | grep -oE 'SBT-DIGEST: [0-9a-fA-F]{64}' | head -1 | awk '{print $2}' | tr 'A-F' 'a-f' || true)"
SIGNATURE="$(printf '%s' "$RELEASE" | grep -oE 'SBT-SIGNATURE: [0-9a-fA-F]{128}' | head -1 | awk '{print $2}' | tr 'A-F' 'a-f' || true)"
ASSET_URL="$(printf '%s' "$RELEASE" | grep -oE '"browser_download_url": *"[^"]*"' | grep "${ASSET_PREFIX}.*\.zip" | head -1 | sed -E 's/.*: *"([^"]*)"/\1/' || true)"

[ -n "$DIGEST" ]    || fail "release ${TAG} carries no SBT-DIGEST (unsigned) — refusing."
[ -n "$SIGNATURE" ] || fail "release ${TAG} carries no SBT-SIGNATURE (unsigned) — refusing."
[ -n "$ASSET_URL" ] || fail "release ${TAG} has no ${ASSET_PREFIX}<tag>.zip asset."

echo "Installing ${OWNER}/${REPO} ${TAG} -> ${DIR}"
echo "  asset: ${ASSET_URL}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ZIP="$TMP/app.zip"
curl -fsSL --retry 3 "$ASSET_URL" -o "$ZIP" || fail "failed to download the release asset."

# ==== integrity: sha256 + Ed25519 signature ====
ACTUAL="$(sha256_of "$ZIP" | tr 'A-F' 'a-f')"
[ "$ACTUAL" = "$DIGEST" ] || fail "sha256 mismatch (got ${ACTUAL}, expected ${DIGEST}) — refusing."

printf '%s' "$DIGEST" > "$TMP/msg"
printf '%s' "$SIGNATURE" | hex2bin > "$TMP/sig.bin"
if ! verify_ed25519 "$TMP/msg" "$TMP/sig.bin" "$PUBKEY_HEX"; then
  fail "Ed25519 signature verification FAILED — refusing to install (possible tampering)."
fi
info "sha256 + Ed25519 signature verified"

# ==== unpack ====
STAGE="$TMP/stage"
mkdir -p "$STAGE"
if command -v unzip >/dev/null 2>&1; then
  unzip -q "$ZIP" -d "$STAGE"
else
  python3 -c 'import sys,zipfile;zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$ZIP" "$STAGE"
fi
[ -f "$STAGE/main.py" ] || fail "release payload is missing main.py."

# ==== install ====
mkdir -p "$DIR"
if [ -n "$(ls -A "$DIR")" ] && [ "$UPDATE" != "yes" ]; then
  fail "${DIR} already has files — re-run with --update to refresh the signed app in place, or pick a fresh --dir."
fi
if [ "$UPDATE" = "yes" ]; then
  # replace ONLY the shipped app files; venv/ and user files are left untouched
  rm -rf "$DIR/sbt"
  rm -f "$DIR/main.py" "$DIR/run.sh" "$DIR/setup.sh" "$DIR/setup.bat" "$DIR/requirements.txt" "$DIR/EULA.txt"
fi
cp -R "$STAGE"/. "$DIR"/
info "app files installed to ${DIR}"

# ==== dependencies + desktop launcher ====
if [ -f "$DIR/setup.sh" ]; then
  ( cd "$DIR" && bash setup.sh )
else
  warn "setup.sh not in this release — skipping dependency install (Python 3.8+ required)."
fi

echo ""
echo -e "${GREEN}Install complete:${NC} ${DIR}"
echo ""
echo "Run the bot:"
echo "  1. cd ${DIR} && ./run.sh"
echo "  2. Isolated profile (separate exchange): ./run.sh --config-dir ~/my-other-bot"
echo ""
echo "Update later:"
echo "  curl -fsSL https://raw.githubusercontent.com/${OWNER}/${REPO}/main/install.sh | bash"
echo ""