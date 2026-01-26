import json
from pathlib import Path
from typing import Any, Dict, List
import os
import sys
import traceback
from logs.logsys import log_and_print, LOG_FILE
import time
from fastapi import FastAPI, Request
from fastapi import HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse
import uvicorn
from threading import Thread
import ai.ai_helpers as ai
import groupme.groupme_helpers as gm

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
}


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def schedule_process_reload(delay: float = 1.0) -> None:
    """Restart the current process after a short delay."""

    def _restart() -> None:
        time.sleep(max(delay, 0.0))
        log_and_print("♻️ Reloading server process...")
        os._exit(0)

    Thread(target=_restart, daemon=True).start()


@app.get('/')
async def serve_index(request: Request):
    client_ip = _get_client_ip(request)
    log_and_print(f"📄 Serving bad_endpoint page to {client_ip}")
    return FileResponse('bad_endpoint.html')


def normalize_text(text: str):
    if not text:
        return ""
    return "".join(ch.lower() if ch.isalnum() else " " for ch in text)


def contains_banned(text: str):
    if not text or text.isspace() or text == "":
        log_and_print("⚪ Empty message, skipping spam check")
        return False
    
    log_and_print(f"🔍 Checking message for spam: '{text[:100]}{'...' if len(text) > 100 else ''}'")
    
    # Use original text (not normalized) so the model can leverage phone numbers, $ amounts, etc.
    if ai.ollama_model is None:
        log_and_print("⚠️ Ollama model is not connected, using fallback keyword detection", level="warning")
        # Enhanced fallback logic with more spam indicators
        lower_text = text.lower()
        spam_keywords = [
            ("ticket", "sell"), ("ticket", "dm"), ("ticket", "text"),
            ("selling", "dm"), ("selling", "text"), ("selling", "venmo"),
            ("buying", "dm"), ("buying", "text"),
            ("macbook", "dm"), ("iphone", "sell"), ("laptop", "sell")
        ]
        
        # Check for combinations of spam keywords
        for keyword1, keyword2 in spam_keywords:
            if keyword1 in lower_text and keyword2 in lower_text:
                log_and_print(f"✅ Fallback detected spam: found '{keyword1}' + '{keyword2}'")
                return True
        
        # Check for phone numbers with selling context
        import re
        phone_pattern = r'\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}'
        if re.search(phone_pattern, text) and any(word in lower_text for word in ["sell", "selling", "ticket", "dm", "text me"]):
            log_and_print("✅ Fallback detected spam: phone number with selling keywords")
            return True
        
        log_and_print("✅ Fallback check: message appears clean")
        return False
    
    # AI is available, use it
    try:
        response = ai.prompt(
            text,
            SYSTEM_MESSAGE,
            gm.training.get("messages", []),
            "Here are labeled examples. Treat assistant labels 'Yes' as spam and 'No' as not spam.",
            "End of examples. Classify the next message. Respond with only Yes or No.",
        )

        log_and_print(f"🤖 Model response: {response}")
        if not response:
            log_and_print("⚠️ Model returned no response, assuming message is clean", level="warning")
            return False
        
        content = ""
        if isinstance(response, dict):
            content = (response.get("content") or "").strip().lower()
            if not content:
                content = str(response).strip().lower()
        else:
            content = str(response).strip().lower()
        
        if not content:
            log_and_print("⚠️ Empty model response, assuming message is clean", level="warning")
            return False
        
        answer = content
        if answer.startswith("yes"):
            log_and_print("🚨 SPAM DETECTED by AI model")
            return True
        if answer.startswith("no"):
            log_and_print("✅ Message marked clean by AI model")
            return False
        
        # Fallback: contain check if model added extra text
        result = "yes" in answer and "no" not in answer
        if result:
            log_and_print("🚨 SPAM DETECTED by AI model (fuzzy match)")
        else:
            log_and_print("✅ Message marked clean by AI model (fuzzy match)")
        return result
    except Exception as e:
        log_and_print(f"❌ Error during AI check: {e}", level="error")
        log_and_print("⚠️ Falling back to keyword detection due to error", level="warning")
        # Fallback to keyword detection on error
        lower_text = text.lower()
        if any(word in lower_text for word in ["ticket", "sell", "selling"]) and any(word in lower_text for word in ["dm", "text", "venmo", "cash app"]):
            log_and_print("✅ Fallback detected spam after AI error")
            return True
        log_and_print("✅ Fallback check: message appears clean after AI error")
        return False


