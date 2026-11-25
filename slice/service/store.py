"""The SQLite store: users, sessions, session tokens, model calls and policy versions.
Only the service and the key proxy open it. It lives outside every sandbox."""
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('user','admin')),
  token_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), tenant TEXT NOT NULL, project TEXT NOT NULL,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS session_tokens (
  token_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, started_at REAL NOT NULL,
  state TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, estimated INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS policy_versions (
  version INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL, published_by TEXT NOT NULL, published_at REAL NOT NULL);
"""


class Store:
    """One connection per thread, WAL mode. clock.now() gives the time, so tests control it."""

    def __init__(self, path, clock):
        self.path, self.clock = path, clock
        self.local = threading.local()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self._db().executescript(SCHEMA)
        os.chmod(path, 0o600)

    def _db(self):
        db = getattr(self.local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)     # every statement commits
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            self.local.db = db
        return db

    def _one(self, sql, *values):
        row = self._db().execute(sql, values).fetchone()
        return dict(row) if row else None

    # users
    def add_user(self, user_id, tenant, role, token_hash):
        self._db().execute("INSERT INTO users (id, tenant, role, token_hash, created_at) VALUES (?,?,?,?,?)",
                           (user_id, tenant, role, token_hash, self.clock.now()))

    def user_by_token(self, token_hash):
        return self._one("SELECT id, tenant, role FROM users WHERE token_hash=? AND revoked_at IS NULL", token_hash)

    def revoke_user(self, user_id):
        self._db().execute("UPDATE users SET revoked_at=? WHERE id=?", (self.clock.now(), user_id))

    # sessions
    def add_session(self, session_id, user_id, tenant, project):
        self._db().execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                           (session_id, user_id, tenant, project, self.clock.now()))

    def session(self, session_id):
        return self._one("SELECT * FROM sessions WHERE id=?", session_id)

    def add_session_token(self, token_hash, session_id, expires_at):
        self._db().execute("INSERT INTO session_tokens VALUES (?,?,?)", (token_hash, session_id, expires_at))

    def session_by_token(self, token_hash):
        """The session a live token belongs to. A token of a revoked user is dead too."""
        return self._one(
            "SELECT s.* FROM session_tokens t JOIN sessions s ON s.id=t.session_id JOIN users u ON u.id=s.user_id "
            "WHERE t.token_hash=? AND t.expires_at>? AND u.revoked_at IS NULL", token_hash, self.clock.now())

    def end_session_tokens(self, session_id):
        self._db().execute("DELETE FROM session_tokens WHERE session_id=?", (session_id,))

    # model calls
    def start_call(self, session_id):
        return self._db().execute("INSERT INTO calls (session_id, started_at, state) VALUES (?,?,'open')",
                                  (session_id, self.clock.now())).lastrowid

    def settle_call(self, call_id, state, input_tokens, output_tokens, estimated):
        self._db().execute("UPDATE calls SET state=?, input_tokens=?, output_tokens=?, estimated=? WHERE id=?",
                           (state, input_tokens, output_tokens, int(estimated), call_id))

    def calls(self, session_id):
        rows = self._db().execute("SELECT * FROM calls WHERE session_id=? ORDER BY id", (session_id,))
        return [dict(r) for r in rows]

    # policy
    def add_policy(self, body, by):
        return self._db().execute("INSERT INTO policy_versions (body, published_by, published_at) VALUES (?,?,?)",
                                  (body, by, self.clock.now())).lastrowid

    def policy(self):
        return self._one("SELECT * FROM policy_versions ORDER BY version DESC LIMIT 1")
