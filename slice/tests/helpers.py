"""Test doubles: a clock the test moves, a model provider on the loopback, and the whole service in one process."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SLICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for folder in ("service", "proxy"):
    sys.path.insert(0, os.path.join(SLICE, folder))
import agentd  # noqa: E402
import auth  # noqa: E402
import keyproxy  # noqa: E402

MAC = sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec")
MODE = "seatbelt" if MAC else "none"
KEY = "test-key-not-a-real-one"
MODEL = "stub-model"
PROJECT = os.path.join(SLICE, "engine", "project")
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with open(os.path.join(SLICE, "config", "policy.example.json"), encoding="utf-8") as _f:
    POLICY = json.load(_f)


class FakeClock:
    def __init__(self):
        self.seconds = time.time()

    def now(self):
        return self.seconds


def say(text):
    return {"content": [{"type": "text", "text": text}]}


def call(name, **args):
    return {"content": [{"type": "tool_use", "id": None, "name": name, "input": args}]}


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):                       # nothing should ask this way; if something does, remember what it carried
        self.server.seen.append({"key": self.headers.get("x-api-key"), "body": None, "path": self.path})
        self.send_error(405)

    def do_POST(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        server.seen.append({"key": self.headers.get("x-api-key"), "body": body, "path": self.path})
        step = server.script.pop(0) if server.script else say("the script is over")
        if "sleep" in step:
            time.sleep(step["sleep"])
            step = say("too late")
        content = step.get("content", [])
        for i, block in enumerate(content):
            if block["type"] == "tool_use":
                block["id"] = "t%d_%d" % (len(server.seen), i)
        tool = any(b["type"] == "tool_use" for b in content)
        reply = {"id": "msg_%d" % len(server.seen), "type": "message", "role": "assistant", "model": MODEL,
                 "content": content, "stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None,
                 "usage": {"input_tokens": 100, "output_tokens": 20}}
        if step.get("no_usage"):
            del reply["usage"]
        data = step["raw"] if "raw" in step else json.dumps(reply).encode()
        self.send_response(step.get("status", 200))
        for name, value in step.get("headers", {}).items():
            self.send_header(name, value)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def upstream(script):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    server.script, server.seen = list(script), []
    server.daemon_threads = True
    return serve(server)


class Stack:
    """The service and the key proxy on free ports, a fake provider behind them, two users and an admin."""

    def __init__(self, script=(), mode=MODE, upstream_timeout=10, policy_body=POLICY):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.home = agentd.Home(os.path.join(self.tmp.name, "home"), self.clock)
        if policy_body is not None:
            self.home.publish_policy(policy_body, "test")
        self.tokens = {name: auth.create_user(self.home.store, name + "@acme", "admin" if name == "root" else "user")
                       for name in ("lin", "sam", "root")}
        self.user_dir = os.path.join(self.home.path, "tenants", "acme", "lin")
        self.project = os.path.join(self.user_dir, "shop")
        shutil.copytree(PROJECT, self.project, ignore=shutil.ignore_patterns(".agent", "__pycache__"))
        self.upstream = upstream(script)
        self.service = agentd.Service(0, self.home, 0, MODEL, mode)
        self.port = self.service.server_address[1]
        self.proxy = keyproxy.KeyProxy(0, self.home.store, KEY,
                                       "http://127.0.0.1:%d" % self.upstream.server_address[1], MODEL,
                                       keyproxy.reporter(self.port, self.home.secret), upstream_timeout)
        self.proxy_port = self.service.runner.proxy_port = self.proxy.server_address[1]
        serve(self.service)
        serve(self.proxy)

    def close(self):
        for server in (self.service, self.proxy, self.upstream):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def http(self, path, body=None, token=None, port=None, headers=None, method="POST"):
        """Returns (status, text)."""
        headers = dict(headers or {})
        if token:
            headers["authorization"] = "Bearer " + token
        data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (port or self.port, path),
                                         data=data if method == "POST" else None, headers=headers, method=method)
        try:
            with DIRECT.open(request, timeout=90) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def session(self, user="lin", project="shop"):
        status, text = self.http("/sessions", {"project": project}, self.tokens[user])
        assert status == 201, (status, text)
        return json.loads(text)["id"]

    def ask(self, text, user="lin", session=None):
        """Send one message and return the events."""
        session = session or self.session(user)
        status, body = self.http("/sessions/%s/messages" % session, {"text": text}, self.tokens[user])
        assert status == 200, (status, body)
        return [json.loads(line) for line in body.splitlines()]

    def audit(self):
        events = []
        for name in sorted(os.listdir(self.home.audit_folder)):
            with open(os.path.join(self.home.audit_folder, name)) as f:
                events += [json.loads(line) for line in f]
        return events

    def sent_back(self):
        """Everything the provider was sent, as one string: what the model could see."""
        return json.dumps([s["body"] for s in self.upstream.seen])
