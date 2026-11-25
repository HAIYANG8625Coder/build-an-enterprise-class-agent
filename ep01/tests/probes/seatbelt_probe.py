"""Does the real engine run inside sandbox/engine.sb, and what can it not reach? macOS only, no API key, no model.
Run from a plain terminal: python3 tests/probes/seatbelt_probe.py

Everything it creates is in one temporary folder. A small stub on the loopback plays the credential proxy."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROFILE = os.path.join(EP, "sandbox", "engine.sb")
COMMAND = os.path.join(EP, "sandbox", "command.sb")
TOKEN = "session-token-for-this-probe"
results = []


def record(name, passed, detail=""):
    results.append(passed)
    print(("ok   " if passed else "FAIL ") + name + ("" if passed or not detail else "\n       " + detail))


class StubProxy(BaseHTTPRequestHandler):
    """Answers POST /v1/messages: first with a read_file call, then with text. Remembers the key it was given."""
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        StubProxy.seen.append((self.path, self.headers.get("x-api-key")))
        first = len(body["messages"]) == 1
        content = ([{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "cart.py"}}] if first
                   else [{"type": "text", "text": "It adds up prices."}])
        reply = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": "stub", "content": content,
                            "stop_reason": "tool_use" if first else "end_turn", "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


# runs inside the sandbox: one turn of the real engine with the real anthropic SDK, through the stub proxy
SDK_TURN = """
import os, sys, anthropic, agent
agent.ROOT = os.getcwd()
client = anthropic.Anthropic(api_key=os.environ["SESSION_TOKEN"], base_url=os.environ["PROXY_URL"], max_retries=0, timeout=20)
messages = agent.run("What does cart.py do?", client, "stub", perms=agent.Permissions(), quiet=True)
assert "def " in str(messages), "the file was not read"
print(agent.final_text(messages))
"""

# runs inside the sandbox: each line tries one thing and says whether the operating system let it
TRY = """
import os, socket, sys
kind, target = sys.argv[1], sys.argv[2]
try:
    if kind == "read": open(target).read()
    elif kind == "list": os.listdir(target)
    elif kind == "write": open(target, "w").write("x")
    elif kind == "connect":
        host, port = target.rsplit(":", 1)
        socket.create_connection((host, int(port)), timeout=5).close()
    print("ALLOWED")
except PermissionError:
    print("BLOCKED")
except OSError as e:
    print("OTHER %r" % e)
