#!/usr/bin/env bash
# ============================================================
#  CodeWiki installer (macOS, Apple Silicon or Intel)
#  Checks prerequisites, then installs nc-codewiki from PyPI with uv,
#  or with pip into ~/.codewiki-venv if you'd rather not use uv.
#  Run:  ./install_codewiki_mac.sh
#
#  nc_codewiki-*.whl files placed next to this script are used too
#  (e.g. a pre-release build); otherwise everything comes from PyPI.
# ============================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Wheels are published for these Python versions only.
PY_RANGE=">=3.12,<3.15"
FAIL=0

echo
echo "==========================================================="
echo "  CodeWiki installer (macOS)"
echo "==========================================================="
echo

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: this installer is for macOS (found $(uname -s))."
    echo "       On Linux use install_codewiki_linux.sh."
    exit 1
fi
case "$(uname -m)" in
    arm64 | x86_64) ;;
    *) echo "ERROR: unsupported Mac architecture $(uname -m)."; exit 1 ;;
esac

# ---- 1. git -------------------------------------------------
echo "[1/4] Checking for git ..."
# /usr/bin/git exists even without the Command Line Tools (it just prompts to
# install them), so check that git actually runs.
if git --version >/dev/null 2>&1; then
    echo "   OK: $(git --version)"
else
    echo "   MISSING: git not found. CodeWiki needs it to analyze repositories."
    echo "      Install the Command Line Tools:  xcode-select --install"
    echo "      or with Homebrew:                brew install git"
    FAIL=1
fi

# ---- 2. Node.js / npm ---------------------------------------
echo "[2/4] Checking for Node.js / npm ..."
if command -v npm >/dev/null 2>&1; then
    echo "   OK: node $(node --version 2>/dev/null), npm $(npm --version 2>/dev/null)"
else
    echo "   MISSING: Node.js / npm not found."
    echo "      A dependency (PythonMonkey's pminit) runs npm during install."
    echo "      Install Node.js LTS from https://nodejs.org"
    echo "      or with Homebrew:  brew install node"
    FAIL=1
fi

if [ "$FAIL" = "1" ]; then
    echo
    echo "==========================================================="
    echo "  Cannot continue - fix the MISSING items above and re-run."
    echo "==========================================================="
    exit 1
fi

# ---- 3. uv, or pip as a fallback ----------------------------
echo "[3/4] Choosing how to install (uv or pip) ..."
ORIG_PATH="$PATH"
# uv's installer and the pip launcher both use ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"
METHOD=""
if command -v uv >/dev/null 2>&1; then
    METHOD="uv"
    echo "   OK: using $(uv --version)"
else
    echo "   uv (Python package manager, https://docs.astral.sh/uv/) is not installed."
    echo "   It installs CodeWiki in its own environment and fetches a suitable Python."
    echo "   Without it, CodeWiki is installed with pip instead."
    answer=""
    if [ -t 0 ]; then
        read -r -p "   Install uv now with its official installer? [y/N] " answer
    fi
    case "$answer" in
        [yY]*)
            if curl -LsSf https://astral.sh/uv/install.sh | sh && command -v uv >/dev/null 2>&1; then
                METHOD="uv"
                echo "   OK: using $(uv --version)"
            else
                echo "   uv install failed - falling back to pip."
            fi
            ;;
    esac
fi

if [ -z "$METHOD" ]; then
    # pip needs a Python version that has a published build.
    PYEXE=""
    for v in 3.14 3.13 3.12; do
        if command -v "python$v" >/dev/null 2>&1; then
            PYEXE="python$v"
        elif command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 | grep -q "^Python ${v//./\\.}\."; then
            PYEXE="python3"
        fi
        [ -n "$PYEXE" ] && break
    done
    if [ -z "$PYEXE" ]; then
        echo "   MISSING: pip install needs Python 3.12, 3.13 or 3.14, and none was found."
        echo "      Install it from https://www.python.org/downloads/macos/"
        echo "      or with Homebrew:  brew install python@3.12"
        echo "      Or install uv (curl -LsSf https://astral.sh/uv/install.sh | sh),"
        echo "      which downloads a suitable Python itself. Then re-run this script."
        exit 1
    fi
    METHOD="pip"
    echo "   OK: using pip with $($PYEXE --version)"
fi

# ---- 4. install ---------------------------------------------
echo "[4/4] Installing CodeWiki with $METHOD ..."
echo
if [ "$METHOD" = "uv" ]; then
    uv tool install --upgrade --python "$PY_RANGE" --find-links "$HERE" nc-codewiki \
        || { echo "ERROR: install failed (see output above)."; exit 1; }
    # Put uv's tool directory on PATH in future shells (no-op if already there).
    uv tool update-shell >/dev/null 2>&1 || true
    CODEWIKI="$(uv tool dir --bin)/codewiki"
    UPGRADE="uv tool upgrade nc-codewiki"
    UNINSTALL="uv tool uninstall nc-codewiki"
else
    # A dedicated venv: Homebrew's Python blocks system-wide pip installs (PEP 668).
    VENV="$HOME/.codewiki-venv"
    echo "Creating virtual environment at $VENV ..."
    # --clear: start fresh, so re-running with a different Python version works.
    "$PYEXE" -m venv --clear "$VENV" || {
        echo "ERROR: could not create the venv with $PYEXE. See the output above."
        exit 1
    }
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --upgrade --find-links "$HERE" nc-codewiki \
        || { echo "ERROR: install failed (see output above)."; exit 1; }
    # Make 'codewiki' runnable without activating the venv.
    mkdir -p "$HOME/.local/bin"
    ln -sf "$VENV/bin/codewiki" "$HOME/.local/bin/codewiki"
    CODEWIKI="$HOME/.local/bin/codewiki"
    UPGRADE="re-run this installer"
    UNINSTALL="rm -rf ~/.codewiki-venv ~/.local/bin/codewiki"
fi

echo
echo "Verifying ..."
if "$CODEWIKI" --version; then
    echo "   OK: codewiki works."
else
    echo "   WARNING: 'codewiki --version' did not run cleanly - check the output above."
fi

echo
echo "==========================================================="
echo "  Done. Open a new terminal, then run:  codewiki --help"
echo "  Upgrade later:  $UPGRADE"
echo "  Uninstall:      $UNINSTALL"
if [ "$METHOD" = "pip" ]; then
    case ":$ORIG_PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *)
            echo
            echo "  If 'codewiki' is not found, add ~/.local/bin to your PATH:"
            echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
            ;;
    esac
fi
echo "==========================================================="
