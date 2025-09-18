import os
import json
from pathlib import Path
from fastapi import FastAPI, Request
import requests
import uvicorn
import ollama

# Env variables
ACCESS_TOKEN = Path("access_token.txt").read_text().strip()
BOT_AUTH_ID = "b9d6e8789517ec14b9e0887086"
BOT_ID = 901804
STRIKES_FILE = Path("strikes.json")
TRAINING_FILE = Path("training.json")
IGNORE_FILE = Path("ignored.json")
CONVERSATIONS_FILE = Path("conversations.json")
MODEL = "deepseek-r1:14b"

BASE = "https://api.groupme.com/v3"
#BANNED_WORDS = {"ticket", "sale", "free"}
WARN_STRIKES = 1  # delete message on first strike, remove on second
SYSTEM_MESSAGE = (
    "You are validating if a message contains spam or scam content for a GroupMe group for the University of Texas at Austin Pickleball Club."
    "If the message contains spam or scam content, respond with 'Yes'."
    "Your message must contain either 'Yes' or 'No'."
)

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
ignored = load_file(IGNORE_FILE).get("users", [])
conversations = load_file(CONVERSATIONS_FILE)

def save_file(data, file: Path):
    file.write_text(json.dumps(data))

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
    normalized = normalize_text(text)
    response = prompt(normalized, SYSTEM_MESSAGE, training["messages"], True)
    print(f"Model response: {response}")
    if "yes" in response.lower():
        print("Banned content detected by model.")
        return True
    return False

def get_membership_id(group_id, user_id):
    url = f"{BASE}/groups/{group_id}"
    r = requests.get(url, params={"token": ACCESS_TOKEN}, timeout=10)
    members = r.json().get("response", {}).get("members", [])
    for m in members:
        if str(m.get("user_id")) == str(user_id):
            return m.get("id"), m
    return None, None

