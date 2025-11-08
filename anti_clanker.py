import json
from pathlib import Path
from typing import Any, Dict, List
import subprocess
from fastapi import FastAPI, Request
from fastapi import HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse
import uvicorn
from threading import Thread
import logging
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

doBans = True  # Set to True to ban users after max strikes, False to only remove them
WARN_STRIKES = 1  # delete message on first strike, remove on second
WAIT = 30  # seconds to wait before checking subgroups for spam
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

# Setup rotating log per process start inside `logging` dir
LOG_DIR = Path("logging")
LOG_DIR.mkdir(parents=True, exist_ok=True)
def _next_log_path():
    existing = [p.name for p in LOG_DIR.iterdir() if p.is_file() and p.name.startswith("log_") and p.suffix==".log"]
    nums = []
    for n in existing:
        try:
            nums.append(int(n.split("_")[1].split(".")[0]))
        except Exception:
            continue
    next_idx = max(nums)+1 if nums else 0
    return LOG_DIR / f"log_{next_idx}.log"

LOG_FILE = _next_log_path()
logging.basicConfig(level=logging.INFO, filename=str(LOG_FILE), filemode="a",
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("app")

def log_and_print(msg: str, level: str = "info"):
    # safe wrapper: never log secrets. Use only for general messages.
    if level == "info":
        logger.info(msg)
        print(msg, flush=True)
    elif level == "error":
        logger.error(msg)
        print(msg, flush=True)
    else:
        logger.debug(msg)
        print(msg, flush=True)

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

STATIC_EXTENSIONS = (".html", ".css", ".js", ".ico", ".png", ".jpg", ".svg")

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
    data = await request.json()
    secret = data.get('key')
    if not secret:
        raise HTTPException(status_code=400, detail="'key' required")
    try:
        identity = require_api_key(secret)
    except HTTPException:
        raise HTTPException(status_code=403, detail="Invalid key")

    response = {"status": "ok", "name": identity.get("name"), "role": identity.get("role", "user")}
    if identity.get("projects"):
        response["projects"] = identity["projects"]
    return response

@app.get('/')
async def serve_index():
    return FileResponse('uis/index.html')

@app.get('/admin_ui')
async def serve_admin_ui():
    return FileResponse('uis/security_admin_ui.html')

@app.get('/user_ui')
async def serve_user_ui():
    return FileResponse('uis/security_user_ui.html')

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
async def status(_: Request):
    """Lightweight status endpoint for uptime checks."""
    return {
        "status": "ok",
        "service": "groupme-spam-remover",
        "version": "1.0",
        "endpoints": ["/kill-da-clanker", "/ai", "/admin/ui"],
    }

@app.post("/kill-da-clanker")
async def callback(request: Request):
    payload = await request.json()
    user_id = payload.get("user_id")

    # Ignore bot’s own messages
    if user_id == "0" or user_id == str(gm.BOT_ID):
        return {"status": "ignored"}

    name = payload.get("name", "Unknown")
    text = payload.get("text", "")
    message_id = payload.get("id")

    log_and_print(f"📩 Message from {name}/{user_id}: '{text}'")

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
            name = lower_text[lower_text.find(" ") + 1:lower_text.rfind(" ")]

            if name:
                added = gm.add_to_ignored(name)
                if added:
                    gm.post_bot_message(f"Added '{name}' to the ignore list.")
                    log_and_print(f"🚫 Added '{name}' to ignore list.", flush=True)
                    return {"status": "ignored_added", "user": name}
                else:
                    gm.post_bot_message(f"'{name}' is already in the ignore list or invalid.")
                    log_and_print(f"🚫 '{name}' is already in ignore list or invalid.", flush=True)
                    return {"status": "ignored_exists", "user": name}

    if name.lower() in gm.ignored:
        log_and_print(f"🚫 Ignored user {name}/{user_id}, liking their message.", flush=True)
        gm.like_message(message_id)
        return {"status": "ignored"}

    if not text or not contains_banned(text):
        return {"status": "ok"}

    gm.reckon(name, user_id, text, message_id)

    Thread(target=gm.subgroup_reckon_worker, args=(name, user_id, WAIT, contains_banned), daemon=True).start()

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
    logger.info("/ai invoked by %s project=%s", caller, project or "*")

    system_message = payload.get("system_message", None)
    data_list = payload.get("data", None)
    train_start = payload.get("train_start", None)
    train_end = payload.get("train_end", None)
    think = payload.get("think", False)

    # Call prompt
    result = ai.prompt(text, system_message, data_list, train_start, train_end, think)
    if not result:
        raise HTTPException(status_code=500, detail="Model error or unavailable")
    if isinstance(result, dict):
        return result
    return {"model": ai.MODEL, "content": str(result)}

@app.post("/admin/generate-key")
async def admin_generate_key(request: Request):
    """Generate a new API key and persist it. Requires admin header defined in admin.key."""
    # Validate admin header using central helper
    require_admin_header(request)

    data = await request.json()
    name = data.get('name')
    if not name:
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
        raise HTTPException(status_code=500, detail=f"Failed to persist new key: {e}")
    # Update local cache without re-importing
    global API_KEYS
    stored_entry = {k: v for k, v in stored.items() if k != "name"}
    API_KEYS[name] = stored_entry
    logger.info(f"Created API key for name={name}")
    # Return plaintext secret once
    stored_response = {k: v for k, v in stored.items() if k != "hash"}
    stored_response["secret"] = secret
    return stored_response

@app.get("/admin/list-keys")
async def admin_list_keys(request: Request):
    require_admin_header(request)
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
        }
    return {"keys": preview}