@app.post("/kill-da-clanker")
async def callback(request: Request):
    try:
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
            if ai.ollama_model is None:
                log_and_print("❌ Ollama model is not connected, cannot process @thanos command", level="error")
                gm.post_bot_message("❌ AI is currently offline. Cannot process @thanos command. Please try again later.")
                return {"status": "ollama_not_connected"}
            try:
                log_and_print(f"🤖 Processing @thanos command from {name}")
                gm.thanos(name, user_id, text, ai.prompt)
                return {"status": "bot_mentioned"}
            except Exception as e:
                log_and_print(f"❌ Error processing @thanos command: {e}", level="error")
                log_and_print(f"Traceback: {traceback.format_exc()}", level="error")
                return {"status": "thanos_error", "error": str(e)}
        
        if name.lower() in admins:
            lower_text = text.lower()
            if "@undo" in lower_text:
                try:
                    gm.undo_last_action()
                    return {"status": "undo"}
                except Exception as e:
                    log_and_print(f"❌ Error processing @undo: {e}", level="error")
                    return {"status": "undo_error", "error": str(e)}
            # Handle @ignore First Last
            if "@ignore" in lower_text:
                try:
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
                except Exception as e:
                    log_and_print(f"❌ Error processing @ignore: {e}", level="error")
                    log_and_print(f"Traceback: {traceback.format_exc()}", level="error")
                    return {"status": "ignore_error", "error": str(e)}
                
            if "@ban" in lower_text:
                try:
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
                except Exception as e:
                    log_and_print(f"❌ Error processing @ban: {e}", level="error")
                    log_and_print(f"Traceback: {traceback.format_exc()}", level="error")
                    return {"status": "ban_error", "error": str(e)}

        try:
            if name.lower() in gm.ignored:
                log_and_print(f"🚫 Ignored user {name}/{user_id}, liking their message.")
                gm.like_message(message_id)
                return {"status": "ignored"}
        except Exception as e:
            log_and_print(f"❌ Error checking ignored list or liking message: {e}", level="error")
            # Continue with spam check even if like fails

        try:
            if not text or not contains_banned(text):
                return {"status": "ok"}
        except Exception as e:
            log_and_print(f"❌ Error during spam check: {e}", level="error")
            log_and_print(f"Traceback: {traceback.format_exc()}", level="error")
            return {"status": "spam_check_error", "error": str(e)}

        try:
            gm.reckon(name, user_id, text, message_id)
            Thread(target=gm.subgroup_reckon_worker, args=(name, user_id, contains_banned), daemon=True).start()
        except Exception as e:
            log_and_print(f"❌ Error during reckon/moderation: {e}", level="error")
            log_and_print(f"Traceback: {traceback.format_exc()}", level="error")
            return {"status": "reckon_error", "error": str(e)}

        return {"status": "processed"}
    
    except json.JSONDecodeError as e:
        log_and_print(f"❌ Invalid JSON in request: {e}", level="error")
        return {"status": "json_error", "error": "Invalid JSON"}
    except Exception as e:
        log_and_print(f"❌ Unexpected error in callback: {e}", level="error")
        log_and_print(f"Traceback: {traceback.format_exc()}", level="error")
        return {"status": "callback_error", "error": str(e)}


def try_connect():
    while True:
        if not ai.connect():
            log_and_print(f"❌ Failed to connect to Ollama host at {ai.OLLAMA_HOST}.", level="error")
            time.sleep(60*60*3)
        else:
            log_and_print(f"Ollama client configured for {ai.OLLAMA_HOST}. Using model: {ai.get_model()}")
            if not ai.check_model_availability():
                ai.pull_model()
                log_and_print(f"⬇️ Pulled model '{ai.get_model()}' from Ollama host.")
            break


# Entry point
if __name__ == "__main__":
    log_and_print("🚀 Starting BOT server...")
    Thread(target=try_connect, daemon=True).start()
    if(not gm.STRIKES_FILE.exists()):
        gm.STRIKES_FILE.write_text("{}")
    if(not gm.CONVERSATIONS_FILE.exists()):
        gm.CONVERSATIONS_FILE.write_text("{}")
    # Ensure training file exists
    if not gm.TRAINING_FILE.exists():
        gm.TRAINING_FILE.write_text(json.dumps({"messages": []}))
    # start invite acceptor from groupme helpers
    Thread(target=gm.accept_invites, daemon=True).start()
    uvicorn.run("anti_clanker:app", host="127.0.0.1", port=8003, reload=False) #, ssl_keyfile='/etc/letsencrypt/live/vaayuronics.com/privkey.pem', ssl_certfile='/etc/letsencrypt/live/vaayuronics.com/fullchain.pem')