def remove_member(group_id, membership_id):
    url = f"{BASE}/groups/{group_id}/members/{membership_id}/remove"
    r = requests.post(url, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code == 200

def delete_message(group_id, message_id):
    #DELETE /conversations/:group_id/messages/:message_id
    url = f"{BASE}/conversations/{group_id}/messages/{message_id}"
    r = requests.delete(url, params={"token": ACCESS_TOKEN}, timeout=10)
    return r.status_code == 200

def post_bot_message(text):
    url = f"{BASE}/bots/post"
    payload = {"bot_id": BOT_AUTH_ID, "text": text}
    requests.post(url, json=payload, timeout=10)

def like_message(group_id, message_id):
    '''POST /messages/:group_id/:message_id/like
    {
    "like_icon": {
        "type": "unicode",
        "code": "❤️"
        }
    }'''
    url = f"{BASE}/messages/{group_id}/{message_id}/like"
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
    print(f"Model {MODEL} available: {is_available}")
    print(f"Available models: {available_models}...")
    return is_available

def pull_model() -> None:
    """
    Pull the DeepSeek R1 model if it's not available locally.
    
    Args:
        None
        
    Returns:
        None
    """

    print(f"Pulling model: {MODEL}")
    ollama_model.pull(MODEL)
    print(f"Successfully pulled {MODEL}")

def prompt(message: str, system_message: str, data: list = None, train : bool = False) -> str:
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
            for entry in data:
                messages.append(entry)

            if train:
                messages.append({"role":"user", "content":"End of training data. Validate the next message."})

        # Add current message
        messages.append({"role": "user", "content": message})

        # Generate response
        response = ollama_model.chat(
            model=MODEL,
            messages=messages,
            stream=False,
            think=False
        )
        
        response_content = response['message']['content']

        if "</think>" in response_content:
            response_content = response_content[response_content.find("</think>") + 8:].strip()             
            
        return response_content
        
    except Exception as e:
        print(f"Error generating response: {e}")
        return None

def thanos(name, user_id, text):
    print(f"🤖 Bot mention detected in message from {name}/{user_id}.")

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
    if user_id == "0" or user_id == BOT_ID:
        return {"status": "ignored"}

    name = payload.get("name", "Unknown")
    text = payload.get("text", "")
    group_id = payload.get("group_id")
    message_id = payload.get("id")

    print(f"📩 Message from {name}/{user_id}: '{text}'")

    if "@thanos" in text.lower():
        thanos(name, user_id, text)

    if int(user_id) in ignored:
        print(f"🚫 Ignored user {name}/{user_id}, liking their message.")
        like_message(group_id, message_id)
        return {"status": "ignored"}

    if not text or not contains_banned(text):
        return {"status": "ok"}

    print(f"🚨 Banned word detected in message from {name}/{user_id}: '{text}'")
    key = f"{user_id}:{name}"
    strikes[key] = strikes.get(key, 0) + 1
    save_file(strikes, STRIKES_FILE)

    if strikes[key] <= WARN_STRIKES:
        post_bot_message(f"@{name}, warning: spam detected, issueing reckoning {strikes[key]} of {WARN_STRIKES}.")
        print(f"🗑️ Delete message from {name} success: {delete_message(group_id, message_id)}")
        print(f"⚠️ Warning issued to {name} (strike {strikes[key]})")
    else:
        membership_id, _ = get_membership_id(group_id, user_id)
        print(f"🗑️ Delete message from {name} success: {delete_message(group_id, message_id)}")
        if membership_id and remove_member(group_id, membership_id):
            post_bot_message(f"@{name} has been thanos snapped.")
            strikes.pop(key, None)
            save_file(strikes, STRIKES_FILE)
            print(f"🗑️ Removed {name} from group.")
        else:
            post_bot_message(f"Failed to remove @{name}, please snap manually.")
            print(f"❌ Failed to remove {name}, membership ID not found.")

    return {"status": "processed"}

@app.post("/train-bot")
async def bot_train(request: Request):
    data = await request.json()
    text = data.get("text", "")
    output = {}
    output["response"] = contains_banned(text)
    output["training_data"] = training
    output["input"] = text
    training["messages"].append({"role": "user", "content": text})
    training["messages"].append({"role": "assistant", "content": str(output["response"])})
    save_file(training, TRAINING_FILE)
    return output

@app.delete("/remove-training-data/{num}")
async def remove_training_data(num: int):
    '''Removes the last n training data entries'''
    if num <= 0 or num > len(training["messages"]):
        return {"error": "Invalid number of entries to remove."}

    training["messages"] = training["messages"][:-num]
    save_file(training, TRAINING_FILE)
    return {"status": "success", "training_data": training, "remaining": len(training["messages"])}

@app.get("/training-data")
async def get_training_data():
    '''Get training data entries'''
    training["messages"] = training["messages"]
    return {"status": "success", "training_data": training}

@app.get("/conversations/{user_id}")
async def get_user_conversations(user_id: str):
    '''Get conversation history for a specific user'''
    user_conversation = get_user_conversation(user_id)
    return {"status": "success", "user_id": user_id, "conversation": user_conversation}

@app.delete("/conversations/{user_id}")
async def clear_user_conversation(user_id: str):
    '''Clear conversation history for a specific user'''
    user_key = str(user_id)
    if user_key in conversations:
        conversations[user_key] = []
        save_file(conversations, CONVERSATIONS_FILE)
        return {"status": "success", "message": f"Conversation cleared for user {user_id}"}
    return {"status": "error", "message": f"No conversation found for user {user_id}"}

# Entry point
if __name__ == "__main__":
    print("🚀 Starting GroupMe bot server...")
    if(not STRIKES_FILE.exists()):
        STRIKES_FILE.write_text("{}")
    if(not CONVERSATIONS_FILE.exists()):
        CONVERSATIONS_FILE.write_text("{}")
    if not check_model_availability():
        pull_model()
    uvicorn.run("anti_clanker:app", host="0.0.0.0", port=7110, reload=True)
