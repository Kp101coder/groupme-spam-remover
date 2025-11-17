import json
from pathlib import Path
from typing import Any, Dict, List
import os
import subprocess
import sys
import traceback
from logs.logsys import log_and_print, LOG_FILE
import time
from fastapi import FastAPI, Request
from fastapi import HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse
import uvicorn
from threading import Thread
from sec.key_helpers import generate_secret
import ai.ai_helpers as ai
import groupme.groupme_helpers as gm
from sec.security_helpers import (
    API_KEYS,
    ADMIN_KEY,
    API_KEYS_FILE,
    API_KEY_HEADER_NAME,
    API_PROJECT_HEADER_NAME,
    ADMIN_KEY_HEADER_NAME,
    require_api_key,
    require_admin_header,
    remove_api_key,
)

SYSTEM_MESSAGE = (
    "You are a strict binary classifier for messages in the UT Austin Pickleball Club GroupMe.\n"
    "Task: Determine if a message is spam relevant to the group. Output exactly one word: Yes or No. No punctuation, no explanations.\n\n"
    "Label as Yes (spam) when the message is about buying/selling/trading tickets or passes for events unrelated to the club, especially if it includes phone numbers, 'text/DM me', prices, or payment apps (Venmo, Cash App, Zelle, PayPal). Also treat ticket giveaways/resales and bulk season tickets as spam.\n\n"
    "Label as No (not spam) for: normal conversation, club announcements, practice or event info, officer communications (including asking members to text or Venmo/Zelle for club dues/fees/merch), and posts clearly tied to official club activities.\n\n"
    "Examples:\n"
    "User: I'm selling two Sam Houston tickets, text me at (719) 555-1234.\nAssistant: Yes\n"
    "User: OU vs TX tickets available, DM if interested.\nAssistant: Yes\n"
    "User: Im selling a Mac Book Pro dm for more info.\nAssistant: Yes\n"
    "User: Please Venmo @utpickleball $30 for club dues by Friday.\nAssistant: No\n"
    "User: Practice moved to 7pm, bring water.\nAssistant: No\n"
)
last_action = None

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

admins = {a.lower() for a in gm.admin if isinstance(a, str)}
API_KEYS: Dict[str, Dict[str, Any]] = API_KEYS

# Set up global exception handler using sys.excepthook
_original_excepthook = sys.excepthook

def custom_excepthook(exc_type, exc_value, exc_traceback):
    """Log uncaught exceptions before calling the original handler."""
    if issubclass(exc_type, KeyboardInterrupt):
        # Don't log keyboard interrupts
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    tb_text = "".join(tb_lines)
    log_and_print(f"❌ Uncaught exception: {exc_type.__name__}: {exc_value}\n{tb_text}")
    
    # Call original handler
    _original_excepthook(exc_type, exc_value, exc_traceback)

sys.excepthook = custom_excepthook

# Middleware to require API key for all routes except a small whitelist
ALLOWED_PATHS = {
    "/",
    "/kill-da-clanker",
    "/auth/login",
    "/admin/ui",
    "/admin_ui",
    "/ui",
    "/user_ui",
    "/status",
}

STATIC_EXTENSIONS = (".html", ".css", ".js", ".ico", ".png", ".jpg", ".svg", ".mp4")


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def _tail_lines(path: Path, max_lines: int = 200) -> List[str]:
    if not path.exists():
        return []
    try:
        content = path.read_text().splitlines()
    except Exception:
        return []
    if len(content) <= max_lines:
        return content
    return content[-max_lines:]


def schedule_process_reload(delay: float = 1.0) -> None:
    """Restart the current process after a short delay."""

    def _restart() -> None:
        time.sleep(max(delay, 0.0))
        log_and_print("♻️ Reloading server process...")
        os._exit(0)

    Thread(target=_restart, daemon=True).start()

def _parse_projects(value: Any) -> List[str]:
    """Return unique project slugs from strings or iterables."""
    if value is None:
        return []
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        return []

    projects: List[str] = []
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
        projects.append(normalized)
    return projects

