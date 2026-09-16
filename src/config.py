"""Paths and defaults for the whole project.

These constants used to live next to where they were used (`cli.py`,
`session.py`, `scripts/build_categories.py`) and got duplicated as string
literals. Now they're collected here, with directories resolved from the repo
root — so commands work from any working directory, not just the root.

The path convention is the same for every option: a **bare name**
(`site1.txt`) goes into the default directory, **any path with a slash or
tilde** (`./site1.txt`, `~/tmp/a.txt`, `/abs/x.txt`) is used as-is.
"""

from __future__ import annotations

from pathlib import Path

# --- Project directories -----------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DB_DIR = PROJECT_ROOT / "db"
ENV_FILE = PROJECT_ROOT / ".env"

# --- Database -----------------------------------------------------------------

DEFAULT_DB_NAME = "database.db"
DEFAULT_DB_PATH = DB_DIR / DEFAULT_DB_NAME

# --- Exports --------------------------------------------------------------

# The tilde is expanded at write time, not on import.
DEFAULT_EXPORT_DIR = Path("~/Exports/scrapy")
DEFAULT_EXPORT_FILE = "export.txt"
DEFAULT_MODELS_EXPORT_FILE = "export-models.txt"
# The category generator writes three more files next to the main one (tag
# map, review csv, stats), so it gets its own subdirectory.
DEFAULT_CATEGORIES_DIR = DEFAULT_EXPORT_DIR / "categories"
DEFAULT_CATEGORIES_FILE = "categories.txt"

# --- Longtail phrases ---------------------------------------------------------

# Looked up in CONFIG_DIR. The file may not exist — export just skips mixing
# in phrases then (unlike an explicitly given missing file, that's not an error).
DEFAULT_LONGTAILS_FILE = "longtails.txt"

# --- Scraping ----------------------------------------------------------------

DEFAULT_RPS = 8.0
DEFAULT_CONCURRENCY = 12
DEFAULT_MODEL_CONCURRENCY = 6

# --- AI (translation and category tagging) --------------------------------------

DEFAULT_AI_API_URL = "https://api.hydraai.ru/v1/chat/completions"
DEFAULT_AI_MODEL = "deepseek-v4-flash"

# --- Category generator ----------------------------------------------------

DEFAULT_NICHE = "adult video tags"
DEFAULT_AI_BATCH_SIZE = 8
DEFAULT_MIN_VIDEO_COUNT = 5
DEFAULT_SIMILARITY = 0.82
DEFAULT_CONFIDENCE = 0.85


def _is_explicit_path(value: str) -> bool:
    """Tells a path apart from a bare name.

    Checks the raw string, not `Path.parent`: otherwise `./x.txt` (explicitly
    "current directory") would be indistinguishable from `x.txt` and would
    end up in the default directory.
    """
    return "/" in value or value.startswith("~")


def _resolve(value: str | Path, default_dir: Path, suffix: str | None = None) -> Path:
    text = str(value)
    path = Path(text).expanduser() if _is_explicit_path(text) else default_dir.expanduser() / text
    if suffix and not path.suffix:
        path = path.with_suffix(suffix)
    return path


def resolve_output_path(value: str | Path) -> Path:
    """Export path: a bare name goes into DEFAULT_EXPORT_DIR, no extension defaults to `.txt`."""
    return _resolve(value, DEFAULT_EXPORT_DIR, suffix=".txt")


def resolve_categories_path(value: str | Path) -> Path:
    """Categories file path: a bare name goes into DEFAULT_CATEGORIES_DIR."""
    return _resolve(value, DEFAULT_CATEGORIES_DIR, suffix=".txt")


def resolve_db_path(value: str | Path) -> Path:
    """DB path: a bare name is looked up in db/, no extension defaults to `.db`."""
    return _resolve(value, DB_DIR, suffix=".db")


def resolve_config_path(value: str | Path) -> Path:
    """Settings file path (longtails, seed categories): a bare name is looked up in config/."""
    return _resolve(value, CONFIG_DIR)


def display_path(path: str | Path) -> str:
    """Short path for display: relative inside the project, `~`-prefixed inside the home dir."""
    path = Path(path)
    for base, prefix in ((PROJECT_ROOT, ""), (Path.home(), "~/")):
        try:
            return f"{prefix}{path.relative_to(base)}"
        except ValueError:
            continue
    return str(path)
