"""A home folder with two users in one tenant, each with a project, and a model provider on the loopback."""
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
from naive import ThreadRunner  # noqa: E402
from runner import ProcessRunner  # noqa: E402
from store import Store  # noqa: E402

PROJECT = os.path.join(EP, "engine", "project")
CODENAMES = {"lin": "BLUE-HERON", "sam": "RED-OTTER"}
KEY = "test-key-not-a-real-one"
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def make_home(home):
    """Users lin and sam and the admin root, all in the tenant acme. Returns their tokens."""
    store = Store(os.path.join(home, "data", "agentd.db"))
    tokens = {name: auth.create_user(store, name + "@acme", "admin" if name == "root" else "user")
              for name in ("lin", "sam", "root")}
    for name, codename in CODENAMES.items():
        project = os.path.join(home, "tenants", "acme", name, "shop")
        shutil.copytree(PROJECT, project, ignore=shutil.ignore_patterns(".agent", "__pycache__"))
        with open(os.path.join(project, "codename.txt"), "w") as f:
            f.write(codename + "\n")
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


def read_then_say(body):
    """The default model: it reads codename.txt, then repeats what the tool gave back."""
    last = body["messages"][-1]["content"]
    if isinstance(last, list) and last[0].get("type") == "tool_result":
        return [{"type": "text", "text": "The tool said: " + last[0]["content"]}]
    return call("read_file", path="codename.txt")


def model(answer=read_then_say):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
    server.daemon_threads, server.answer, server.seen = True, answer, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Stack:
    """The service in this process, with either runner."""

    def __init__(self, runner=ProcessRunner, answer=read_then_say):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        self.tokens = make_home(self.home)
        self.model = model(answer)
        self.url = "http://127.0.0.1:%d" % self.model.server_address[1]
        self.runner = runner("stub", self.url, KEY)
        self.start()

    def start(self):
        self.service = agentd.Service(0, self.home, self.runner)
        self.port = self.service.server_address[1]
        threading.Thread(target=self.service.serve_forever, daemon=True).start()

    def restart(self):
        self.service.shutdown()
        self.service.server_close()
        self.start()

    def close(self):
        for server in (self.service, self.model):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def http(self, method, path, body=None, user="lin"):
        return http(self.port, method, path, body, self.tokens.get(user, user))

    def session(self, user="lin"):
        status, reply = self.http("POST", "/sessions", {"project": "shop"}, user)
        assert status == 201, (status, reply)
        return reply["id"]

    def say(self, session, text, user="lin"):
        status, reply = self.http("POST", "/sessions/%s/messages" % session, {"text": text}, user)
        assert status == 200, (status, reply)
        return reply
