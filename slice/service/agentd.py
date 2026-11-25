"""The service: identity, policy decisions, session runner and the audit log. Listens on the loopback only.

Run: AGENTD_HOME=... python3 service/agentd.py"""
import hmac
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth  # noqa: E402
import policy  # noqa: E402
from audit import AuditLog, verify  # noqa: E402
from runner import Job, Runner  # noqa: E402
from store import Store  # noqa: E402

SLICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_BODY = 64 * 1024
PROXY_EVENTS = ("model_call", "model_usage")


class Home:
    """The files under AGENTD_HOME. Only tenants/<tenant>/<name>/ is ever opened to a sandbox."""

    def __init__(self, path, clock=None):
        self.path = os.path.realpath(path)
        self.clock = clock or auth.Clock()
        os.makedirs(os.path.join(self.path, "data"), mode=0o700, exist_ok=True)
        self.store = Store(os.path.join(self.path, "data", "agentd.db"), self.clock)
        self.audit_folder = os.path.join(self.path, "audit")
        self.anchor = os.path.join(self.path, "anchor")
        secret = os.path.join(self.path, "data", "internal")
        if not os.path.exists(secret):
            with open(os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write(secrets.token_urlsafe(32))
        with open(secret, encoding="ascii") as f:
            self.secret = f.read().strip()

    def publish_policy(self, body, by):
        policy.parse_rules(body.get("rules", {}), "company")      # a policy with a bad rule is never stored
        return self.store.add_policy(json.dumps(body), by)

    def rules(self):
        row = self.store.policy()
        if row is None:
            raise policy.PolicyError("no company policy is published")
        body = json.loads(row["body"])
        return policy.parse_rules(body.get("rules", {}), "company"), body.get("locks", {})

    def user_dir(self, user):
        return os.path.join(self.path, "tenants", user["tenant"], user["id"].split("@")[0])


class Service(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, home, proxy_port, model, mode="seatbelt"):
        super().__init__(("127.0.0.1", port), Handler)
        self.home = home
        self.audit = AuditLog(home.audit_folder, home.anchor, home.clock)
        self.runner = Runner(self.audit, home.rules, proxy_port, model, mode)
        self.busy = set()
        self.busy_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, code, message):
        self.reply(status, {"error": code, "message": message})

    def body(self):
        try:
            length = int(self.headers.get("content-length") or 0)
            if not 0 <= length <= MAX_BODY:
                return None
            value = json.loads(self.rfile.read(length) or b"{}")
            return value if isinstance(value, dict) else None
        except (ValueError, RecursionError):
            return None

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True})
        self.error(404, "not_found", "no such path")

    def do_POST(self):
        if self.path == "/internal/audit":
            return self.internal_audit()
        server = self.server
        user = auth.user_from_header(server.home.store, self.headers.get("authorization"))
        parts = self.path.strip("/").split("/")
        known = parts[0] if parts[0] in ("sessions", "audit") else "other"      # never log text the caller chose
        server.audit.append(type="request", source="service", user=user and user["id"],
                            tenant=user and user["tenant"], decision="allow" if user else "deny",
                            detail={"path": known})
        if user is None:
            return self.error(401, "unauthorized", "send Authorization: Bearer <your token>")
        body = self.body()
        if body is None:
            return self.error(400, "bad_request", "the body must be a small JSON object")
        if parts == ["sessions"]:
            return self.new_session(user, body)
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "messages":
            return self.message(user, parts[1], body)
        if parts == ["audit", "verify"]:
            if user["role"] != "admin":
                return self.error(403, "forbidden", "admins only")
            result = verify(server.home.audit_folder, server.home.anchor)
            return self.reply(200, {k: result[k] for k in ("ok", "checked", "broken_at", "why")})
        self.error(404, "not_found", "no such path")

    def project_dir(self, user, name):
        """The project folder, only if it really is inside this user's folder."""
        try:
            auth.check_name(name)
        except ValueError:
            return None
        base = self.server.home.user_dir(user)
        real = os.path.realpath(os.path.join(base, name))
        return real if real.startswith(base + os.sep) and os.path.isdir(real) else None

    def new_session(self, user, body):
        if self.project_dir(user, body.get("project")) is None:
            return self.error(404, "not_found", "you have no project with that name")
        session = "s-" + secrets.token_hex(8)
        self.server.home.store.add_session(session, user["id"], user["tenant"], body["project"])
        self.reply(201, {"id": session, "state": "idle"})

    def message(self, user, session_id, body):
        server = self.server
        session = server.home.store.session(session_id)
        if session is None or session["user_id"] != user["id"]:   # the same answer: others' ids are not confirmed
            return self.error(404, "not_found", "you have no session with that id")
        project = self.project_dir(user, session["project"])
        text = body.get("text")
        if project is None or not isinstance(text, str) or not text.strip():
            return self.error(400, "bad_request", 'send {"text": "..."} to a session whose project exists')
        with server.busy_lock:
            # one engine per user folder at a time: two would share files
            if user["id"] in server.busy:
                return self.error(409, "session_busy", "one message at a time")
            server.busy.add(user["id"])
        events = None
        try:
            token = auth.new_session_token(server.home.store, session_id)
            job = Job(user["tenant"], user["id"], session_id, server.home.user_dir(user), project)
            self.send_response(200)
            self.send_header("content-type", "application/x-ndjson")
            self.end_headers()
            events = server.runner.run(job, text, token)
            for event in events:
                self.wfile.write((json.dumps(event) + "\n").encode())
                self.wfile.flush()
        finally:
            if events is not None:
                events.close()                  # stops the engine if the caller went away
            server.home.store.end_session_tokens(session_id)
            with server.busy_lock:
                server.busy.discard(user["id"])

    def internal_audit(self):
        """For the key proxy only: it adds its events here, so the log has one writer."""
        server = self.server
        given = (self.headers.get("x-internal-secret") or "").encode()
        if not hmac.compare_digest(given, server.home.secret.encode()):
            return self.error(401, "unauthorized", "internal")
        body = self.body() or {}
        session = server.home.store.session(str(body.get("session")))
        if session is None or body.get("type") not in PROXY_EVENTS or not isinstance(body.get("detail"), dict):
            return self.error(400, "bad_request", "internal")
        server.audit.append(tenant=session["tenant"], user=session["user_id"], session=session["id"],
                            type=body["type"], source="proxy", detail=body["detail"])
        self.reply(200, {"ok": True})


def main():
    home = Home(os.environ["AGENTD_HOME"])
    if home.store.policy() is None:
        with open(os.path.join(SLICE, "config", "policy.example.json"), encoding="utf-8") as f:
            home.publish_policy(json.load(f), "setup")
    mode = os.environ.get("AGENTD_SANDBOX", "seatbelt")
    service = Service(int(os.environ.get("AGENTD_PORT", "8700")), home,
                      int(os.environ.get("KEYPROXY_PORT", "8701")),
                      os.environ.get("MODEL_NAME", "deepseek-flash"), mode)
    if mode == "none":
        print("WARNING: no sandbox. For tests only.", flush=True)
    print("agentd listening on 127.0.0.1:%d" % service.server_address[1], flush=True)
    service.serve_forever()


if __name__ == "__main__":
    main()
