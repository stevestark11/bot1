"""
storage.py — JSON-file storage backed on disk (Railway Volume at /data).

Coupon data:  /data/coupons.json
User data:    /data/users.json

Coupon structure:
{
  "folders": {
    "<folder_id>": {
      "id": "abc123",
      "name": "Nike June 2025",
      "created_at": "2025-06-01T10:00:00",
      "coupons": {
        "<coupon_id>": {
          "id": "xyz789",
          "code": "NIKE20",
          "description": "20% off everything",
          "created_at": "2025-06-01T10:05:00"
        }
      }
    }
  }
}

User structure:
{
  "users": {
    "<user_id>": {
      "id": 123456789,
      "username": "johndoe",
      "first_name": "John",
      "last_name": "Doe",
      "first_seen": "2025-06-01T10:00:00",
      "last_seen": "2025-06-01T10:05:00"
    }
  }
}
"""

import json
import os
import uuid
import threading
from datetime import datetime
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("STORAGE_PATH", "/data"))
DATA_FILE = DATA_DIR / "coupons.json"
USERS_FILE = DATA_DIR / "users.json"

_lock = threading.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

def _uid() -> str:
    return uuid.uuid4().hex[:10]

def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(path: Path, data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)  # atomic rename — never corrupts the file

# ── Init ──────────────────────────────────────────────────────────────────────

def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        _save(DATA_FILE, {"folders": {}})
    if not USERS_FILE.exists():
        _save(USERS_FILE, {"users": {}})
    logger.info("Storage ready — coupons: %s | users: %s", DATA_FILE, USERS_FILE)

# ── User tracking ─────────────────────────────────────────────────────────────

def track_user(user) -> None:
    """
    Upsert a Telegram user object into users.json.
    Call this on every /start and interaction so no user is ever missed.
    """
    uid = str(user.id)
    with _lock:
        data = _load(USERS_FILE)
        users = data.get("users", {})
        existing = users.get(uid)
        users[uid] = {
            "id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "first_seen": existing["first_seen"] if existing else _now(),
            "last_seen": _now(),
        }
        data["users"] = users
        _save(USERS_FILE, data)

def get_users() -> list[dict]:
    """Return all tracked users sorted by first_seen ascending."""
    data = _load(USERS_FILE)
    users = list(data.get("users", {}).values())
    users.sort(key=lambda u: u["first_seen"])
    return users

def users_file_path() -> Path:
    return USERS_FILE

# ── Folders ───────────────────────────────────────────────────────────────────

def create_folder(name: str) -> str:
    with _lock:
        data = _load(DATA_FILE)
        fid = _uid()
        data.setdefault("folders", {})[fid] = {
            "id": fid,
            "name": name,
            "created_at": _now(),
            "coupons": {},
        }
        _save(DATA_FILE, data)
    return fid

def get_folders() -> list[dict]:
    """Folders sorted newest-first, with coupon_count added."""
    data = _load(DATA_FILE)
    result = []
    for f in data.get("folders", {}).values():
        result.append({**f, "coupon_count": len(f["coupons"])})
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return result

def get_folder(folder_id: str) -> dict | None:
    return _load(DATA_FILE).get("folders", {}).get(folder_id)

def delete_folder(folder_id: str) -> None:
    with _lock:
        data = _load(DATA_FILE)
        data.get("folders", {}).pop(folder_id, None)
        _save(DATA_FILE, data)

# ── Coupons ───────────────────────────────────────────────────────────────────

def add_coupon(folder_id: str, code: str, description: str = "") -> str:
    with _lock:
        data = _load(DATA_FILE)
        folder = data.get("folders", {}).get(folder_id)
        if not folder:
            raise KeyError(f"Folder {folder_id!r} not found")
        cid = _uid()
        folder["coupons"][cid] = {
            "id": cid,
            "code": code,
            "description": description,
            "created_at": _now(),
        }
        _save(DATA_FILE, data)
    return cid

def get_coupons(folder_id: str) -> list[dict]:
    """Coupons in a folder, newest first."""
    folder = _load(DATA_FILE).get("folders", {}).get(folder_id, {})
    items = list(folder.get("coupons", {}).values())
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items

def get_all_coupons() -> list[dict]:
    """All coupons across all folders, enriched with folder_name."""
    data = _load(DATA_FILE)
    result = []
    for folder in data.get("folders", {}).values():
        for c in folder["coupons"].values():
            result.append({**c, "folder_name": folder["name"], "folder_id": folder["id"]})
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return result

def delete_coupon(coupon_id: str) -> None:
    with _lock:
        data = _load(DATA_FILE)
        for folder in data.get("folders", {}).values():
            if coupon_id in folder["coupons"]:
                del folder["coupons"][coupon_id]
                _save(DATA_FILE, data)
                return
