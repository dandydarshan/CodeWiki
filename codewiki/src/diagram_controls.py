"""Shared diagram zoom / pan / fullscreen assets.

Both documentation viewers embed the same controls:

* the GitHub Pages export (``codewiki/templates/github_pages/viewer_template.html``),
  which must stay a single self-contained file
* the web app docs view (``codewiki/src/fe/templates.py``)

Keeping the CSS and JS in ``codewiki/templates/assets/`` and loading them from
here means the two viewers cannot drift apart.
"""

from functools import lru_cache
from pathlib import Path

ASSETS_DIR = Path(__file__).parent.parent / "templates" / "assets"


@lru_cache(maxsize=None)
def _read_asset(name: str) -> str:
    path = ASSETS_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        # A missing asset must not take the whole viewer down; the diagrams
        # simply render without controls, exactly as they did before.
        return ""


def diagram_controls_css() -> str:
    """CSS for the diagram toolbar, viewport and fullscreen states."""
    return _read_asset("diagram_controls.css")


def diagram_controls_js() -> str:
    """JS defining ``window.CodeWikiDiagrams.enhanceAll()``."""
    return _read_asset("diagram_controls.js")
