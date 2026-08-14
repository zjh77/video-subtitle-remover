from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
API_ROOT = APP_DIR.parent
PROJECT_ROOT = API_ROOT.parent
CONFIG_PATH = API_ROOT / "config.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8020},
    "storage": {"data_root": "./data"},
}


def _load() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return DEFAULT_CONFIG
    with CONFIG_PATH.open(encoding="utf-8") as f:
        loaded = json.load(f)
    return {section: {**DEFAULT_CONFIG[section], **loaded.get(section, {})} for section in DEFAULT_CONFIG}


RAW_CONFIG = _load()
SERVER_HOST = str(os.getenv("VSR_API_HOST", RAW_CONFIG["server"]["host"]))
SERVER_PORT = int(os.getenv("VSR_API_PORT", str(RAW_CONFIG["server"]["port"])))
DATA_ROOT = Path(os.getenv("VSR_API_DATA_ROOT", str(RAW_CONFIG["storage"]["data_root"]))).resolve()
ASSETS_ROOT, JOBS_ROOT, DB_PATH = DATA_ROOT / "assets", DATA_ROOT / "jobs", DATA_ROOT / "app.db"


def ensure_data_dirs() -> None:
    ASSETS_ROOT.mkdir(parents=True, exist_ok=True)
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)


def write_default_config() -> None:
    if not CONFIG_PATH.exists():
        # Runtime configuration is intentionally local-only and ignored by Git.
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