@app.middleware("http")
async def enforce_api_key_middleware(request: Request, call_next) -> Any:
    path = request.url.path
    normalized_path = path.rstrip("/") or "/"
    # Allow explicit whitelist, static instructions, and health endpoints without a key
    if normalized_path in ALLOWED_PATHS or path.endswith(STATIC_EXTENSIONS):
        return await call_next(request)

    # Check header or query param
    api_key = request.headers.get(API_KEY_HEADER_NAME) or request.query_params.get("api_key")
    if not api_key:
        admin_key = request.headers.get(ADMIN_KEY_HEADER_NAME) or request.query_params.get("admin_key")
        if admin_key:
            try:
                require_admin_header(request)
            except HTTPException as e:
                return JSONResponse({"detail": e.detail}, status_code=e.status_code)
            request.state.identity = {"name": ADMIN_KEY.get("name", "admin"), "role": "admin", "projects": ["*"]}
            request.state.project = None
            return await call_next(request)
        return JSONResponse({"detail": "Missing API Key"}, status_code=401)

    try:
        identity = require_api_key(api_key)
    except HTTPException as e:
        return JSONResponse({"detail": e.detail}, status_code=e.status_code)

    project = request.headers.get(API_PROJECT_HEADER_NAME) or request.query_params.get("project")
    allowed = [p.lower() for p in identity.get("projects", []) if isinstance(p, str)]
    if allowed and "*" not in allowed:
        if not project:
            return JSONResponse({"detail": "Project header required for this API key"}, status_code=403)
        if project.lower() not in allowed:
            return JSONResponse({"detail": f"Project '{project}' not permitted for this API key"}, status_code=403)

    request.state.identity = identity
    request.state.project = project.lower() if project else None
    return await call_next(request)

@app.post('/auth/login')
async def auth_login(request: Request):
    client_ip = _get_client_ip(request)
    data = await request.json()
    secret = data.get('key')
    if not secret:
        log_and_print(f"⚠️ /auth/login failed: missing key from {client_ip}")
        raise HTTPException(status_code=400, detail="'key' required")
    try:
        identity = require_api_key(secret)
    except HTTPException:
        log_and_print(f"🔒 /auth/login failed: invalid key from {client_ip}")
        raise HTTPException(status_code=403, detail="Invalid key")

    response = {"status": "ok", "name": identity.get("name"), "role": identity.get("role", "user")}
    if identity.get("projects"):
        response["projects"] = identity["projects"]
    log_and_print(f"✅ /auth/login successful for {identity.get('name')} (role: {identity.get('role', 'user')}) from {client_ip}")
    return response

@app.get('/')
async def serve_index(request: Request):
    client_ip = _get_client_ip(request)
    log_and_print(f"📄 Serving index page to {client_ip}")
    return FileResponse('uis/index.html')

@app.get('/admin_ui')
async def serve_admin_ui(request: Request):
    client_ip = _get_client_ip(request)
    log_and_print(f"🔧 Serving admin UI to {client_ip}")
    return FileResponse('uis/security_admin_ui.html')

@app.get('/user_ui')
async def serve_user_ui(request: Request):
    client_ip = _get_client_ip(request)
    log_and_print(f"👤 Serving user UI to {client_ip}")
    return FileResponse('uis/security_user_ui.html')

@app.get('/uis/{filename}')
async def serve_static_file(filename: str, request: Request):
    client_ip = _get_client_ip(request)
    log_and_print(f"📁 Serving static file {filename} to {client_ip}")
    return FileResponse(f'uis/{filename}')

def normalize_text(text: str):
    if not text:
        return ""
    return "".join(ch.lower() if ch.isalnum() else " " for ch in text)

def contains_banned(text: str):
    if not text or text.isspace() or text == "":
        return False
    # Use original text (not normalized) so the model can leverage phone numbers, $ amounts, etc.
    response = ai.prompt(
        text,
        SYSTEM_MESSAGE,
        gm.training.get("messages", []),
        "Here are labeled examples. Treat assistant labels 'Yes' as spam and 'No' as not spam.",
        "End of examples. Classify the next message. Respond with only Yes or No.",
    )

    log_and_print(f"Model response: {response}")
    if not response:
        return False
    content = ""
    if isinstance(response, dict):
        content = (response.get("content") or "").strip().lower()
        if not content:
            content = str(response).strip().lower()
    else:
        content = str(response).strip().lower()
    if not content:
        return False
    answer = content
    if answer.startswith("yes"):
        log_and_print("Banned content detected by model.")
        return True
    if answer.startswith("no"):
        return False
    # Fallback: contain check if model added extra text
    return "yes" in answer and "no" not in answer

