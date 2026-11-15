import os
import sys
from pyrogram import Client

# Load environment variables from .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
                if _k not in os.environ:
                    os.environ[_k] = _v

api_id_raw = os.environ.get("API_ID")
API_ID = int(api_id_raw) if api_id_raw else None
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    print("❌ Missing API_ID, API_HASH, or BOT_TOKEN in .env file!")
    sys.exit(1)

# Initialize Pyrogram client from environment variables
app = Client(
    "test_pyro_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@app.on_message()
async def handle_message(client, message):
    print(f"📥 RECEIVED: {message.text} | From User ID: {message.from_user.id if message.from_user else 'Unknown'}")
    await message.reply_text("🤖 Echo: working!")

print("🚀 Test bot is running... Send a message to it now.")
app.run()
