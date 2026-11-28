"""The service in front of the engine: who is asking, which folder, which conversation, and what may run. Loopback only.

  AGENTD_HOME=... MODEL_KEY=... python3 service/agentd.py            the service decides every tool call
  AGENTD_HOME=... MODEL_KEY=... python3 service/agentd.py --naive    the first try, to see it break"""
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth  # noqa: E402
import policy  # noqa: E402
from runner import NaiveRunner, PolicyRunner  # noqa: E402
from store import Store  # noqa: E402

MAX_BODY = 64 * 1024


class Service(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, home, runner):
        super().__init__(("127.0.0.1", port), Handler)
        self.home = os.path.realpath(home)
        self.store = Store(os.path.join(self.home, "data", "agentd.db"))
        self.runner = runner
        runner.store = self.store
        self.busy, self.busy_lock = set(), threading.Lock()

    def user_dir(self, user):
        """Each user has one folder: tenants/<tenant>/<name>. The names were checked when the user was made."""
        return os.path.join(self.home, "tenants", user["tenant"], user["id"].split("@")[0])


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

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method):
        if method == "GET" and self.path == "/health":
            return self.reply(200, {"ok": True})
        # identity comes first: no path below can be reached without a user
        user = auth.user_from_header(self.server.store, self.headers.get("authorization"))
        if user is None:
            return self.error(401, "unauthorized", "send Authorization: Bearer <your token>")
        try:
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if 0 <= length <= MAX_BODY else None
        except (ValueError, RecursionError):
            body = None
        if not isinstance(body, dict):
            return self.error(400, "bad_request", "the body must be a small JSON object")
        parts = self.path.strip("/").split("/")
        if parts[0] == "tokens":
            return self.tokens(method, user, parts, body)
        if parts == ["policy"]:
            return self.policy(method, user, body)
        if parts == ["sessions"] and method == "GET":
            return self.reply(200, {"sessions": self.server.store.sessions_of(user["id"])})
        if parts == ["sessions"] and method == "POST":
            return self.new_session(user, body)
        if len(parts) >= 2 and parts[0] == "sessions":
            session = self.server.store.session(parts[1])
            # someone else's session and no such session look the same
            if session is None or session["user_id"] != user["id"]:
                return self.error(404, "not_found", "you have no session with that id")
            if len(parts) == 2 and method == "GET":
                return self.reply(200, {k: session[k] for k in ("id", "project", "turns")})
            if parts[2:] == ["messages"] and method == "POST":
                return self.message(user, session, body)
        self.error(404, "not_found", "no such path")

    def policy(self, method, user, body):
        """The company policy of the admin's own tenant. Each change is a new version."""
        if user["role"] != "admin":
            return self.error(403, "forbidden", "admins only")
        store = self.server.store
        if method == "GET":
            row = store.policy(user["tenant"])
            return self.reply(200, {"version": row["version"], "policy": json.loads(row["body"])} if row
                              else {"version": 0, "policy": None})
        if method != "PUT":
            return self.error(404, "not_found", "no such path")
        base = body.get("base_version")
        try:
            if type(base) is not int:
                raise policy.PolicyError('send {"base_version": the version you changed, "policy": {...}}')
            policy.parse_company(body.get("policy"))          # a policy nobody can read must not be published
        except policy.PolicyError as e:
            return self.error(400, "bad_policy", str(e))
        version = store.publish_policy(user["tenant"], base, json.dumps(body["policy"]), user["id"])
        if version is None:
            return self.error(409, "stale_version", "someone published a newer version; read it first")
        self.reply(200, {"version": version})

    def tokens(self, method, user, parts, body):
        """Admins make and revoke tokens. An admin manages their own tenant only."""
        if user["role"] != "admin":
            return self.error(403, "forbidden", "admins only")
        target = body.get("user") if method == "POST" else parts[1] if len(parts) == 2 else None
        if not isinstance(target, str) or target.partition("@")[2] != user["tenant"]:
            return self.error(404, "not_found", "no such user in your tenant")
        if method == "POST" and len(parts) == 1:
            try:
                token = auth.create_user(self.server.store, target, body.get("role", "user"))
            except Exception:
                return self.error(400, "bad_request", "a user is name@tenant, a role is user or admin")
            return self.reply(201, {"user": target, "token": token})       # shown once
        if method == "DELETE" and self.server.store.user(target):
            self.server.store.revoke_user(target)
            return self.reply(200, {"user": target, "revoked": True})
        self.error(404, "not_found", "no such user in your tenant")

    def project_dir(self, user, name):
        """The project folder, only if it really is inside this user's folder."""
        try:
            auth.check_name(name)
        except ValueError:
            return None
        base = self.server.user_dir(user)
        real = os.path.realpath(os.path.join(base, name))
        return real if real.startswith(base + os.sep) and os.path.isdir(real) else None

    def new_session(self, user, body):
        if self.project_dir(user, body.get("project")) is None:
            return self.error(404, "not_found", "you have no project with that name")
        mode = body.get("mode", "ask")
        if mode not in ("ask", "bypass"):
            return self.error(400, "bad_request", "mode is ask or bypass")
        row = self.server.store.policy(user["tenant"])
        if mode == "bypass" and row and json.loads(row["body"]).get("locks", {}).get("disable_bypass"):
            return self.error(403, "bypass_disabled", "your company does not allow sessions that run without asking")
        session = "s-" + secrets.token_hex(8)
        self.server.store.add_session(session, user["id"], body["project"], mode)
        self.reply(201, {"id": session})

    def message(self, user, session, body):
        project = self.project_dir(user, session["project"])
        text = body.get("text")
        if project is None or not isinstance(text, str) or not text.strip():
            return self.error(400, "bad_request", 'send {"text": "..."} to a session whose project exists')
        server = self.server
        with server.busy_lock:                    # two turns at once would overwrite one saved conversation
            if session["id"] in server.busy:
                return self.error(409, "session_busy", "one message at a time in a session")
            server.busy.add(session["id"])
        try:
            folder = os.path.join(server.user_dir(user), ".sessions")
            os.makedirs(folder, mode=0o700, exist_ok=True)
            result = server.runner.run(server.user_dir(user), project,
                                       os.path.join(folder, session["id"] + ".json"), text,
                                       user["tenant"], session["mode"])
            if "error" in result:
                return self.reply(502, {"error": "engine", "message": result["error"]})
            server.store.count_turn(session["id"])
            self.reply(200, result)
        finally:
            with server.busy_lock:
                server.busy.discard(session["id"])


def main():
    args = (os.environ.get("MODEL_NAME", "deepseek-flash"),
            os.environ.get("MODEL_URL", "https://api.deepseek.com/anthropic"), os.environ["MODEL_KEY"])
    runner = NaiveRunner(*args) if "--naive" in sys.argv else PolicyRunner(*args)
    service = Service(int(os.environ.get("AGENTD_PORT", "8700")), os.environ["AGENTD_HOME"], runner)
    if os.environ.get("AGENTD_POLICY"):           # the first policy of a tenant, from a file: TENANT=path
        tenant, _, path = os.environ["AGENTD_POLICY"].partition("=")
        if service.store.policy(tenant) is None:
            with open(path, encoding="utf-8") as f:
                body = json.load(f)
            policy.parse_company(body)
            service.store.publish_policy(tenant, 0, json.dumps(body), "setup")
    print("agentd listening on 127.0.0.1:%d (%s)" % (service.server_address[1], type(runner).__name__), flush=True)
    service.serve_forever()


if __name__ == "__main__":
    main()
