from __future__ import annotations

from argon2 import PasswordHasher
import secrets
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

ph = PasswordHasher()

def generate_secret(n_bytes: int = 32) -> str:
    return secrets.token_urlsafe(n_bytes)

def hash_secret(secret: str) -> str:
    return ph.hash(secret)

def verify_secret(secret: str, hashed: str) -> bool:
    try:
        return ph.verify(hashed, secret)
    except Exception:
        return False


def _normalize_str_list(value: Any) -> List[str]:
    """Return a de-duplicated list of lower-stripped strings from several input shapes."""
    if value is None:
        return []
    if isinstance(value, str):
        candidates: Iterable[str] = value.split(",")
    elif isinstance(value, Iterable):
        candidates = value
    else:
        return []

    out: List[str] = []
    seen = set()
    for item in candidates:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(normalized)
    return out


def load_api_keys(file: Path) -> Dict[str, Dict[str, Any]]:
    """Load JSON api_keys file and return dict name -> entry

    Each entry always contains at least {"hash": str}. Optional metadata fields such as
    "projects", "notes", and "created_at" are preserved when available.
    """
    if not file.exists():
        return {}
    data = json.loads(file.read_text())
    raw = data.get("api_keys") if isinstance(data.get("api_keys"), list) else []
    out: Dict[str, Dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, dict) and "name" in item and "hash" in item:
            entry: Dict[str, Any] = {"hash": str(item["hash"])}
            projects = _normalize_str_list(item.get("projects"))
            if projects:
                entry["projects"] = projects
            if item.get("notes"):
                entry["notes"] = str(item["notes"])
            if item.get("role"):
                entry["role"] = str(item["role"]).lower()
            if item.get("created_at"):
                entry["created_at"] = str(item["created_at"])
            for extra_key, value in item.items():
                if extra_key in {"name", "hash", "projects", "notes", "role", "created_at"}:
                    continue
                entry[extra_key] = value
            out[str(item["name"])] = entry
    return out


def persist_named_key(name: str, secret: str, file: Path, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Persist a named API key into JSON-only api_keys file using argon2 hash.

    Returns the stored entry (excluding the plaintext secret) so callers can confirm
    associated metadata.
    """
    h = hash_secret(secret)
    data = {}
    if file.exists():
        try:
            data = json.loads(file.read_text())
        except Exception:
            data = {}
    keys_list = data.get("api_keys") if isinstance(data.get("api_keys"), list) else []
    by_name: Dict[str, Any] = {}
    for item in keys_list:
        if isinstance(item, dict) and "name" in item and "hash" in item:
            by_name[item["name"]] = item
    entry: Dict[str, Any] = {"name": name, "hash": h}
    metadata = metadata or {}
    projects = _normalize_str_list(metadata.get("projects"))
    if projects:
        entry["projects"] = projects
    if metadata.get("notes"):
        entry["notes"] = str(metadata["notes"])
    if metadata.get("role"):
        entry["role"] = str(metadata["role"]).lower()
    entry["created_at"] = metadata.get("created_at", datetime.now(timezone.utc).isoformat())
    for key, value in metadata.items():
        if key in {"projects", "notes", "role", "created_at"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            entry[key] = value
        else:
            entry[key] = str(value)

    by_name[name] = entry
    data["api_keys"] = list(by_name.values())
    file.write_text(json.dumps(data, indent=2))
    return entry


def persist_admin_key(name: str, secret: str, file: Path) -> None:
    """Persist admin key JSON with {name, hash}. This file is used only for admin verification."""
    h = hash_secret(secret)
    data = {"name": name, "temp_plaintext": secret, "hash": h}
    file.write_text(json.dumps(data, indent=2))
