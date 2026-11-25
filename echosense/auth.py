"""Device-local owner account and revocable server-side sessions."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

COOKIE = "echosense_session"
SESSION_SECONDS = 12 * 60 * 60
ROLE_PERMISSIONS = {
    "owner": ("view", "train", "configure", "accounts"),
    "trainer": ("view", "train"),
    "technician": ("view", "configure"),
    "viewer": ("view",),
}


def password_hash(password: str, salt: bytes) -> bytes:
    # OWASP's memory-constrained scrypt profile (16 MiB, five passes).
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=5,
                          maxmem=32 * 1024 * 1024, dklen=32)


def validate_credentials(username: str, password: str):
    if not re.fullmatch(r"[a-zA-Z0-9._@+-]{3,64}", username):
        raise ValueError("Use a username of 3–64 letters, numbers, or . _ @ + -")
    if not 15 <= len(password) <= 128:
        raise ValueError("Use a password or passphrase of 15–128 characters.")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthStore:
    """Device owner and explicitly invited caregivers. No open registration."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive permissions before SQLite writes credentials.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
                    salt BLOB NOT NULL, password BLOB NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('owner','trainer','technician','viewer')),
                    disabled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invitations (
                    digest TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    role TEXT NOT NULL, expires REAL NOT NULL
                );
            """)
            # Upgrade the original single-account database without resetting passwords or cookies.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account'").fetchone():
                db.execute("INSERT OR IGNORE INTO users(id,username,salt,password,role) "
                           "SELECT id,username,salt,password,'owner' FROM account")
                db.execute("DROP TABLE account")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
            if "user_id" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        self.setup_code = secrets.token_urlsafe(24)
        self._dummy_salt = secrets.token_bytes(16)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def has_account(self) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM users WHERE role='owner'").fetchone() is not None

    def create_owner(self, username: str, password: str, code: str):
        if not hmac.compare_digest(code.encode(), self.setup_code.encode()):
            raise PermissionError("The setup code is incorrect. Check the EchoSense terminal.")
        validate_credentials(username, password)
        salt = secrets.token_bytes(16)
        hashed = password_hash(password, salt)
        with self.connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM users WHERE role='owner'").fetchone():
                    raise ValueError("This device already has a parent account. Sign in instead.")
                db.execute("INSERT INTO users(id,username,salt,password,role) VALUES(1,?,?,?,'owner')",
                           (username, salt, hashed))
            except sqlite3.IntegrityError:
                raise ValueError("This device already has a parent account. Sign in instead.") from None

    def verify(self, username: str, password: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=? AND disabled=0", (username,)).fetchone()
        expected = row["password"] if row else bytes(32)
        actual = password_hash(password, row["salt"] if row else self._dummy_salt)
        matched = hmac.compare_digest(expected, actual)
        return bool(row and hmac.compare_digest(row["username"].encode(), username.encode()) and matched)

    def create_session(self, user_id: int = 1) -> tuple[str, dict]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = time.time() + SESSION_SECONDS
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
            if not db.execute("SELECT 1 FROM users WHERE id=? AND disabled=0", (user_id,)).fetchone():
                raise ValueError("This caregiver account is unavailable.")
            db.execute("INSERT INTO sessions(digest,csrf,expires,user_id) VALUES(?,?,?,?)",
                       (token_hash(token), csrf, expires, user_id))
        return token, {"digest": token_hash(token), "csrf": csrf, "expires": expires}

    def session(self, token: str) -> dict | None:
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute("SELECT s.*, u.username, u.role FROM sessions s JOIN users u ON s.user_id=u.id "
                             "WHERE s.digest=? AND s.expires>? AND u.disabled=0",
                             (token_hash(token), time.time())).fetchone()
        return {**dict(row), "permissions": list(ROLE_PERMISSIONS[row["role"]])} if row else None

    def revoke(self, digest: str):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (digest,))

    def change_password(self, username: str, current: str, password: str) -> bool:
        validate_credentials(username, password)
        salt = secrets.token_bytes(16)
        hashed = password_hash(password, salt)
        # Compare and replace under the same transaction so concurrent changes cannot win twice.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM users WHERE username=? AND disabled=0", (username,)).fetchone()
            if not row or not hmac.compare_digest(row["password"], password_hash(current, row["salt"])):
                return False
            db.execute("UPDATE users SET salt=?,password=? WHERE id=?", (salt, hashed, row["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        return True

    def reset_password(self, password: str):
        """Local terminal recovery; invalidates every existing session."""
        with self.connect() as db:
            row = db.execute("SELECT id,username FROM users WHERE role='owner'").fetchone()
        if not row:
            raise ValueError("No parent account exists. Open /login to set one up first.")
        validate_credentials(row["username"], password)
        salt = secrets.token_bytes(16)
        hashed = password_hash(password, salt)
        with self.connect() as db:
            db.execute("UPDATE users SET salt=?, password=? WHERE id=?", (salt, hashed, row["id"]))
            db.execute("DELETE FROM sessions")

    def user(self, username: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT id,username,role,disabled FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,username,role,disabled FROM users ORDER BY id")]

    def invite(self, username: str, role: str) -> str:
        validate_credentials(username, "temporary validation passphrase")
        if role not in ROLE_PERMISSIONS or role == "owner":
            raise ValueError("Choose viewer, trainer, or technician access.")
        code = secrets.token_urlsafe(24)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise ValueError("That username already exists.")
            db.execute("INSERT OR REPLACE INTO invitations VALUES(?,?,?,?)",
                       (token_hash(code), username, role, time.time() + 86400))
        return code

    def accept_invitation(self, username: str, password: str, code: str) -> int:
        validate_credentials(username, password)
        salt = secrets.token_bytes(16)
        hashed = password_hash(password, salt)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM invitations WHERE digest=? AND username=? AND expires>?",
                             (token_hash(code), username, time.time())).fetchone()
            if not row:
                raise ValueError("The invitation is invalid or expired. Ask the owner for a new code.")
            try:
                result = db.execute("INSERT INTO users(username,salt,password,role) VALUES(?,?,?,?)",
                                    (username, salt, hashed, row["role"]))
            except sqlite3.IntegrityError:
                raise ValueError("That username already exists. Sign in instead.") from None
            db.execute("DELETE FROM invitations WHERE digest=?", (row["digest"],))
            return result.lastrowid

    def disable_user(self, user_id: int, disabled: bool):
        with self.connect() as db:
            row = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if not row or row["role"] == "owner":
                raise ValueError("The owner cannot be disabled.")
            db.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def allow_attempt(self, remote: str) -> bool:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE expires<=?", (now,))
            # Global bound also limits hashing work from many source addresses.
            keys = ["global", token_hash(remote)]
            for key in keys:
                db.execute("INSERT INTO attempts VALUES(?, 1, ?) "
                           "ON CONFLICT(key) DO UPDATE SET count=count+1", (key, now + 60))
            counts = [db.execute("SELECT count FROM attempts WHERE key=?", (key,)).fetchone()[0]
                      for key in keys]
        return counts[0] <= 30 and counts[1] <= 8