@app.post("/admin/revoke-key")
async def admin_revoke_key(request: Request):
    require_admin_header(request)

    data = await request.json()
    name_to_revoke = data.get("name")
    if not name_to_revoke:
        raise HTTPException(status_code=400, detail="'name' is required in body")

    removed = remove_api_key(name_to_revoke, API_KEYS_FILE)
    if removed:
        global API_KEYS
        API_KEYS.pop(name_to_revoke, None)
        logger.info(f"Revoked API key name={name_to_revoke}")
        return {"status": "revoked", "name": name_to_revoke}
    else:
        raise HTTPException(status_code=404, detail="Key name not found")

@app.get("/admin/ui")
async def admin_ui(request: Request):
    """Serve a simple admin UI for listing, creating, and revoking keys, and testing model."""
    return FileResponse("uis/security_admin_ui.html")

@app.get("/ui")
async def user_ui(request: Request):
    """Serve a simple user UI that calls the /ai endpoint."""
    return FileResponse("uis/security_user_ui.html")

@app.post("/admin/models/list")
async def admin_list_models(request: Request):
    require_admin_header(request)
    models = ai.list_models()
    return {"models": models}

@app.post("/admin/models/pull")
async def admin_pull_model(request: Request):
    """Pull a model by name (body: {"model": "name"})."""
    require_admin_header(request)
    data = await request.json()
    model_name = data.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="'model' is required")
    try:
        ai.pull_model_name(model_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to pull model: {e}")
    return {"status": "pulled", "model": model_name}

@app.post("/admin/models/delete")
async def admin_delete_model(request: Request):
    require_admin_header(request)
    data = await request.json()
    model_name = data.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="'model' is required")
    try:
        ai.remove_model(model_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete model: {e}")
    return {"status": "deleted", "model": model_name}

@app.post("/admin/models/switch")
async def admin_switch_model(request: Request):
    require_admin_header(request)
    data = await request.json()
    model_name = data.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="'model' is required")
    # Change model inside ai_helpers
    try:
        active_model = ai.set_model(model_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to switch model: {exc}") from exc
    return {"status": "switched", "model": active_model}

@app.post("/admin/git-pull")
async def admin_git_pull(request: Request):
    require_admin_header(request)
    try:
        res = subprocess.run(["git", "fetch", "--all"], cwd=str(Path('.').absolute()), capture_output=True, text=True)
        res2 = subprocess.run(["git", "pull"], cwd=str(Path('.').absolute()), capture_output=True, text=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Git command failed: {e}")
    return {"fetch": res.stdout + res.stderr, "pull": res2.stdout + res2.stderr}

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
    uvicorn.run("anti_clanker:app", host="0.0.0.0", port=443, reload=False, ssl_keyfile='/etc/letsencrypt/live/vaayuronics.com/privkey.pem', ssl_certfile='/etc/letsencrypt/live/vaayuronics.com/fullchain.pem')