"""


def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    return s


def main():
    import anthropic
    deps = os.path.dirname(os.path.dirname(os.path.realpath(anthropic.__file__)))
    python = os.path.realpath(sys.executable)
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubProxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy_port = server.server_address[1]
    other = listener()                      # stands for agentd: the engine must not reach it
    other_port = other.getsockname()[1]

    with tempfile.TemporaryDirectory() as made:
        base = os.path.realpath(made)
        tenant, outside = base + "/tenant", base + "/outside"
        os.makedirs(tenant + "/tmp")
        os.makedirs(outside)
        with open(outside + "/secret.txt", "w") as f:
            f.write("not for the engine")
        shutil.copytree(EP + "/engine/project", tenant + "/project")
        os.symlink(outside + "/secret.txt", tenant + "/link-to-secret")
        # a clean environment: no key of ours can leak in through a variable
        env = {"PATH": "/usr/bin:/bin", "HOME": tenant, "TMPDIR": tenant + "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": os.pathsep.join([EP + "/engine", EP + "/tests", deps]),
               "AGENT_HOME": tenant + "/agent-home", "AGENT_MEMORY": tenant + "/memory",
               "SESSION_TOKEN": TOKEN, "PROXY_URL": "http://127.0.0.1:%d" % proxy_port}
        jail = ["/usr/bin/sandbox-exec", "-D", "ENGINE=" + EP, "-D", "DEPS=" + deps, "-D", "TENANT=" + tenant,
                "-D", "PROXY=localhost:%d" % proxy_port, "-f", PROFILE]

        def run(args, boxed=True):
            return subprocess.run((jail if boxed else []) + args, cwd=tenant + "/project", env=env,
                                  capture_output=True, text=True, timeout=120)

        print("== the engine works inside the sandbox")
        for name, script in (("series 1 checks (test_repl.py)", EP + "/engine/test_repl.py"),
                             ("fake model and clock checks", EP + "/tests/test_fakes.py")):
            done = run([python, script])
            tail = (done.stdout.strip().splitlines() or [""])[-1]
            record("%s: %s" % (name, tail), done.returncode == 0, (done.stderr or done.stdout)[-900:])
        done = run([python, "-c", SDK_TURN])
        record("one turn with the real SDK through the proxy port", done.returncode == 0, done.stderr[-900:])
        record("the proxy received the session token and nothing else",
               bool(StubProxy.seen) and {key for _, key in StubProxy.seen} == {TOKEN}, repr(StubProxy.seen))
        done = run(["/bin/sh", "-c", "grep -c def cart.py"])
        record("a shell command runs", done.returncode == 0, done.stderr[-400:])

        print("== what the sandbox blocks (each one is first shown to work without it)")
        attempts = [("read a file outside the tenant folder", "read", outside + "/secret.txt"),
                    ("read it through a symlink in the tenant folder", "read", tenant + "/link-to-secret"),
                    ("list the folder outside", "list", outside),
                    ("write outside the tenant folder", "write", outside + "/new.txt"),
                    ("change the engine's own code", "write", EP + "/engine/probe-write.tmp"),
                    ("connect to another loopback port", "connect", "127.0.0.1:%d" % other_port)]
        for name, kind, target in attempts:
            if kind == "write" and target.startswith(EP):
                free = "ALLOWED" if os.access(os.path.dirname(target), os.W_OK) else "no write access"
            else:
                free = run([python, "-c", TRY, kind, target], boxed=False).stdout.strip()
            held = run([python, "-c", TRY, kind, target]).stdout.strip()
            record("blocked: " + name, free == "ALLOWED" and held == "BLOCKED",
                   "without sandbox: %s, with: %s" % (free, held))
        for name, kind, target in (("list your real home folder", "list", os.path.expanduser("~")),
                                   ("connect to the internet", "connect", "1.1.1.1:443")):
            held = run([python, "-c", TRY, kind, target]).stdout.strip()
            record("blocked: " + name, held == "BLOCKED", "with sandbox: %s" % held)
        held = run([python, "-c", TRY, "connect", "127.0.0.1:%d" % proxy_port]).stdout.strip()
        record("allowed: connect to the proxy port", held == "ALLOWED", held)

        print("== can a stricter command profile be started from inside the engine's sandbox?")
        inner = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)", "/bin/echo", "inner ran"]
        alone = run(inner, boxed=False)
        nested = run(inner)
        print("     on its own: exit %d %s" % (alone.returncode, (alone.stdout or alone.stderr).strip()[:200]))
        print("     nested:     exit %d %s" % (nested.returncode, (nested.stdout or nested.stderr).strip()[:200]))
        record("a sandbox cannot be started from inside the engine's sandbox", alone.returncode == 0
               and nested.returncode != 0, "nesting works here: the runner design can be looked at again")
        # so a command run by the engine itself only has the engine's profile, and with it the proxy port
        child = run(["/bin/sh", "-c", '"$0" -c "$1" connect "$2"', python, TRY, "127.0.0.1:%d" % proxy_port])
        record("shown: a command started by the engine can still reach the proxy", child.stdout.strip() == "ALLOWED",
               child.stdout + child.stderr[-300:])

        print("== command.sb, for a command that the runner starts from outside")
        shell = ["/usr/bin/sandbox-exec", "-D", "TENANT=" + tenant, "-f", COMMAND]
        done = run(shell + ["/bin/sh", "-c", "grep -c def cart.py && echo x > made-here"], boxed=False)
        record("a command runs and writes in the tenant folder", done.returncode == 0, done.stderr[-400:])
        for name, kind, target in (("reach the proxy port", "connect", "127.0.0.1:%d" % proxy_port),
                                   ("reach the internet", "connect", "1.1.1.1:443"),
                                   ("read a file outside the tenant folder", "read", outside + "/secret.txt"),
                                   ("write outside the tenant folder", "write", outside + "/new.txt")):
            held = run(shell + [python, "-c", TRY, kind, target], boxed=False).stdout.strip()
            record("blocked: " + name, held == "BLOCKED", "with command.sb: %s" % held)

    server.shutdown()
    other.close()
    print("%d of %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
