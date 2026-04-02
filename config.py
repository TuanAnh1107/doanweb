import os
import secrets
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
    DEBUG = _env_flag("FLASK_DEBUG", False)
    DB_ENGINE = os.getenv("DB_ENGINE", "sqlite").lower()
    DATABASE_URL = os.getenv("DATABASE_URL")
    SQLITE_PATH = os.getenv("SQLITE_PATH", os.path.join(BASE_DIR, "instance", "quanlylophoc.db"))
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_NAME = os.getenv("DB_NAME", "quan_ly_lop_hoc")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", False)
