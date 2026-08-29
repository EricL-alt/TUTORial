"""
store.py — the .db file behind TUTORial Academy.

One SQLite file, tutorial.db, holds every learner account, every unit or
exercise they have finished, and every achievement they have unlocked.
Passwords are never written down. Only a PBKDF2 digest and its salt are.

Nothing here talks to Flask, so it can be imported and exercised on its own.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "tutorial.db"

PBKDF2_ROUNDS = 240_000
_write_lock = threading.Lock()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION
# ─────────────────────────────────────────────────────────────────────────────

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE,
    display_name  TEXT    NOT NULL DEFAULT '',
    password_hash TEXT    NOT NULL,
    password_salt TEXT    NOT NULL,
    rounds        INTEGER NOT NULL,
    created_at    REAL    NOT NULL,
    last_seen_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS progress (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_key   TEXT    NOT NULL,
    score      REAL    NOT NULL DEFAULT 0,
    finished_at REAL   NOT NULL,
    PRIMARY KEY (user_id, item_key)
);

CREATE TABLE IF NOT EXISTS achievements (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code      TEXT    NOT NULL,
    earned_at REAL    NOT NULL,
    PRIMARY KEY (user_id, code)
);

CREATE INDEX IF NOT EXISTS idx_progress_user ON progress(user_id);
CREATE INDEX IF NOT EXISTS idx_achievements_user ON achievements(user_id);
"""


def init_db() -> None:
    """Create tutorial.db and its tables if they are not there yet."""
    with _write_lock:
        conn = connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# PASSWORDS
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(password: str, salt: str | None = None,
                  rounds: int = PBKDF2_ROUNDS) -> tuple[str, str, int]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds
    )
    return digest.hex(), salt, rounds


def password_matches(password: str, stored_hash: str, salt: str, rounds: int) -> bool:
    candidate, _, _ = hash_password(password, salt, rounds)
    return secrets.compare_digest(candidate, stored_hash)


class StoreError(Exception):
    """Something a learner can read and act on."""


def check_credentials_shape(email: str, password: str) -> None:
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise StoreError("That does not look like an email address.")
    if len(email) > 254:
        raise StoreError("That email address is too long.")
    if len(password or "") < 8:
        raise StoreError("Pick a password of at least eight characters.")
    if len(password or "") > 512:
        raise StoreError("That password is too long.")


# ─────────────────────────────────────────────────────────────────────────────
# ACCOUNTS
# ─────────────────────────────────────────────────────────────────────────────

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def create_user(email: str, password: str, display_name: str = "") -> dict[str, Any]:
    check_credentials_shape(email, password)
    email = normalize_email(email)
    digest, salt, rounds = hash_password(password)
    now = time.time()
    name = (display_name or email.split("@")[0]).strip()[:60]

    with _write_lock:
        conn = connect()
        try:
            cur = conn.execute(
                "INSERT INTO users (email, display_name, password_hash, password_salt,"
                " rounds, created_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
                (email, name, digest, salt, rounds, now, now),
            )
            conn.commit()
            user_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            raise StoreError("An account already uses that email address.")
        finally:
            conn.close()
    return {"id": user_id, "email": email, "display_name": name}


def verify_user(email: str, password: str) -> dict[str, Any]:
    email = normalize_email(email)
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()

    if not row or not password_matches(password or "", row["password_hash"],
                                       row["password_salt"], int(row["rounds"])):
        # One message for both cases, so nobody can probe for registered emails.
        raise StoreError("That email and password pair did not match an account.")

    touch(int(row["id"]))
    return {"id": int(row["id"]), "email": row["email"], "display_name": row["display_name"]}


def get_user(user_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, email, display_name, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def touch(user_id: int) -> None:
    with _write_lock:
        conn = connect()
        try:
            conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?",
                         (time.time(), user_id))
            conn.commit()
        finally:
            conn.close()


def set_password(user_id: int, password: str) -> None:
    if len(password or "") < 8:
        raise StoreError("Pick a password of at least eight characters.")
    digest, salt, rounds = hash_password(password)
    with _write_lock:
        conn = connect()
        try:
            conn.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, rounds = ?"
                " WHERE id = ?", (digest, salt, rounds, user_id))
            conn.commit()
        finally:
            conn.close()


def delete_user(user_id: int) -> None:
    with _write_lock:
        conn = connect()
        try:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# PROGRESS
# ─────────────────────────────────────────────────────────────────────────────

def record_progress(user_id: int, item_key: str, score: float = 1.0) -> None:
    """Mark one unit or one exercise finished. The best score ever seen wins."""
    with _write_lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO progress (user_id, item_key, score, finished_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(user_id, item_key) DO UPDATE SET"
                " score = MAX(score, excluded.score), finished_at = excluded.finished_at",
                (user_id, item_key, float(score), time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def clear_progress(user_id: int) -> None:
    with _write_lock:
        conn = connect()
        try:
            conn.execute("DELETE FROM progress WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM achievements WHERE user_id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()


def progress_map(user_id: int) -> dict[str, dict[str, float]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT item_key, score, finished_at FROM progress WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return {r["item_key"]: {"score": r["score"], "finished_at": r["finished_at"]}
            for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# ACHIEVEMENTS
# ─────────────────────────────────────────────────────────────────────────────

def award(user_id: int, code: str) -> bool:
    """Return True only the first time a code is pinned to this learner."""
    with _write_lock:
        conn = connect()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO achievements (user_id, code, earned_at)"
                " VALUES (?,?,?)", (user_id, code, time.time()))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def earned_map(user_id: int) -> dict[str, float]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT code, earned_at FROM achievements WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return {r["code"]: r["earned_at"] for r in rows}


def stats() -> dict[str, int]:
    conn = connect()
    try:
        users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        done = conn.execute("SELECT COUNT(*) AS n FROM progress").fetchone()["n"]
        badges = conn.execute("SELECT COUNT(*) AS n FROM achievements").fetchone()["n"]
    finally:
        conn.close()
    return {"users": users, "finished": done, "achievements": badges}


# ─────────────────────────────────────────────────────────────────────────────
# COOKIE SIGNING KEY — kept beside the database so logins survive a restart
# ─────────────────────────────────────────────────────────────────────────────

def secret_key() -> bytes:
    path = BASE_DIR / ".flask-secret"
    if path.exists():
        return path.read_bytes()
    key = secrets.token_bytes(48)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key
