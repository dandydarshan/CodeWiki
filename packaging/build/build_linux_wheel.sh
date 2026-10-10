#!/usr/bin/env bash
# ============================================================
#  CodeWiki - compiled Linux wheel build (manylinux, source-hidden)
#  Location: packaging/build/build_linux_wheel.sh
#  Run in WSL or Linux with Podman (or Docker):
#      ./packaging/build/build_linux_wheel.sh
#  Output: dist-linux/nc_codewiki-<ver>-cp312-...-manylinux...x86_64.whl
#
#  Overrides (env vars):
#    WORK=...                  scratch build dir   (default ~/codewiki-linux-build)
#    CIBW_ENV=...              cibuildwheel venv   (default ~/cibw-env)
#    CIBW_CONTAINER_ENGINE=... podman | docker     (default podman)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORK="${WORK:-$HOME/codewiki-linux-build}"
CIBW_ENV="${CIBW_ENV:-$HOME/cibw-env}"
ENGINE="${CIBW_CONTAINER_ENGINE:-podman}"

echo "Repo:   $REPO_ROOT"
echo "Work:   $WORK"
echo "Engine: $ENGINE"

# Safety: never rsync --delete onto the repo itself
if [ "$(realpath -m "$WORK")" = "$REPO_ROOT" ]; then
    echo "ERROR: WORK must not be the repo root." >&2; exit 1
fi
for tool in rsync "$ENGINE" python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' not found. Install it and re-run." >&2; exit 1; }
done

echo "=== [1/7] Ensure cibuildwheel venv exists ==="
if [ ! -d "$CIBW_ENV" ]; then
    python3 -m venv "$CIBW_ENV"
    # shellcheck disable=SC1091
    source "$CIBW_ENV/bin/activate"
    pip install --upgrade pip
    pip install cibuildwheel
else
    # shellcheck disable=SC1091
    source "$CIBW_ENV/bin/activate"
fi

echo "=== [2/7] Sync a clean source copy into $WORK ==="
mkdir -p "$WORK"
rsync -a --delete \
  --exclude 'buildenv/' --exclude 'mac-buildenv/' --exclude 'testenv*/' \
  --exclude 'dist/' --exclude 'dist-linux/' --exclude 'build/' \
  --exclude 'build_cython/' --exclude '_wheel/' --exclude 'wheelhouse/' \
  --exclude '__pycache__/' --exclude '*.pyd' --exclude '*.so' \
  --exclude '*.c' --exclude '.git/' \
  "$REPO_ROOT/" "$WORK/"

cd "$WORK"

echo "=== [3/7] Ensure cython is in build-system.requires ==="
if ! grep -q '"cython"' pyproject.toml; then
    echo "   NOTE: adding cython to build-system.requires in the build copy."
    echo "   (Add it to the repo's pyproject.toml too so this step becomes a no-op.)"
    sed -i 's/requires = \["setuptools>=68.0.0", "wheel"\]/requires = ["setuptools>=68.0.0", "wheel", "cython"]/' pyproject.toml
fi
grep -A2 '\[build-system\]' pyproject.toml

echo "=== [4/7] Using cibuildwheel config: $SCRIPT_DIR/cibw.toml ==="
cp "$SCRIPT_DIR/cibw.toml" "$WORK/cibw.toml"

echo "=== [5/7] Build the wheel (compiles inside manylinux container) ==="
rm -rf wheelhouse
CIBW_CONTAINER_ENGINE="$ENGINE" cibuildwheel --config-file cibw.toml --output-dir wheelhouse .

echo "=== [6/7] Strip source (.py that has a compiled .so sibling) ==="
cd wheelhouse
python3 "$SCRIPT_DIR/strip_wheel.py" '*.whl'

echo "=== [7/7] Copy wheels back to the repo ==="
mkdir -p "$REPO_ROOT/dist-linux"
cp ./*.whl "$REPO_ROOT/dist-linux/"

echo
echo "=== DONE ==="
echo "Wheels copied to: $REPO_ROOT/dist-linux/"
echo "Expected readable source: the pydantic model files + templates/*.py only."
echo "If a core logic module appears, add it to EXCLUDE in setup.py and re-run."
