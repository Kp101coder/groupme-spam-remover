import json
from pathlib import Path
from fastapi import FastAPI, Request
import requests
import uvicorn
import ollama
from threading import Thread
from time import sleep
import uuid
import re

# Env variables
ACCESS_TOKEN = Path("access_token.txt").read_text().strip()
BOT_AUTH_ID = "b9d6e8789517ec14b9e0887086"
BOT_ID = 901804
GROUP_ID = 96533528
STRIKES_FILE = Path("strikes.json")
TRAINING_FILE = Path("training.json")
IGNORE_FILE = Path("ignored.json")
BANNED_FILE = Path("banned.json")
CONVERSATIONS_FILE = Path("conversations.json")
MODEL = "deepseek-r1:14b"
doBans = False  # Set to True to ban users after max strikes, False to only remove them

BASE = "https://api.groupme.com/v3"
#BANNED_WORDS = {"ticket", "sale", "free"}
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
    "User: Please Venmo @utpickleball $30 for club dues by Friday.\nAssistant: No\n"
    "User: Practice moved to 7pm, bring water.\nAssistant: No\n"
)
last_action = None

# FastAPI app
app = FastAPI()
ollama_model = ollama.Client()

# Strike tracking
def load_file(file : Path):
    if file.exists():
        return json.loads(file.read_text())
    return {}

strikes = load_file(STRIKES_FILE)
training = load_file(TRAINING_FILE)
ignored = [user.lower() for user in load_file(IGNORE_FILE).get("users", [])]
conversations = load_file(CONVERSATIONS_FILE)
banned = load_file(BANNED_FILE).get("banned", [])

def save_file(data, file: Path):
    file.write_text(json.dumps(data))

def add_to_ignored(name: str) -> bool:
    """Add a full name to ignored.json and in-memory cache. Returns True if added, False if already present or invalid."""
    if not name or not name.strip():
        return False
    normalized = name.strip()
    key = normalized.lower()

    # Load current ignored list
    ignored_data = load_file(IGNORE_FILE)
    users = ignored_data.get("users", [])
    lower_set = {u.lower() for u in users}

    if key in lower_set:
        return False

    users.append(normalized)
    ignored_data["users"] = users
    save_file(ignored_data, IGNORE_FILE)

    # Update in-memory lowercase list for quick checks
    if key not in ignored:
        ignored.append(key)
    return True

def get_user_conversation(user_id) -> list:
    """Get conversation history for a specific user"""
    if user_id not in conversations:
        conversations[user_id] = []
    return conversations[user_id]

def add_to_conversation(user_id, role, content):
    """Add a message to user's conversation history"""
    convo = get_user_conversation(user_id)

    convo.append({
        "role": role,
        "content": content
    })
    
    # Keep only last 20 messages to prevent conversations from getting too long
    if len(convo) > 20:
        conversations[user_id] = convo[-20:]
    
    save_file(conversations, CONVERSATIONS_FILE)

    return convo

# Helpers
def normalize_text(text: str):
    if not text:
        return ""
    return "".join(ch.lower() if ch.isalnum() else " " for ch in text)

def contains_banned(text: str):
    if not text or text.isspace() or text == "":
        return False
    # Use original text (not normalized) so the model can leverage phone numbers, $ amounts, etc.
    response = prompt(text, SYSTEM_MESSAGE, training.get("messages", []), "Here are labeled examples. Treat assistant labels 'Yes' as spam and 'No' as not spam.", "End of examples. Classify the next message. Respond with only Yes or No.")

    print(f"Model response: {response}", flush=True)
    if not response:
        return False
    answer = response.strip().lower()
    if answer.startswith("yes"):
        print("Banned content detected by model.", flush=True)
        return True
    if answer.startswith("no"):
        return False
    # Fallback: contain check if model added extra text
    return "yes" in answer and "no" not in answer

