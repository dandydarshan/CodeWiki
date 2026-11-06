import os
import json
import threading
from typing import Any, Optional, Dict


# ------------------------------------------------------------
# ---------------------- File Manager ---------------------
# ------------------------------------------------------------


class FileManager:
    """Handles file I/O operations."""

    @staticmethod
    def ensure_directory(path: str) -> None:
        """Create directory if it doesn't exist."""
        os.makedirs(path, exist_ok=True)

    @staticmethod
    def save_json(data: Any, filepath: str, indent: int = 4) -> None:
        """Save data as JSON to file (atomically, so readers never see half a file)."""
        tmp_path = f"{filepath}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=indent, ensure_ascii=False)
            os.replace(tmp_path, filepath)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    @staticmethod
    def load_json(filepath: str) -> Optional[Dict[str, Any]]:
        """Load JSON from file, return None if file doesn't exist."""
        if not os.path.exists(filepath):
            return None

        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def save_text(content: str, filepath: str) -> None:
        """Save text content to file."""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    @staticmethod
    def load_text(filepath: str) -> str:
        """Load text content from file."""
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()


file_manager = FileManager()


def portable_repo_path(repo_path: str, docs_dir: str) -> Optional[str]:
    """``repo_path`` relative to ``docs_dir`` (usually ``..``), for files published with the docs.

    An absolute path would reveal where the checkout lives (``C:/Users/<name>/...``).
    None when no relative path exists (another drive on Windows).
    """
    try:
        rel = os.path.relpath(os.path.abspath(repo_path), os.path.abspath(docs_dir))
    except ValueError:
        return None
    return rel.replace(os.sep, "/")
