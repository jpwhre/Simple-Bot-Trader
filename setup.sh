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
# Supported: Python 3.9+ (current released Python incl. 3.14 works; the
# requirements pins gate coincurve off on >= 3.14 exactly like ccxt does).
# Prefer the interpreter the release was validated on, then any >= 3.9.
PYTHON=""
PYVER=""
for cmd in python3.12 python3.13 python3.11 python3.10 python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ver=$("$cmd" --version 2>&1 | awk '{print $2}')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
            PYTHON="$cmd"
            PYVER="$ver"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo ""
    fail "Python 3.9+ is required but not found.\n
  Install Python:
    Linux:  sudo apt install python3 python3-venv python3-pip   (Debian/Ubuntu)
            sudo dnf install python3 python3-pip                (Fedora)
    macOS:  brew install python3    (Homebrew)
            https://www.python.org/downloads/"
fi
info "Found $PYTHON ($PYVER)"

# --- Virtual environment ---------------------------------------------------
if [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/python" ]; then
    OLD_VER="$("$VENV_DIR/bin/python" --version 2>&1 | awk '{print $2}' || true)"
    if [ -n "$OLD_VER" ] && [ "$OLD_VER" != "$PYVER" ]; then
        echo "Existing venv was built with Python $OLD_VER; selected is $PYVER. Recreating..."
        rm -rf "$VENV_DIR"
    fi
fi
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

# --- Qt xcb system prerequisites (Linux) -----------------------------------
# PyQt5's xcb platform plugin needs a handful of SYSTEM libraries (the wheel
# only bundles the plugin itself). The notorious one on fresh Ubuntu/Mint is
# `libxcb-cursor0` (Qt 5.15.4+); `libxkbcommon-x11-0` is commonly missing too.
# We diff the installed plugin's dynamic deps against ldconfig and offer to
# install exactly what's missing, so a fresh install never hits the
# "Could not load the Qt platform plugin xcb" failure.
detect_pm() {
    if command -v apt-get >/dev/null 2>&1; then echo apt
    elif command -v dnf      >/dev/null 2>&1; then echo dnf
    elif command -v pacman   >/dev/null 2>&1; then echo pacman
    elif command -v zypper   >/dev/null 2>&1; then echo zypper
    else echo ""; fi
}

qt_pkg_apt() {  # library name -> apt package (empty = no known mapping)
    case "$1" in
        libxcb-cursor.so.0)       echo "libxcb-cursor0" ;;
        libxkbcommon-x11.so.0)    echo "libxkbcommon-x11-0" ;;
        libxcb-icccm.so.4)        echo "libxcb-icccm4" ;;
        libxcb-image.so.0)        echo "libxcb-image0" ;;
        libxcb-keysyms.so.1)      echo "libxcb-keysyms1" ;;
        libxcb-render-util.so.0)  echo "libxcb-render-util0" ;;
        libxcb-shape.so.0)        echo "libxcb-shape0" ;;
        libxcb-xkb.so.1)          echo "libxcb-xkb1" ;;
        libxcb-xinerama.so.0)     echo "libxcb-xinerama0" ;;
        libx11-xcb.so.1)          echo "libx11-xcb1" ;;
        libEGL.so.1)              echo "libegl1" ;;
        libGL.so.1)               echo "libgl1" ;;
        *) echo "" ;;
    esac
}

qt_check_system_libs() {
    [ "$PLATFORM" = "linux" ] || return 0
    command -v ldd >/dev/null 2>&1 || { warn "ldd not found — skipping Qt system-lib check"; return 0; }
    plugin="$(find "$VENV_DIR" -path '*/Qt5/plugins/platforms/libqxcb.so' 2>/dev/null | head -1)"
    [ -n "$plugin" ] || { info "Qt platform plugin not found — skipping system-lib check"; return 0; }
    mapfile -t missing < <(ldd "$plugin" 2>/dev/null | sed -n 's/=> not found$//p' | awk '{print $1}' | sort -u)
    [ "${#missing[@]}" -eq 0 ] && { info "Qt xcb platform dependencies present"; return 0; }

    warn "PyQt5's xcb platform plugin needs these system libraries, which are missing:"
    printf '     %s\n' "${missing[@]}"
    pm="$(detect_pm)"
    if [ "$pm" = "apt" ]; then
        apt_list=()
        for lib in "${missing[@]}"; do
            p="$(qt_pkg_apt "$lib")"
            [ -n "$p" ] && apt_list+=("$p")
        done
        if [ "${#apt_list[@]}" -gt 0 ]; then
            read -r -p "Install now with sudo apt-get (${apt_list[*]})? [Y/n] " ans < /dev/tty
            case "${ans:-Y}" in
                y|Y|yes)
                    sudo apt-get install -y "${apt_list[@]}" >/dev/null || {
                        warn "apt-get install failed — install these manually:"; printf '     sudo apt-get install %s\n' "${apt_list[*]}"; }
                    ;;
                *) info "Skipped (you can install later: sudo apt-get install ${apt_list[*]})" ;;
            esac
        fi
    fi
    # re-check after any install; else give a portable hint
    leftover="$(ldd "$plugin" 2>/dev/null | sed -n 's/=> not found$//p' | awk '{print $1}' | sort -u | wc -l)"
    if [ "$leftover" -gt 0 ] && [ "$pm" != "apt" ]; then
        warn "Your package manager isn't apt — install the equivalents of:"
        warn "  apt: libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-keysyms1"
        warn "  dnf: libxcb-cursor libxkbcommon-x11 xcb-util-wm xcb-util-keysyms"
        warn "  pacman: xcb-util-cursor xcb-util-keysyms xcb-util-wm libxkbcommon-x11"
    fi
}

qt_check_system_libs

# --- Return-to-state autostart (Linux systemd / macOS LaunchAgent) ----------
# Reopens the bot after a reboot/login IF it was running when the machine went
# down (main.py --if-was-launched + launched.marker). Linux needs a logged-in
# user session (auto-login) to fire at boot; otherwise it fires at login.
if [ "$PLATFORM" = "linux" ] || [ "$PLATFORM" = "macos" ]; then
    if "$VENV_DIR/bin/python" -c 'from sbt import autorun; autorun.enable_all()' 2>/dev/null; then
        info "Return-to-state autostart enabled ($PLATFORM)"
    else
        warn "Autostart could not be enabled — the bot reopens at next login only if the OS autostart is configured."
    fi
fi

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
