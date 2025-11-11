import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import HTTPException, Request, Depends
from fastapi.security import APIKeyHeader
from sec.key_helpers import load_api_keys, verify_secret
from sec.key_helpers import persist_named_key as kh_persist_named_key

API_KEYS_FILE = Path("sec/users.key")
ADMIN_KEY_FILE = Path("sec/admin.key")
API_KEY_HEADER_NAME = "X-API-Key"
ADMIN_KEY_HEADER_NAME = "X-API-Admin-Key"
API_PROJECT_HEADER_NAME = "X-API-Project"

def load_admin_key(file: Path) -> Dict[str, str]:
    if not file.exists():
        raise FileNotFoundError(f"Admin key file not found: {file}")
    try:
        data = json.loads(file.read_text())
    except Exception as e:
        raise ValueError(f"Failed to parse admin_key json: {e}")
    if not isinstance(data, dict) or not all(k in data for k in ("name","hash")):
        raise ValueError("Admin key file must be JSON with keys: name, hash")
    return {"name": str(data["name"]), "hash": str(data["hash"]) }


ADMIN_KEY = load_admin_key(ADMIN_KEY_FILE)
API_KEYS = load_api_keys(API_KEYS_FILE)

api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


def verify_secret_against_entry(secret: str, entry: Dict[str, Any]) -> bool:
    stored = entry.get("hash")
    return verify_secret(secret, stored)


def _record_key_usage(name: str, entry_snapshot: Dict[str, Any]) -> None:
    """Persist last_used timestamp for the given API key."""
    timestamp = datetime.now(timezone.utc).isoformat()

    if API_KEYS_FILE.exists():
        try:
            data = json.loads(API_KEYS_FILE.read_text())
            keys_list = data.get("api_keys") if isinstance(data.get("api_keys"), list) else []
            updated = False
            for item in keys_list:
                if isinstance(item, dict) and item.get("name") == name:
                    item["last_used"] = timestamp
                    updated = True
                    break
            if updated:
                data["api_keys"] = keys_list
                API_KEYS_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            # If persisting fails we still continue, auth should not break
            pass

    merged = dict(entry_snapshot or {})
    merged["last_used"] = timestamp
    global API_KEYS
    API_KEYS[name] = merged


def require_api_key(api_key: str = Depends(api_key_header)) -> Dict[str, Any]:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API Key")
    try:
        if verify_secret_against_entry(api_key, ADMIN_KEY):
            return {"name": ADMIN_KEY.get("name", "admin"), "role": "admin", "projects": ["*"]}
    except Exception:
        pass
    for name, entry in API_KEYS.items():
        try:
            if verify_secret_against_entry(api_key, entry):
                _record_key_usage(name, entry)
                current_entry = API_KEYS.get(name, entry)
                return {
                    "name": name,
                    "role": current_entry.get("role", "user"),
                    "projects": current_entry.get("projects", []),
                    "last_used": current_entry.get("last_used"),
                }
        except Exception:
            continue
    raise HTTPException(status_code=403, detail="Invalid API Key")


def require_admin_header(request: Request) -> None:
    admin_key = request.headers.get(ADMIN_KEY_HEADER_NAME) or request.query_params.get("admin_key")
    if not admin_key:
        raise HTTPException(status_code=401, detail="Missing admin key")
    try:
        if not verify_secret_against_entry(admin_key, ADMIN_KEY):
            raise HTTPException(status_code=403, detail="Invalid admin key")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid admin key")


def persist_named_key(name: str, secret: str, file: Path, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Persist a named API key and reload the in-memory API_KEYS."""
    stored = kh_persist_named_key(name, secret, file, metadata)
    # reload
    global API_KEYS
    API_KEYS = load_api_keys(API_KEYS_FILE)
    return stored


def remove_api_key(name: str, file: Path) -> bool:
    """Remove by name from JSON-only api_keys file; returns True if removed."""
    if not file.exists():
        return False
    try:
        data = json.loads(file.read_text())
    except Exception:
        return False
    keys_list = data.get("api_keys") if isinstance(data.get("api_keys"), list) else []
    new_list = [k for k in keys_list if not (isinstance(k, dict) and k.get("name") == name)]
    if len(new_list) == len(keys_list):
        return False
    data["api_keys"] = new_list
    file.write_text(json.dumps(data, indent=2))
    # reload
    global API_KEYS
    API_KEYS = load_api_keys(API_KEYS_FILE)
    return True


def reload_api_keys(file: Path) -> None:
    global API_KEYS
    API_KEYS = load_api_keys(file)
