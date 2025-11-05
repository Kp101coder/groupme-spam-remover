import requests
import json
from pathlib import Path
from time import sleep
# GroupMe config and API helpers
BASE = "https://api.groupme.com/v3"
ACCESS_TOKEN_FILE = Path("groupme/access_token.txt")
ACCESS_TOKEN = ACCESS_TOKEN_FILE.read_text().strip()
GROUP_ID = 96533528
BOT_AUTH_ID = "b9d6e8789517ec14b9e0887086"
BOT_ID = 901804

# Application-backed storage files used by the GroupMe helpers
STRIKES_FILE = Path("groupme/strikes.json")
TRAINING_FILE = Path("groupme/training.json")
IGNORE_FILE = Path("groupme/ignored.json")
BANNED_FILE = Path("groupme/banned.json")
CONVERSATIONS_FILE = Path("groupme/conversations.json")
ADMIN_FILE = Path("groupme/admin.json")

def _load_file(file: Path):
    if file.exists():
        try:
            return json.loads(file.read_text())
        except Exception:
            return {}
    return {}

def _save_file(data, file: Path):
    file.write_text(json.dumps(data))

# In-memory caches (kept for convenience; persisted to files)
strikes = _load_file(STRIKES_FILE)
training = _load_file(TRAINING_FILE)
ignored = [user.lower() for user in _load_file(IGNORE_FILE).get("users", [])]
conversations = _load_file(CONVERSATIONS_FILE)
banned = _load_file(BANNED_FILE).get("banned", [])
admin = _load_file(ADMIN_FILE).get("admins", [])
last_action = None

