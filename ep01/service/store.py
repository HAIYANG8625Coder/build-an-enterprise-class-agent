"""Users and sessions, in SQLite. A token is stored only as its SHA-256."""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('user','admin')),
  token_hash TEXT UNIQUE, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), project TEXT NOT NULL,
  turns INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
"""


class Store:
    def __init__(self, path):
        self.path, self.local = path, threading.local()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self._db().executescript(SCHEMA)
        os.chmod(path, 0o600)

    def _db(self):                                   # one connection per thread
        db = getattr(self.local, "db", None)
        if db is None:
            db = self.local.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
        return db

    def _all(self, sql, *values):
        return [dict(r) for r in self._db().execute(sql, values)]

    def add_user(self, user_id, tenant, role, token_hash):
        """A new user, or a new token for one that exists."""
        self._db().execute(
            "INSERT INTO users VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash, "
            "role=excluded.role", (user_id, tenant, role, token_hash, time.time()))

    def user(self, user_id):
        rows = self._all("SELECT id, tenant, role FROM users WHERE id=?", user_id)
        return rows[0] if rows else None

    def user_by_token(self, token_hash):
        rows = self._all("SELECT id, tenant, role FROM users WHERE token_hash=?", token_hash)
        return rows[0] if rows else None

    def revoke_user(self, user_id):
        self._db().execute("UPDATE users SET token_hash=NULL WHERE id=?", (user_id,))

    def add_session(self, session_id, user_id, project):
        self._db().execute("INSERT INTO sessions (id, user_id, project, created_at) VALUES (?,?,?,?)",
                           (session_id, user_id, project, time.time()))

    def session(self, session_id):
        rows = self._all("SELECT * FROM sessions WHERE id=?", session_id)
        return rows[0] if rows else None

    def sessions_of(self, user_id):
        return self._all("SELECT id, project, turns FROM sessions WHERE user_id=? ORDER BY created_at DESC", user_id)

    def count_turn(self, session_id):
        self._db().execute("UPDATE sessions SET turns=turns+1 WHERE id=?", (session_id,))