@app.get("/status")
async def status(request: Request):
    """Lightweight status endpoint for uptime checks."""
    client_ip = _get_client_ip(request)
    log_and_print(f"💓 Status check received from {client_ip}")
    return {
        "status": "ok",
        "service": "groupme-spam-remover",
        "version": "1.0",
        "endpoints": ["/kill-da-clanker", "/ai", "/admin/ui"],
    }

@app.post("/kill-da-clanker")
async def callback(request: Request):
    client_ip = _get_client_ip(request)
    payload = await request.json()
    user_id = payload.get("user_id")

    # Ignore bot's own messages
    if user_id == "0" or user_id == str(gm.BOT_ID):
        return {"status": "ignored"}

    name = payload.get("name", "Unknown")
    text = payload.get("text", "")
    message_id = payload.get("id")

    log_and_print(f"📩 Message from {name}/{user_id} (IP: {client_ip}): '{text}'")

    if "@thanos" in text.lower():
        # Use groupme helper's Thanos flow and pass ai.prompt as the prompt function
        gm.thanos(name, user_id, text, ai.prompt)
        return {"status": "bot_mentioned"}
    
    if name.lower() in admins:
        lower_text = text.lower()
        if "@undo" in lower_text:
            gm.undo_last_action()
            return {"status": "undo"}
        # Handle @ignore First Last
        if "@ignore" in lower_text:
            name = lower_text[lower_text.find(" ") + 1:].strip()

            if name:
                added = gm.add_to_ignored(name)
                if added:
                    gm.post_bot_message(f"Added '{name}' to the ignore list.")
                    log_and_print(f"🚫 Added '{name}' to ignore list.")
                    return {"status": "ignored_added", "user": name}
                else:
                    gm.post_bot_message(f"'{name}' is already in the ignore list or invalid.")
                    log_and_print(f"🚫 '{name}' is already in ignore list or invalid.")
                    return {"status": "ignored_exists", "user": name}
                
        if "@ban" in lower_text:
            name = lower_text[lower_text.find(" ") + 1:].strip()
            if name:
                banned_id = gm.get_member_id(name)
                gm.ban(banned_id)
                if banned_id:
                    gm.post_bot_message(f"Banned user '{name}'.")
                    log_and_print(f"🚫 Banned user '{name}'.")
                    return {"status": "banned", "user": name}
                else:
                    gm.post_bot_message(f"User '{name}' not found or already banned.")
                    log_and_print(f"🚫 User '{name}' not found or already banned.")
                    return {"status": "ban_failed", "user": name}

    if name.lower() in gm.ignored:
        log_and_print(f"🚫 Ignored user {name}/{user_id}, liking their message.")
        gm.like_message(message_id)
        return {"status": "ignored"}

    if not text or not contains_banned(text):
        return {"status": "ok"}

    gm.reckon(name, user_id, text, message_id)

    Thread(target=gm.subgroup_reckon_worker, args=(name, user_id, contains_banned), daemon=True).start()

    return {"status": "processed"}

@app.post("/ai")
async def ai_endpoint(request: Request, identity: Dict[str, Any] = Depends(require_api_key)):
    """Call the internal prompt function with provided parameters.

    Expected JSON body keys:
    - text (str) required
    - system_message (str) optional
    - data (list) optional
    - train_start (str) optional
    - train_end (str) optional
    - think (bool) optional
    """
    payload = await request.json()
    text = payload.get("text")
    if not text or text.isspace():
        raise HTTPException(status_code=400, detail="'text' is required")

    caller = identity.get("name", "unknown")
    project = getattr(request.state, "project", None)
    client_ip = _get_client_ip(request)
    log_and_print(f"🤖 /ai invoked by {caller} project={project or '*'} from {client_ip}")

    system_message = payload.get("system_message", None)
    data_list = payload.get("data", None)
    train_start = payload.get("train_start", None)
    train_end = payload.get("train_end", None)
    think = payload.get("think", False)

    # Call prompt
    result = ai.prompt(text, system_message, data_list, train_start, train_end, think)
    if not result:
        log_and_print(f"❌ /ai failed for {caller} from {client_ip}: model error or unavailable")
        raise HTTPException(status_code=500, detail="Model error or unavailable")
    log_and_print(f"✅ /ai successful for {caller} (project: {project or '*'}) from {client_ip}")
    if isinstance(result, dict):
        return result
    return {"model": ai.MODEL, "content": str(result)}

