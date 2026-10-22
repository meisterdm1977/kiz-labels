import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from . import config, db
from .labels import from_ru_layout

ROLE_NAMES = {
    "admin": "Администратор",
    "operator": "Оператор",
    "packer": "Упаковщик",
    "station": "Компьютер упаковки",
}


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def check_password(password, stored):
    if not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(actual.hex(), digest)


def normalize_code(code):
    return from_ru_layout("".join(code.split())).upper()


def hash_code(code):
    """Personal codes are looked up by value, so they get a keyed hash instead of a salted one."""
    return hmac.new(config.SECRET_KEY.encode(), normalize_code(code).encode(), hashlib.sha256).hexdigest()


def new_personal_code():
    return "".join(secrets.choice("0123456789") for _ in range(8))


def user_by_code(conn, code):
    if not code or not code.strip():
        return None
    return conn.execute(
        "SELECT * FROM users WHERE code_hash = %s AND active", (hash_code(code),)
    ).fetchone()


def ensure_admin():
    if not config.ADMIN_PASSWORD:
        return
    with db.tx() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO users (name, role, login, password_hash) VALUES (%s, 'admin', %s, %s)",
                ("Администратор", config.ADMIN_LOGIN, hash_password(config.ADMIN_PASSWORD)),
            )


def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with db.pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id = %s AND active", (user_id,)
        ).fetchone()


class LoginRequired(Exception):
    pass


def require(request: Request, *roles):
    user = current_user(request)
    if not user:
        raise LoginRequired()
    if roles and user["role"] not in roles:
        raise HTTPException(403, "Недостаточно прав")
    return user


class Throttle:
    """Counts failed attempts per client and blocks it for a while after too many."""

    def __init__(self, limit, window_seconds):
        self.limit, self.window = limit, window_seconds
        self.failures = {}

    def _recent(self, key):
        now = time.monotonic()
        recent = [t for t in self.failures.get(key, []) if now - t < self.window]
        if recent:
            self.failures[key] = recent
        else:
            self.failures.pop(key, None)
        return recent

    def blocked(self, key):
        return len(self._recent(key)) >= self.limit

    def fail(self, key):
        self._recent(key)
        self.failures.setdefault(key, []).append(time.monotonic())

    def reset(self, key):
        self.failures.pop(key, None)


# Passwords: 10 wrong tries in 15 minutes. Personal codes are scanned all day on one shared
# computer, and all packers share one office address, so their limit is looser: 20 in 15 minutes.
login_throttle = Throttle(10, 15 * 60)
code_throttle = Throttle(20, 15 * 60)


def client_key(request: Request):
    return request.client.host if request.client else "unknown"
