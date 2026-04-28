from __future__ import annotations

import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
MEMORY_DATA_DIR = DATA_DIR / "memory_data"
CANVAS_DATA_DIR = DATA_DIR / "canvas_data"

LEGACY_SESSION_FILE = DATA_DIR / "session.json"
LEGACY_MEMORIES_FILE = DATA_DIR / "memories.json"
LEGACY_MEMORY_LOG_FILE = DATA_DIR / "memory_log.jsonl"
LEGACY_MEMORY_TOMBSTONES_FILE = DATA_DIR / "memory_tombstones.json"
LEGACY_CANVAS_CONFIG_FILE = DATA_DIR / "canvas_config.json"

SESSION_FILE = MEMORY_DATA_DIR / "session.json"
MEMORIES_FILE = MEMORY_DATA_DIR / "memories.json"
MEMORY_LOG_FILE = MEMORY_DATA_DIR / "memory_log.jsonl"
MEMORY_TOMBSTONES_FILE = MEMORY_DATA_DIR / "memory_tombstones.json"

CANVAS_CONFIG_FILE = CANVAS_DATA_DIR / "canvas_config.json"
CANVAS_HISTORY_FILE = CANVAS_DATA_DIR / "canvas_history.json"


def ensure_storage_layout() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DATA_DIR.mkdir(parents=True, exist_ok=True)
    CANVAS_DATA_DIR.mkdir(parents=True, exist_ok=True)


def migrate_legacy_file(legacy_path: Path, target_path: Path) -> None:
    ensure_storage_layout()
    if target_path.exists() or not legacy_path.exists():
        return
    try:
        shutil.move(str(legacy_path), str(target_path))
    except OSError:
        try:
            shutil.copy2(str(legacy_path), str(target_path))
        except OSError:
            return


def prepare_storage_layout() -> None:
    ensure_storage_layout()
    migrate_legacy_file(LEGACY_SESSION_FILE, SESSION_FILE)
    migrate_legacy_file(LEGACY_MEMORIES_FILE, MEMORIES_FILE)
    migrate_legacy_file(LEGACY_MEMORY_LOG_FILE, MEMORY_LOG_FILE)
    migrate_legacy_file(LEGACY_MEMORY_TOMBSTONES_FILE, MEMORY_TOMBSTONES_FILE)
    migrate_legacy_file(LEGACY_CANVAS_CONFIG_FILE, CANVAS_CONFIG_FILE)