@app.post("/admin/generate-key")
async def admin_generate_key(request: Request):
    """Generate a new API key and persist it. Requires admin header defined in admin.key."""
    # Validate admin header using central helper
    require_admin_header(request)
    client_ip = _get_client_ip(request)

    data = await request.json()
    name = data.get('name')
    if not name:
        log_and_print(f"⚠️ /admin/generate-key failed: missing name from {client_ip}")
        raise HTTPException(status_code=400, detail="'name' is required to create a key")
    projects = _parse_projects(data.get("projects"))
    notes = data.get("notes")
    role = str(data.get("role", "user")).lower()
    if role not in {"user", "service", "admin"}:
        role = "user"
    # Generate secret
    secret = generate_secret(32)
    metadata: Dict[str, Any] = {"projects": projects, "notes": notes, "role": role}
    try:
        # persist via security helpers
        from sec.security_helpers import persist_named_key as sh_persist_named_key
        stored = sh_persist_named_key(name, secret, API_KEYS_FILE, metadata)
    except Exception as e:
        log_and_print(f"❌ /admin/generate-key failed for name={name} from {client_ip}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to persist new key: {e}")
    # Update local cache without re-importing
    global API_KEYS
    stored_entry = {k: v for k, v in stored.items() if k != "name"}
    API_KEYS[name] = stored_entry
    log_and_print(f"✅ /admin/generate-key successful: created API key for name={name} (role: {role}) from {client_ip}")
    # Return plaintext secret once
    stored_response = {k: v for k, v in stored.items() if k != "hash"}
    stored_response["secret"] = secret
    return stored_response

@app.get("/admin/list-keys")
async def admin_list_keys(request: Request):
    require_admin_header(request)
    client_ip = _get_client_ip(request)
    log_and_print(f"📋 /admin/list-keys requested from {client_ip}")
    # Return list of names with metadata for identification
    preview: Dict[str, Dict[str, Any]] = {}
    for name, entry in API_KEYS.items():
        if not isinstance(entry, dict):
            entry = {"hash": str(entry)}
        hash_value = entry.get("hash", "")
        hash_preview = (
            f"{hash_value[:8]}...{hash_value[-6:]}" if isinstance(hash_value, str) and len(hash_value) > 14 else hash_value
        )
        preview[name] = {
            "hash_preview": hash_preview,
            "projects": entry.get("projects", []),
            "role": entry.get("role", "user"),
            "created_at": entry.get("created_at"),
            "notes": entry.get("notes"),
            "last_used": entry.get("last_used"),
        }
    return {"keys": preview}

@app.post("/admin/revoke-key")
async def admin_revoke_key(request: Request):
    require_admin_header(request)
    client_ip = _get_client_ip(request)

    data = await request.json()
    name_to_revoke = data.get("name")
    if not name_to_revoke:
        log_and_print(f"⚠️ /admin/revoke-key failed: missing name from {client_ip}")
        raise HTTPException(status_code=400, detail="'name' is required in body")

    removed = remove_api_key(name_to_revoke, API_KEYS_FILE)
    if removed:
        global API_KEYS
        API_KEYS.pop(name_to_revoke, None)
        log_and_print(f"✅ /admin/revoke-key successful: revoked API key name={name_to_revoke} from {client_ip}")
        return {"status": "revoked", "name": name_to_revoke}
    else:
        log_and_print(f"❌ /admin/revoke-key failed: key name={name_to_revoke} not found (from {client_ip})")
        raise HTTPException(status_code=404, detail="Key name not found")

