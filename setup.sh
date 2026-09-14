#!/usr/bin/env bash
# Simple Bot Trader — Setup Script (Linux / macOS)
# Detects OS, checks Python 3, installs dependencies, creates a desktop launcher.
# Usage:  bash setup.sh
set -e

APP_NAME="Simple Bot Trader"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$APP_DIR/venv"
REQ_FILE="$APP_DIR/requirements.txt"

# --- Colors ----------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# --- OS detection ----------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
    Linux*)  PLATFORM="linux";;
    Darwin*) PLATFORM="macos";;
    *)       fail "Unsupported OS: $OS (this script supports Linux and macOS)";;
esac
echo "Detected platform: $PLATFORM"

# --- Python check ----------------------------------------------------------
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ver=$("$cmd" --version 2>&1 | awk '{print $2}')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 8 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo ""
    fail "Python 3.8+ is required but not found.\n
  Install Python:
    Linux:  sudo apt install python3 python3-venv python3-pip   (Debian/Ubuntu)
            sudo dnf install python3 python3-pip                (Fedora)
    macOS:  brew install python3    (Homebrew)
            https://www.python.org/downloads/"
fi
info "Found $PYTHON ($($PYTHON --version 2>&1))"

# --- Virtual environment ---------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
    info "Virtual environment created at $VENV_DIR"
else
    info "Virtual environment already exists"
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# --- Install dependencies --------------------------------------------------
echo "Installing dependencies from requirements.txt..."
pip install --upgrade pip -q
pip install -r "$REQ_FILE" -q
info "Dependencies installed"

# --- Make run.sh executable ------------------------------------------------
chmod +x "$APP_DIR/run.sh"
info "run.sh is executable"

# --- Desktop launcher (Linux) ----------------------------------------------
if [ "$PLATFORM" = "linux" ]; then
    DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$DESKTOP_DIR"
    DESKTOP_FILE="$DESKTOP_DIR/simple-bot-trader.desktop"
    # Use the app's shipped icon
    ICON_LINE=""
    if [ -f "$APP_DIR/sbt/ui/styles/icon.png" ]; then
        ICON_LINE="Icon=$APP_DIR/sbt/ui/styles/icon.png"
    fi
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=$APP_NAME
Comment=Cryptocurrency trading bot
Exec=$APP_DIR/run.sh
$ICON_LINE
Terminal=false
Type=Application
Categories=Finance;Office;
Keywords=crypto;trading;bot;
EOF
    chmod +x "$DESKTOP_FILE"
    info "Desktop launcher created: $DESKTOP_FILE"
fi

# --- Desktop launcher (macOS .command file) --------------------------------
if [ "$PLATFORM" = "macos" ]; then
    CMD_FILE="$HOME/Desktop/Simple Bot Trader.command"
    cat > "$CMD_FILE" <<EOF
#!/usr/bin/env bash
cd "$APP_DIR"
./run.sh
EOF
    chmod +x "$CMD_FILE"
    info "Desktop launcher created: $CMD_FILE"
fi

# --- Done ------------------------------------------------------------------
echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo "To run the bot:"
echo "  1. Open a terminal in: $APP_DIR"
echo "  2. Run: ./run.sh"
if [ "$PLATFORM" = "linux" ]; then
    echo "  Or find \"$APP_NAME\" in your application menu."
fi
if [ "$PLATFORM" = "macos" ]; then
    echo "  Or double-click \"Simple Bot Trader.command\" on your Desktop."
fi
echo ""
echo "To run with an isolated profile (separate exchange):"
echo "  ./run.sh --config-dir ~/my-other-bot"
echo ""
