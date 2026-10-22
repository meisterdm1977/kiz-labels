import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone

from fastapi import Request

from . import config, db
from .labels import from_ru_layout

ROLE_NAMES = {
    "admin": "Администратор",
    "packer": "Упаковщик",
}

DEVICE_COOKIE = "kiz_device"
DEVICE_COOKIE_DAYS = 3650


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


def admin_by_password(conn, password):
    """Administrators sign in with the password alone, so each one must have a different password."""
    admins = conn.execute(
        "SELECT * FROM users WHERE role = 'admin' AND active AND password_hash IS NOT NULL ORDER BY id"
    ).fetchall()
    return next((a for a in admins if check_password(password, a["password_hash"])), None)


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def approve_device(conn, name, admin, ip):
    """Registers this browser as an allowed computer and returns the token for its cookie."""
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO devices (name, token_hash, approved_by, last_ip, last_seen_at) VALUES (%s, %s, %s, %s, now())",
        (name, _token_hash(token), admin["id"], ip),
    )
    return token


def current_device(request: Request):
    token = request.cookies.get(DEVICE_COOKIE)
    if not token:
        return None
    with db.pool.connection() as conn:
        device = conn.execute(
            "SELECT * FROM devices WHERE token_hash = %s AND active", (_token_hash(token),)
        ).fetchone()
        if device and (device["last_seen_at"] is None
                       or (datetime.now(timezone.utc) - device["last_seen_at"]).total_seconds() > 300):
            conn.execute("UPDATE devices SET last_seen_at = now(), last_ip = %s WHERE id = %s",
                         (client_key(request), device["id"]))
    return device


def current_admin(request: Request):
    admin_id = request.session.get("admin_id")
    seen = request.session.get("admin_seen", 0)
    if not admin_id or time.time() - seen > config.ADMIN_IDLE_MINUTES * 60:
        request.session.pop("admin_id", None)
        return None
    with db.pool.connection() as conn:
        admin = conn.execute(
            "SELECT * FROM users WHERE id = %s AND role = 'admin' AND active", (admin_id,)
        ).fetchone()
    if not admin:
        request.session.pop("admin_id", None)
        return None
    request.session["admin_seen"] = time.time()
    return admin


class DeviceRequired(Exception):
    pass


class AdminRequired(Exception):
    pass


def require_device(request: Request):
    """Every page except the approval page is open only on computers an administrator allowed."""
    device = current_device(request)
    if not device:
        raise DeviceRequired()
    request.state.device = device
    request.state.admin = current_admin(request)
    return device


def require_admin(request: Request):
    require_device(request)
    if not request.state.admin:
        raise AdminRequired()
    return request.state.admin


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
