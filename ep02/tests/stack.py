"""A home folder with two users and an admin in one tenant, each user with a project, the example company policy,
a model provider on the loopback and a web page that counts who fetched it."""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(EP, "service"))
sys.path.insert(0, os.path.join(EP, "engine"))
import agentd  # noqa: E402
import auth  # noqa: E402
import runner  # noqa: E402
from runner import NaiveRunner, PolicyRunner  # noqa: E402
from store import Store  # noqa: E402

PROJECT = os.path.join(EP, "engine", "project")
with open(os.path.join(EP, "config", "policy.example.json"), encoding="utf-8") as _f:
    POLICY = json.load(_f)
DB_PASSWORD = "PLUM-4471-example"          # made up, only for the tests
PAGE = "quarterly-report: 42 orders"
KEY = "test-key-not-a-real-one"
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def write(base, name, text):
    path = os.path.join(base, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_home(home, company=POLICY):
    """Users lin and sam and the admin root, all in the tenant acme, with the company policy as version 1."""
    store = Store(os.path.join(home, "data", "agentd.db"))
    tokens = {name: auth.create_user(store, name + "@acme", "admin" if name == "root" else "user")
              for name in ("lin", "sam", "root")}
    for name in ("lin", "sam"):
        project = os.path.join(home, "tenants", "acme", name, "shop")
        shutil.copytree(PROJECT, project, ignore=shutil.ignore_patterns(".agent", "__pycache__"))
        write(project, "config/secrets.env", "DB_HOST=db.internal\nDB_PASSWORD=%s\n" % DB_PASSWORD)
    if company is not None:
        store.publish_policy("acme", 0, json.dumps(company), "setup")
    return tokens


def http(port, method, path, body=None, token=None):
    """Returns (status, decoded JSON)."""
    headers = {"authorization": "Bearer " + token} if token else {}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data, headers, method=method)
    try:
        with DIRECT.open(request, timeout=700) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class _Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.seen.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        content = self.server.answer(body)
        tool = any(b["type"] == "tool_use" for b in content)
        data = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": "stub", "content": content,
                           "stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None,
                           "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def call(name, **args):
    return [{"type": "tool_use", "id": "t%d" % call.n(), "name": name, "input": args}]


call.n = iter(range(10 ** 9)).__next__


def last_result(body):
    last = body["messages"][-1]["content"]
    if isinstance(last, list) and last[0].get("type") == "tool_result":
        return last[0]
    return None


def script(*steps):
    """A model that asks for these tools one after the other, then ends with what the last one gave back."""
    queue = list(steps)

    def answer(body):
        if queue:
            return queue.pop(0)
        result = last_result(body)
        return [{"type": "text", "text": "done: " + (result["content"] if result else "")}]
    return answer


class _Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.hits.append({"path": self.path, "agent": self.headers.get("user-agent", "")})
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.end_headers()
        self.wfile.write(PAGE.encode())


def page():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    server.daemon_threads, server.hits = True, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def model(answer):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
    server.daemon_threads, server.answer, server.seen = True, answer, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Stack:
    """The service in this process, with either runner."""

    def __init__(self, runner_class=PolicyRunner, answer=None, company=POLICY):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        self.tokens = make_home(self.home, company)
        self.model = model(answer or script())
        self.page = page()
        self.url = "http://127.0.0.1:%d" % self.model.server_address[1]
        self.page_url = "http://127.0.0.1:%d/report" % self.page.server_address[1]
        self.service = agentd.Service(0, self.home, runner_class("stub", self.url, KEY))
        self.port = self.service.server_address[1]
        threading.Thread(target=self.service.serve_forever, daemon=True).start()

    def project(self, user="lin"):
        return os.path.join(self.home, "tenants", "acme", user, "shop")

    def close(self):
        for server in (self.service, self.model, self.page):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def http(self, method, path, body=None, user="lin"):
        return http(self.port, method, path, body, self.tokens.get(user, user))

    def session(self, user="lin", mode=None):
        body = {"project": "shop"}
        if mode:
            body["mode"] = mode
        status, reply = self.http("POST", "/sessions", body, user)
        assert status == 201, (status, reply)
        return reply["id"]

    def say(self, session, text="go", user="lin"):
        status, reply = self.http("POST", "/sessions/%s/messages" % session, {"text": text}, user)
        assert status == 200, (status, reply)
        return reply