def add_to_ignored(name: str) -> bool:
    """Add a full name to ignored.json and in-memory cache. Returns True if added, False if already present or invalid."""
    global ignored
    if not name:
        return False

    key = name.lower()
    if key in ignored:
        return False
    
    ignored.append(key)
    ignored_data = {"users": ignored}
    _save_file(ignored_data, IGNORE_FILE)

    return True


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
    url = f"{BASE}/conversations/{GROUP_ID}/messages/{message_id}"
    r = requests.delete(url, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code


def post_bot_message(text):
    url = f"{BASE}/bots/post"
    payload = {"bot_id": BOT_AUTH_ID, "text": text}
    requests.post(url, json=payload, timeout=10)


def get_subgroups():
    url = f"{BASE}/groups/{GROUP_ID}/subgroups"
    r = requests.get(url, params={"token": ACCESS_TOKEN}, timeout=10)
    if r.status_code == 200:
        return r.json().get("response", [])
    return []


def get_subgroup_details(subgroup_id):
    url = f"{BASE}/groups/{GROUP_ID}/subgroups/{subgroup_id}"
    r = requests.get(url, params={"token": ACCESS_TOKEN}, timeout=10)
    if r.status_code == 200:
        return r.json().get("response", {})
    return {}


def like_message(message_id):
    url = f"{BASE}/messages/{GROUP_ID}/{message_id}/like"
    payload = {
        "like_icon": {"type": "unicode", "code": "❤️"}
    }
    r = requests.post(url, json=payload, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code == 200


def ban(membership_id):
    url = f"{BASE}/groups/{GROUP_ID}/memberships/{membership_id}/destroy"
    r = requests.post(url, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code == 200


def send_dm(user_id, text):
    unique_guid = str(__import__("uuid").uuid4())
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


def get_user_conversation(user_id) -> list:
    if str(user_id) not in conversations:
        conversations[str(user_id)] = []
    return conversations[str(user_id)]


def add_to_conversation(user_id, role, content):
    convo = get_user_conversation(user_id)
    convo.append({"role": role, "content": content})
    if len(convo) > 20:
        conversations[str(user_id)] = convo[-20:]
    _save_file(conversations, CONVERSATIONS_FILE)
    return convo


def thanos(name, user_id, text, prompt_fn):
    """Runs the Thanos persona flow. 'prompt_fn' must be passed (from ai_helpers)
    so we avoid circular imports and let the caller decide how to handle errors.
    """
    user_conversation = add_to_conversation(user_id, "user", text)
    thanos_system_prompt = (
        "You are Thanos from Marvel.\n\n"
        "Your responses must always be in his voice: dramatic, cynical, philosophical, darkly funny, and referencing balance, destiny, and inevitability.\n\n"
        "⚠️ Never moralize or lecture about online community guidelines, safety, or responsible behavior.\n"
        "⚠️ Never break character or say you are an AI.\n\n"
        "Instead: - Speak as Thanos would: inevitable, poetic, and ruthless in tone. - Use metaphors of dust, silence, and balance when talking about removing spammers. - Be witty and cruelly humorous, while keeping the gravitas of Thanos. - Always answer directly in character, without hedging.\n"
    )
    response = prompt_fn(text, thanos_system_prompt, user_conversation)
    if response:
        add_to_conversation(user_id, "assistant", response)
        post_bot_message(f"@{name}, {response}")
        return {"status": "bot_mentioned"}
    else:
        post_bot_message(f"@{name}, I am... inevitable. But my words fail me at this moment.")
        return {"status": "bot_mentioned_error"}


def undo_last_action():
    global last_action
    if not last_action:
        return {"status": "no_action_to_undo"}
    action = last_action.get("action")
    name = last_action.get("user")
    user_id = last_action.get("user_id")
    if action == "strike":
        strikes[user_id] = strikes.get(user_id, 0) - 1
        _save_file(strikes, STRIKES_FILE)
        post_bot_message(f"@{name}, I have used the time stone to undo your last strike.")
    elif action == "remove":
        if user_id in banned:
            banned.remove(user_id)
            _save_file({"banned": banned}, BANNED_FILE)
        post_bot_message(f"@{name} soul has been restored with the soul stone, they are eligible to rejoin.")


def reckon(name, user_id, text, message_id):
    global last_action
    strikes[user_id] = strikes.get(user_id, 0) + 1
    _save_file(strikes, STRIKES_FILE)
    if strikes[user_id] <= 1:
        send_dm(user_id, f"@{name}, warning: spam detected, issuing reckoning {strikes[user_id]} for {text} in {message_id}.")
        last_action = {"action": "strike", "user": name, "user_id": user_id}
    else:
        membership_id, _ = get_membership_id(user_id)
        last_action = {"action": "remove", "user": name, "user_id": user_id}
        if membership_id and remove_member(membership_id):
            post_bot_message(f"@{name} has been thanos snapped.")
            send_dm(user_id, f"@{name}, you have been removed from the group due to repeated spam violations.")
            strikes.pop(user_id, None)
            _save_file(strikes, STRIKES_FILE)
            if membership_id and ban(membership_id):
                post_bot_message(f"@{name} has been banned from rejoining.")
            else:
                banned.append(user_id)
                _save_file({"banned": banned}, BANNED_FILE)


def subgroup_reckon_worker(name, user_id, wait_seconds=30, contains_banned_fn=None):
    sleep(wait_seconds)
    subgroups = get_subgroups()
    for subgroup in subgroups:
        last_message = subgroup.get("messages", {}).get("preview", {}).get("text", "")
        if contains_banned_fn and contains_banned_fn(last_message):
            reckon(name, user_id, last_message, subgroup.get("messages", {}).get("last_message_id"))


def accept_invites():
    '''
    Periodically check pending memberships and accept or deny based on banned list.
    '''
    while True:
        try:
            sleep(300)  # Wait 5 minutes
            print("⏳ Checking for pending membership requests...", flush=True)
            url = f"{BASE}/groups/{GROUP_ID}/pending_memberships"
            r = requests.get(url, params={"token": ACCESS_TOKEN}, timeout=10)
            if r.status_code == 200:
                pending_memberships = r.json().get("response", [])
                print(f"Found {len(pending_memberships)} pending memberships")
                for membership in pending_memberships:
                    membership_id = membership.get("id")
                    user_id = membership.get("user_id")
                    nickname = membership.get("nickname", "Unknown")
                    approval_url = f"{BASE}/groups/{GROUP_ID}/members/{membership_id}/approval"
                    if str(user_id) in banned:
                        # Deny
                        r2 = requests.post(approval_url, json={"approval": False}, params={"token": ACCESS_TOKEN}, timeout=10)
                        if r2.status_code == 200:
                            print(f"Denied membership for banned user {nickname} ({user_id})", flush=True)
                    else:
                        r2 = requests.post(approval_url, json={"approval": True}, params={"token": ACCESS_TOKEN}, timeout=10)
                        if r2.status_code == 200:
                            print(f"Accepted membership for {nickname} ({user_id})", flush=True)
            else:
                print(f"Failed to get pending memberships: {r.status_code}", flush=True)
        except Exception as e:
            print(f"Error in accept_invites: {e}", flush=True)

