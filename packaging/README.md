# Packaging

Builds source-hidden (Cython-compiled) CodeWiki wheels.
Every script finds the repo root from its own location, so they work from any checkout path.

## Layout

| File | Purpose |
|---|---|
| `../setup.py` (repo root) | Cython compile config, active only when `CODEWIKI_CYTHONIZE=1` (the build scripts set it). `EXCLUDE` lists modules shipped as source (pydantic models — they break when compiled). |
| `build/build_wheel.bat` | **Windows** wheel. Run from cmd. Finds MSVC via `vswhere`. |
| `build/build_linux_wheel.sh` | **Linux** manylinux wheel via cibuildwheel + Podman/Docker. Run in WSL or Linux. |
| `build/cibw.toml` | cibuildwheel config used by the Linux build. |
| `build/strip_wheel.py` | Shared by all build scripts: removes `.py` files that have a compiled sibling and rewrites the wheel's `RECORD`. |
| `build/build_mac_wheel.sh` | **macOS** wheel. Must run on a Mac. With a python.org Python it builds one universal2 wheel (Apple Silicon + Intel). |
| `install/install_codewiki.bat` | End-user installer (Windows): checks git/npm, installs with uv (offers to install it) or falls back to pip. |
| `install/install_codewiki_linux.sh` | Same, for Linux. |
| `install/install_codewiki_mac.sh` | Same, for macOS. |
| `install/INSTALL.md` | End-user install guide. |
| `install/QUICKSTART.md` | End-user configure-and-generate guide. |

## Build

| Platform | Command | Output |
|---|---|---|
| Windows | `packaging\build\build_wheel.bat` | `dist\*.whl` |
| Linux | `./packaging/build/build_linux_wheel.sh` | `dist-linux/*.whl` |
| macOS | `./packaging/build/build_mac_wheel.sh` | `wheelhouse/*.whl` |

Python version defaults to 3.12. Override per run: `set PYVER=3.13` (Windows) or `PYVER=3.13 ./...` (macOS).
For Linux, edit the `build =` line in `build/cibw.toml`.

## Prerequisites

- **All:** Python matching `PYVER`, git, Node.js/npm.
- **Windows:** Visual Studio Build Tools with "Desktop development with C++".
- **Linux:** Podman (or Docker), rsync.
- **macOS:** Xcode Command Line Tools (`xcode-select --install`).

## After building

Each script prints the source files still readable in the wheel. Expected: the
`EXCLUDE`d pydantic model files plus `templates/*.py`. If a core module appears,
add it to `EXCLUDE` in `setup.py` and rebuild. Then test the wheel in a clean venv
with `codewiki --help`.

Wheels are not committed. Attach them to a release or put them on a shared drive.