@app.get("/admin/ui")
async def admin_ui(request: Request):
    """Serve a simple admin UI for listing, creating, and revoking keys, and testing model."""
    client_ip = _get_client_ip(request)
    log_and_print(f"🔧 Serving /admin/ui to {client_ip}")
    return FileResponse("uis/security_admin_ui.html")

@app.get("/ui")
async def user_ui(request: Request):
    """Serve a simple user UI that calls the /ai endpoint."""
    client_ip = _get_client_ip(request)
    log_and_print(f"👤 Serving /ui to {client_ip}")
    return FileResponse("uis/security_user_ui.html")

@app.post("/admin/models/list")
async def admin_list_models(request: Request):
    require_admin_header(request)
    client_ip = _get_client_ip(request)
    log_and_print(f"📋 /admin/models/list requested from {client_ip}")
    models = ai.list_models()
    model_count = len(models.get('models', [])) if isinstance(models, dict) else 'unknown'
    log_and_print(f"✅ /admin/models/list successful: {model_count} models found (from {client_ip})")
    return {"models": models}

@app.post("/admin/models/pull")
async def admin_pull_model(request: Request):
    """Pull a model by name (body: {"model": "name"})."""
    require_admin_header(request)
    client_ip = _get_client_ip(request)
    data = await request.json()
    model_name = data.get("model")
    if not model_name:
        log_and_print(f"⚠️ /admin/models/pull failed: missing model name from {client_ip}")
        raise HTTPException(status_code=400, detail="'model' is required")
    log_and_print(f"📥 /admin/models/pull: pulling model {model_name} (from {client_ip})")
    try:
        ai.pull_model_name(model_name)
    except Exception as e:
        log_and_print(f"❌ /admin/models/pull failed for {model_name} from {client_ip}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to pull model: {e}")
    log_and_print(f"✅ /admin/models/pull successful: pulled model {model_name} (from {client_ip})")
    return {"status": "pulled", "model": model_name}

@app.post("/admin/models/delete")
async def admin_delete_model(request: Request):
    require_admin_header(request)
    client_ip = _get_client_ip(request)
    data = await request.json()
    model_name = data.get("model")
    if not model_name:
        log_and_print(f"⚠️ /admin/models/delete failed: missing model name from {client_ip}")
        raise HTTPException(status_code=400, detail="'model' is required")
    log_and_print(f"🗑️ /admin/models/delete: deleting model {model_name} (from {client_ip})")
    try:
        ai.remove_model(model_name)
    except Exception as e:
        log_and_print(f"❌ /admin/models/delete failed for {model_name} from {client_ip}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete model: {e}")
    log_and_print(f"✅ /admin/models/delete successful: deleted model {model_name} (from {client_ip})")
    return {"status": "deleted", "model": model_name}

