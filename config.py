
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.get("BOT_TOKEN")
ADMIN_ID = int(os.get("ADMIN_ID", "0"))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))

# Project directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
DATABASE_PATH = os.path.join(DATA_DIR, "bot.db")

# Make sure data folder exists
os.makedirs(DATA_DIR, exist_ok=True)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing from .env")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID missing from .env")