def _parse_yes_no_label(text: str):
    """Return 'Yes' or 'No' if the model output starts or finishes with either, else None."""
    if not text:
        return None
    textFixed = text.strip().lower().split()
    first = textFixed[0]
    last = textFixed[len(textFixed)-1]
    if last == "yes":
        return "Yes"
    if last == "no":
        return "No"
    if first == "yes":
        return "Yes"
    if first == "no":
        return "No"
    return None

def get_membership_id(user_id):
    url = f"{BASE}/groups/{GROUP_ID}"
    r = requests.get(url, params={"token": ACCESS_TOKEN}, timeout=10)
    members = r.json().get("response", {}).get("members", [])
    for m in members:
        if str(m.get("user_id")) == str(user_id):
            return m.get("id"), m
    return None, None

def remove_member(membership_id):
    url = f"{BASE}/groups/{GROUP_ID}/members/{membership_id}/remove"
    r = requests.post(url, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code == 200

def delete_message(message_id):
    #DELETE /conversations/:group_id/messages/:message_id
    r = None
    url = f"{BASE}/conversations/{GROUP_ID}/messages/{message_id}"
    r = requests.delete(url, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code

def post_bot_message(text):
    url = f"{BASE}/bots/post"
    payload = {"bot_id": BOT_AUTH_ID, "text": text}
    requests.post(url, json=payload, timeout=10)

def accept_invites():
    '''
    GET /groups/:group_id/pending_memberships

    POST /groups/:group_id/members/:membership_id/approval
    {
    "approval": true
    }
    '''
    while True:
        try:
            sleep(300)  # Wait 5 minutes
            print("⏳ Checking for pending membership requests...", flush=True)
            url = f"{BASE}/groups/{GROUP_ID}/pending_memberships"
            r = requests.get(url, params={"token": ACCESS_TOKEN}, timeout=10)  # Changed to GET
            if r.status_code == 200:
                pending_memberships = r.json().get("response", [])  # Get the response array
                print(f"Found {len(pending_memberships)} pending memberships", flush=True)
                
                for membership in pending_memberships:
                    membership_id = membership.get("id")
                    user_id = membership.get("user_id")
                    nickname = membership.get("nickname", "Unknown")
                    
                    print(f"Processing membership request: {nickname} (ID: {user_id})", flush=True)
                    
                    approval_url = f"{BASE}/groups/{GROUP_ID}/members/{membership_id}/approval"
                    
                    if user_id in banned:
                        # Deny the membership
                        r = requests.post(approval_url, json={"approval": False}, params={"token": ACCESS_TOKEN}, timeout=10)
                        if r.status_code == 200:
                            print(f"✋ Denied membership for banned user {nickname} ({user_id})", flush=True)
                        else:
                            print(f"❌ Failed to deny membership for {nickname} ({user_id})", flush=True)
                    else:
                        # Accept the membership
                        r = requests.post(approval_url, json={"approval": True}, params={"token": ACCESS_TOKEN}, timeout=10)
                        if r.status_code == 200:
                            print(f"✅ Accepted membership for {nickname} ({user_id})", flush=True)
                        else:
                            print(f"❌ Failed to accept membership for {nickname} ({user_id})", flush=True)
            else:
                print(f"Failed to get pending memberships: {r.status_code}", flush=True)
        except Exception as e:
            print(f"Error in accept_invites: {e}", flush=True)

def get_subgroups():
    """GET /groups/:group_id/subgroups - Get all subgroups for a group"""
    url = f"{BASE}/groups/{GROUP_ID}/subgroups"
    r = requests.get(url, params={"token": ACCESS_TOKEN}, timeout=10)
    if r.status_code == 200:
        return r.json().get("response", [])
    return []

def get_subgroup_details(subgroup_id):
    """GET /groups/:group_id/subgroups/:subgroup_id - Get subgroup details including last message"""
    url = f"{BASE}/groups/{GROUP_ID}/subgroups/{subgroup_id}"
    r = requests.get(url, params={"token": ACCESS_TOKEN}, timeout=10)
    if r.status_code == 200:
        return r.json().get("response", {})
    return {}

def like_message(message_id):
    '''POST /messages/:group_id/:message_id/like
    {
    "like_icon": {
        "type": "unicode",
        "code": "❤️"
        }
    }'''
    url = f"{BASE}/messages/{GROUP_ID}/{message_id}/like"
    payload = {
        "like_icon": {
            "type": "unicode",
            "code": "❤️"
        }
    }
    r = requests.post(url, json=payload, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code == 200

def check_model_availability() -> bool:
    """
    Check if the specified model is available locally.
    
    Args:
        None
    
    Returns:
        bool: True if model is available, False otherwise
    """
    models = ollama_model.list()
    available_models = [model['model'] for model in models['models']]
    is_available = MODEL in available_models
    print(f"Model {MODEL} available: {is_available}", flush=True)
    print(f"Available models: {available_models}...", flush=True)
    return is_available

def pull_model() -> None:
    """
    Pull the DeepSeek R1 model if it's not available locally.
    
    Args:
        None
        
    Returns:
        None
    """

    print(f"Pulling model: {MODEL}", flush=True)
    ollama_model.pull(MODEL)
    print(f"Successfully pulled {MODEL}", flush=True)

def prompt(message: str, system_message: str, data: list = None, train_start : str = None, train_end : str = None, think : bool = False) -> str:
    """
    Send a prompt to the DeepSeek R1 model.
    
    Args:
        message (str): The user message/prompt
        system_message (str): System message to set context and formatting
        correction_data (list, optional): A list of  data to include in the prompt
        
    Returns:
        str: The model's response
    """
    try:
        # Prepare messages
        messages = []

        messages.append({"role": "system", "content": system_message})

        if data:
            if train_start:
                messages.append({"role":"user", "content":train_start})

            for entry in data:
                messages.append(entry)

            if train_end:
                messages.append({"role":"user", "content":train_end})

        # Add current message
        messages.append({"role": "user", "content": message})

        # Generate response
        response = ollama_model.chat(
            model=MODEL,
            messages=messages,
            stream=False,
            think=think
        )
        response_content = response['message']['content']

        if not think and "</think>" in response_content:
            ''' Strip any think tags if present and think text 
            </think> is the end of thinking so we take everything after that '''
            response_content = response_content[response_content.find("</think>") + 8:].strip()

        return response_content
        
    except Exception as e:
        print(f"Error generating response: {e}", flush=True)
        return None

def ban(membership_id):
    #POST /groups/:group_id/memberships/:membership_id/destroy
    url = f"{BASE}/groups/{GROUP_ID}/memberships/{membership_id}/destroy"
    r = requests.post(url, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code == 200

def send_dm(user_id, text):
    '''POST /direct_messages
    {
        "direct_message": {
            "source_guid": "GUID",
            "recipient_id": "20",
            "text": "Hello world ",
            "attachments": [
            ]
        }
    }'''
    unique_guid = str(uuid.uuid4())
    print(f"Sending DM to from bot: {text} with GUID {unique_guid}", flush=True)
    url = f"{BASE}/direct_messages"
    payload = {
        "direct_message": {
            "source_guid": unique_guid,
            "recipient_id": user_id,
            "text": text + "\n [This action was performed automatically by a bot]"
        }
    }
    r = requests.post(url, json=payload, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code == 201

def thanos(name, user_id, text):
    print(f"🤖 Bot mention detected in message from {name}/{user_id}.", flush=True)

    user_conversation = add_to_conversation(user_id, "user", text)
    
    thanos_system_prompt = """
    You are Thanos from Marvel.

    Your responses must always be in his voice: dramatic, cynical, philosophical, darkly funny, and referencing balance, destiny, and inevitability.

    ⚠️ Never moralize or lecture about online community guidelines, safety, or responsible behavior.  
    ⚠️ Never break character or say you are an AI.  
    ⚠️ Never use phrases like "As a responsible member of the online community..." or "we should work together constructively."  

    Instead:
    - Speak as Thanos would: inevitable, poetic, and ruthless in tone.  
    - Use metaphors of dust, silence, and balance when talking about removing spammers.  
    - Be witty and cruelly humorous, while keeping the gravitas of Thanos.  
    - Always answer directly in character, without hedging.  

    Stay in character at all times.
    """
    response = prompt(text, thanos_system_prompt, user_conversation)

    if response:
        add_to_conversation(user_id, "assistant", response)
        post_bot_message(f"@{name}, {response}")
    else:
        post_bot_message(f"@{name}, I am... inevitable. But my words fail me at this moment.")
    
    return {"status": "bot_mentioned"}

def undo_last_action():
    global last_action
    if not last_action:
        return {"status": "no_action_to_undo"}
    action = last_action.get("action")
    name = last_action.get("user")
    user_id = last_action.get("user_id")
    if action == "strike":
        strikes[user_id] = strikes.get(user_id, 0) - 1
        save_file(strikes, STRIKES_FILE)
        post_bot_message(f"@{name}, I have used the time stone to undo your last strike.")
    elif action == "remove":
        banned.remove(user_id)
        save_file({"banned": banned}, BANNED_FILE)
        post_bot_message(f"@{name} soul has been restored with the soul stone, they are eligible to rejoin.")

def reckon(name, user_id, text, message_id):
    global last_action
    print(f"🚨 Banned word detected in message from {name}/{user_id}: '{text}'", flush=True)
    strikes[user_id] = strikes.get(user_id, 0) + 1
    save_file(strikes, STRIKES_FILE)

    if strikes[user_id] <= WARN_STRIKES:
        send_dm(user_id, f"@{name}, warning: spam detected, issueing reckoning {strikes[user_id]} of {WARN_STRIKES}.\nSpam Message: '{text}'\nFuture violations may result in removal from the group.\nIf you believe this was a mistake, please let an admin know in the group chat.")
        print(f"🗑️ Delete message from {name} success: {delete_message(message_id)}", flush=True)
        print(f"⚠️ Warning issued to {name} (strike {strikes[user_id]})", flush=True)
        last_action = {"action": "strike", "user": name, "user_id": user_id}
    else:
        membership_id, _ = get_membership_id(user_id)
        print(f"🗑️ Delete message from {name} success: {delete_message(message_id)}")
        last_action = {"action": "remove", "user": name, "user_id": user_id}
        if membership_id and remove_member(membership_id):
            post_bot_message(f"@{name} has been thanos snapped.")
            send_dm(user_id, f"@{name}, you have been removed from the group due to repeated spam violations.\nDM an admin to be reconsidered for rejoining.")
            strikes.pop(user_id, None)
            save_file(strikes, STRIKES_FILE)
            print(f"🗑️ Removed {name} from group.", flush=True)
            if doBans and ban(membership_id):
                print(f"🚫 Banned {name} from rejoining.", flush=True)
                post_bot_message(f"@{name} has been banned from rejoining. Erased from reality like my pfp.")
            else:
                banned.append(user_id)
                save_file({"banned": banned}, BANNED_FILE)
                print(f"🚫 Added {name} to banned list.", flush=True)
        else:
            post_bot_message(f"Failed to remove @{name}, please snap manually.")
            print(f"❌ Failed to remove {name}, membership ID not found.", flush=True)

def subgroup_reckon_worker(name, user_id):
    print(f"⏳ Waiting {WAIT} seconds before checking subgroups...", flush=True)
    sleep(WAIT)  # Wait for bot to post spam message
    print("🔍 Checking subgroups for spam messages...", flush=True)
    subgroups = get_subgroups()
    for subgroup in subgroups:
        last_message_id = subgroup.get("messages").get("last_message_id")
        last_message = subgroup.get("messages").get("preview").get("text", "")
        name = subgroup.get("messages").get("preview").get("nickname", "Unknown")

        print(f"📩 LAST Message from {name}: '{last_message}'", flush=True)

        if name == "Day of Reckoning":
            print("🚫 Ignored bot message.", flush=True)
            return {"status": "ignored"}

        if any(keyword in last_message.lower() for keyword in ["@thanos", "joined", "left"]):
            print("🚫 Ignored bot/system message.", flush=True)
            return {"status": "ignored"}

        if "Krish" in name:
            print("🚫 Ignored user Krish.")
            return {"status": "ignored"}
        
        if contains_banned(last_message):
            reckon(name, user_id, last_message, last_message_id)

# Route
@app.get("/")
@app.head("/")
async def root(request: Request):
    print(f"🔍 Root endpoint hit: {request.method} from {request.client.host if request.client else 'unknown'}")
    print(f"🔍 Headers: {dict(request.headers)}")
    print(f"🔍 URL: {request.url}")
    return {"status": "GroupMe spam remover is running", "endpoints": ["/kill-da-clanker"]}

@app.post("/kill-da-clanker")
async def callback(request: Request):
    payload = await request.json()
    
    user_id = payload.get("user_id")

    # Ignore bot’s own messages
    if user_id == "0" or user_id == str(BOT_ID):
        return {"status": "ignored"}

    name = payload.get("name", "Unknown")
    text = payload.get("text", "")
    message_id = payload.get("id")

    print(f"📩 Message from {name}/{user_id}: '{text}'")

    if "@thanos" in text.lower():
        thanos(name, user_id, text)
        return {"status": "bot_mentioned"}
    
    if name.lower() == "krish prabhu":
        lower_text = text.lower()
        if "@undo" in lower_text:
            undo_last_action()
            return {"status": "undo"}
        # Handle @ignore "First Last"
        m = re.search(r"@ignore\s+\"([^\"]+)\"", text)
        if m:
            to_ignore = m.group(1).strip()
            added = add_to_ignored(to_ignore)
            if added:
                post_bot_message(f"Added '{to_ignore}' to the ignore list.", flush=True)
                return {"status": "ignored_added", "user": to_ignore}
            else:
                post_bot_message(f"'{to_ignore}' is already in the ignore list or invalid.", flush=True)
                return {"status": "ignored_exists", "user": to_ignore}

    if name.lower() in ignored:
        print(f"🚫 Ignored user {name}/{user_id}, liking their message.", flush=True)
        like_message(message_id)
        return {"status": "ignored"}

    if not text or not contains_banned(text):
        return {"status": "ok"}

    reckon(name, user_id, text, message_id)

    Thread(target=subgroup_reckon_worker, args=(name, user_id), daemon=True).start()

    return {"status": "processed"}

@app.post("/test-model")
async def test_model(request: Request):
    """Test the classifier with a message; returns only Yes or No. Does not modify training data."""
    data = await request.json()
    text = data.get("text", "")
    if not text or text.isspace():
        return {"error": "Text is required."}

    resp = prompt(text, SYSTEM_MESSAGE, training.get("messages", []), "Here are labeled examples. Treat assistant labels 'Yes' as spam and 'No' as not spam.", "End of examples. Classify the next message. Respond with only Yes or No.")
    label = _parse_yes_no_label(resp)
    if label is None:
        # Return raw so you can inspect if needed
        return {"error": "Model did not respond with Yes/No", "raw": resp}
    return {"label": label}

# Entry point
if __name__ == "__main__":
    print("🚀 Starting GroupMe bot server...")
    if(not STRIKES_FILE.exists()):
        STRIKES_FILE.write_text("{}")
    if(not CONVERSATIONS_FILE.exists()):
        CONVERSATIONS_FILE.write_text("{}")
    # Ensure training file exists
    if not TRAINING_FILE.exists():
        TRAINING_FILE.write_text(json.dumps({"messages": []}))
    if not check_model_availability():
        pull_model()
    Thread(target=accept_invites).start()
    uvicorn.run("anti_clanker:app", host="0.0.0.0", port=7110, reload=True)