@app.post("/admin/models/switch")
async def admin_switch_model(request: Request):
    require_admin_header(request)
    client_ip = _get_client_ip(request)
    data = await request.json()
    model_name = data.get("model")
    if not model_name:
        log_and_print(f"⚠️ /admin/models/switch failed: missing model name from {client_ip}")
        raise HTTPException(status_code=400, detail="'model' is required")
    log_and_print(f"🔄 /admin/models/switch: switching to model {model_name} (from {client_ip})")
    # Change model inside ai_helpers
    try:
        active_model = ai.set_model(model_name)
    except ValueError as exc:
        log_and_print(f"❌ /admin/models/switch failed for {model_name} from {client_ip}: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_and_print(f"❌ /admin/models/switch failed for {model_name} from {client_ip}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to switch model: {exc}") from exc
    log_and_print(f"✅ /admin/models/switch successful: switched to model {active_model} (from {client_ip})")
    return {"status": "switched", "model": active_model}

@app.post("/admin/git-pull")
async def admin_git_pull(request: Request):
    require_admin_header(request)
    client_ip = _get_client_ip(request)
    log_and_print(f"📥 /admin/git-pull: executing git fetch and pull (from {client_ip})")
    try:
        res = subprocess.run(["git", "fetch", "--all"], cwd=str(Path('.').absolute()), capture_output=True, text=True)
        res2 = subprocess.run(["git", "pull"], cwd=str(Path('.').absolute()), capture_output=True, text=True)
    except Exception as e:
        log_and_print(f"❌ /admin/git-pull failed from {client_ip}: {e}")
        raise HTTPException(status_code=500, detail=f"Git command failed: {e}")
    log_and_print(f"✅ /admin/git-pull successful (from {client_ip})")
    return {"fetch": res.stdout + res.stderr, "pull": res2.stdout + res2.stderr}


@app.get("/admin/logs/current")
async def admin_current_log(request: Request, limit: int = 200):
    """Return tail of current log file via query param (backwards compatible)."""
    require_admin_header(request)
    client_ip = _get_client_ip(request)
    log_and_print(f"📜 /admin/logs/current requested (limit: {limit}) from {client_ip}")
    try:
        max_lines = int(limit)
    except (TypeError, ValueError):
        log_and_print(f"⚠️ /admin/logs/current failed: invalid limit parameter from {client_ip}")
        raise HTTPException(status_code=400, detail="'limit' must be an integer")
    max_lines = max(1, min(max_lines, 2000))
    lines = _tail_lines(LOG_FILE, max_lines)
    return {"path": str(LOG_FILE), "lines": lines}


@app.post("/admin/logs/refresh")
async def admin_refresh_log(request: Request):
    """Refresh/read the current log file. Accepts JSON body { "limit": <int> }.

    This is the preferred endpoint for UI-driven refreshes.
    """
    require_admin_header(request)
    client_ip = _get_client_ip(request)
    try:
        data = await request.json()
    except Exception:
        data = None

    limit = 200
    if isinstance(data, dict):
        raw = data.get("limit")
        if raw is not None:
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                log_and_print(f"⚠️ /admin/logs/refresh failed: invalid limit parameter from {client_ip}")
                raise HTTPException(status_code=400, detail="'limit' must be an integer")

    limit = max(1, min(limit, 2000))
    lines = _tail_lines(LOG_FILE, limit)
    log_and_print(f"📜 /admin/logs/refresh successful (limit: {limit}, returned {len(lines)} lines) from {client_ip}")
    return {"path": str(LOG_FILE), "lines": lines}

@app.post("/admin/server/reload")
async def admin_reload_server(request: Request):
    require_admin_header(request)
    client_ip = _get_client_ip(request)
    delay_seconds = 1.0
    try:
        data = await request.json()
    except Exception:
        data = None

    if isinstance(data, dict):
        raw_delay = data.get("delay_seconds")
        if raw_delay is None:
            raw_delay = data.get("delay")
        if raw_delay is not None:
            try:
                delay_seconds = float(raw_delay)
            except (TypeError, ValueError):
                log_and_print(f"⚠️ /admin/server/reload failed: invalid delay_seconds parameter from {client_ip}")
                raise HTTPException(status_code=400, detail="'delay_seconds' must be a number")

    delay_seconds = max(delay_seconds, 0.0)
    log_and_print(f"🔄 /admin/server/reload: scheduling server reload in {delay_seconds}s (from {client_ip})")
    schedule_process_reload(delay_seconds)
    return {"status": "reloading", "delay_seconds": delay_seconds}

# Entry point
if __name__ == "__main__":
    log_and_print("🚀 Starting bot server...")
    host = ai.get_host()
    log_and_print(f"Ollama client configured for {host}")
    if(not gm.STRIKES_FILE.exists()):
        gm.STRIKES_FILE.write_text("{}")
    if(not gm.CONVERSATIONS_FILE.exists()):
        gm.CONVERSATIONS_FILE.write_text("{}")
    # Ensure training file exists
    if not gm.TRAINING_FILE.exists():
        gm.TRAINING_FILE.write_text(json.dumps({"messages": []}))
    if not ai.check_model_availability():
        ai.pull_model()
    # start invite acceptor from groupme helpers
    Thread(target=gm.accept_invites, daemon=True).start()
    uvicorn.run("vaayuronics:app", host="127.0.0.1", port=8000, reload=False) #, ssl_keyfile='/etc/letsencrypt/live/vaayuronics.com/privkey.pem', ssl_certfile='/etc/letsencrypt/live/vaayuronics.com/fullchain.pem')
