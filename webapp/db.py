from pathlib import Path
import sqlite3

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
BOT_DB_PATH = PROJECT_ROOT / "bot.db"


def get_db_connection():
    conn = sqlite3.connect(BOT_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn