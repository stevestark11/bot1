"""
storage.py — JSON-backed storage on Railway Volume at /data

coupons.json  → folders + items (each item stores a Telegram file_id + type + caption)
users.json    → every user who has interacted with the bot
"""

import json
import os
import uuid
import threading
from datetime import datetime
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

DATA_DIR   = Path(os.environ.get("STORAGE_PATH", "/data"))
DATA_FILE  = DATA_DIR / "coupons.json"
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
    tmp.replace(path)

# ── Init ──────────────────────────────────────────────────────────────────────

def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        _save(DATA_FILE, {"folders": {}})
    if not USERS_FILE.exists():
        _save(USERS_FILE, {"users": {}})
    logger.info("Storage ready — data: %s | users: %s", DATA_FILE, USERS_FILE)

# ── User tracking ─────────────────────────────────────────────────────────────

def track_user(user) -> None:
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
            "items": {},
        }
        _save(DATA_FILE, data)
    return fid

def get_folders() -> list[dict]:
    data = _load(DATA_FILE)
    result = []
    for f in data.get("folders", {}).values():
        result.append({**f, "item_count": len(f.get("items", {}))})
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return result

def get_folder(folder_id: str) -> dict | None:
    return _load(DATA_FILE).get("folders", {}).get(folder_id)

def delete_folder(folder_id: str) -> None:
    with _lock:
        data = _load(DATA_FILE)
        data.get("folders", {}).pop(folder_id, None)
        _save(DATA_FILE, data)

# ── Items (files/media/text) ──────────────────────────────────────────────────

def add_item(folder_id: str, file_id: str, file_type: str, caption: str = "", file_name: str = "") -> str:
    """
    Save a reference to a Telegram file (or text message) inside a folder.

    file_type: "document" | "photo" | "video" | "audio" | "voice" |
               "video_note" | "sticker" | "animation" | "text"
    file_id:   Telegram file_id string (empty string for plain text items)
    caption:   text caption / plain text content
    file_name: original filename for documents (optional, display only)
    """
    with _lock:
        data = _load(DATA_FILE)
        folder = data.get("folders", {}).get(folder_id)
        if not folder:
            raise KeyError(f"Folder {folder_id!r} not found")
        iid = _uid()
        folder.setdefault("items", {})[iid] = {
            "id": iid,
            "file_id": file_id,
            "file_type": file_type,
            "caption": caption,
            "file_name": file_name,
            "created_at": _now(),
        }
        _save(DATA_FILE, data)
    return iid

def get_items(folder_id: str) -> list[dict]:
    folder = _load(DATA_FILE).get("folders", {}).get(folder_id, {})
    items = list(folder.get("items", {}).values())
    items.sort(key=lambda x: x["created_at"])
    return items

def get_all_items() -> list[dict]:
    data = _load(DATA_FILE)
    result = []
    for folder in data.get("folders", {}).values():
        for item in folder.get("items", {}).values():
            result.append({**item, "folder_name": folder["name"], "folder_id": folder["id"]})
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return result

def delete_item(item_id: str) -> None:
    with _lock:
        data = _load(DATA_FILE)
        for folder in data.get("folders", {}).values():
            if item_id in folder.get("items", {}):
                del folder["items"][item_id]
                _save(DATA_FILE, data)
                return

# ── Backward-compat shims (old coupon-based callers) ─────────────────────────
# These let old references to get_coupons / add_coupon / delete_coupon still work.

def get_coupons(folder_id: str) -> list[dict]:
    return get_items(folder_id)

def get_all_coupons() -> list[dict]:
    return get_all_items()

def add_coupon(folder_id: str, code: str, description: str = "") -> str:
    return add_item(folder_id, "", "text", caption=code)

def delete_coupon(item_id: str) -> None:
    return delete_item(item_id)
