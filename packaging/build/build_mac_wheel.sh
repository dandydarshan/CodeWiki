#!/usr/bin/env bash
# ============================================================
#  CodeWiki - compiled macOS wheel build (source-hidden)
#  Location: packaging/build/build_mac_wheel.sh
#  RUN THIS ON A MAC:   ./packaging/build/build_mac_wheel.sh
#  Compiles for the architectures the Python was built for. With a
#  python.org (or GitHub setup-python) Python that is universal2, so the
#  wheel runs on both Apple Silicon and Intel Macs. A single-arch Python
#  (e.g. Homebrew's) gives a wheel for that architecture only.
#  Output: wheelhouse/nc_codewiki-<ver>-cp312-cp312-macosx_*_universal2.whl
#
#  Override: PYVER=3.13 ./packaging/build/build_mac_wheel.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYVER="${PYVER:-3.12}"
cd "$REPO"

echo "=== CodeWiki macOS wheel build ==="
echo "Repo:   $REPO"
echo "Arch:   $(uname -m)"
echo "Python: $PYVER"
echo

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: this script must run on macOS." >&2; exit 1
fi

# ---- 0. Prerequisite checks --------------------------------
FAIL=0
echo "[0/7] Checking prerequisites ..."

if xcode-select -p >/dev/null 2>&1 && command -v clang >/dev/null 2>&1; then
    echo "   OK: $(clang --version | head -1)"
else
    echo "   MISSING: Xcode Command Line Tools.  Install: xcode-select --install"; FAIL=1
fi

PYEXE=""
if command -v "python$PYVER" >/dev/null 2>&1; then
    PYEXE="python$PYVER"
elif command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 | grep -q "$PYVER\."; then
    PYEXE="python3"
fi
if [ -z "$PYEXE" ]; then
    echo "   MISSING: Python $PYVER.  Install from python.org or: brew install python@$PYVER"; FAIL=1
else
    echo "   OK: $($PYEXE --version)"
fi

command -v git >/dev/null 2>&1 && echo "   OK: $(git --version)" \
    || { echo "   MISSING: git.  Install: xcode-select --install  (or brew install git)"; FAIL=1; }

command -v npm >/dev/null 2>&1 && echo "   OK: node $(node --version), npm $(npm --version)" \
    || { echo "   MISSING: Node.js/npm.  Install from nodejs.org or: brew install node"; FAIL=1; }

[ -f setup.py ] || { echo "   MISSING: setup.py in $REPO"; FAIL=1; }

if [ "$FAIL" = "1" ]; then echo; echo "Fix the MISSING items above and re-run."; exit 1; fi

echo "[1/7] Creating build venv ..."
rm -rf mac-buildenv
$PYEXE -m venv mac-buildenv
# shellcheck disable=SC1091
source mac-buildenv/bin/activate
python -m pip install --upgrade pip >/dev/null

echo "[2/7] Installing build tools + project dependencies ..."
pip install cython build wheel setuptools delocate
pip install .

echo "[3/7] Cleaning previous artifacts ..."
rm -rf build build_cython _wheel dist wheelhouse
find codewiki -name '*.so' -delete 2>/dev/null || true
find codewiki -name '*.c'  -delete 2>/dev/null || true

echo "[4/7] Compiling with Cython + clang ..."
export CODEWIKI_CYTHONIZE=1
python setup.py build_ext --inplace

echo "[5/7] Building the wheel ..."
python -m build --wheel --no-isolation
RAW_WHEEL=$(ls dist/*.whl | head -1)

echo "[6/7] Repairing with delocate (bundles dylibs, sets macOS tag) ..."
mkdir -p wheelhouse
delocate-wheel -w wheelhouse -v "$RAW_WHEEL"

echo "[7/7] Stripping source (.py that has a compiled .so sibling) ..."
python "$SCRIPT_DIR/strip_wheel.py" "wheelhouse/*.whl"
WHEEL=$(cd wheelhouse && ls ./*.whl | head -1)
WHEEL=${WHEEL#./}

echo
echo "=== DONE ==="
echo "macOS wheel: $REPO/wheelhouse/$WHEEL   ($(uname -m))"
echo "Test it:  $PYEXE -m venv /tmp/cwtest && source /tmp/cwtest/bin/activate"
echo "          pip install $REPO/wheelhouse/$WHEEL && codewiki --help"
