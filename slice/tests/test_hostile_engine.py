"""R3 and C1: an engine that has been taken over. It is replaced by a script that does whatever it likes,
inside the same sandbox, and the runner and the operating system have to hold."""
import json
import os
import textwrap
import unittest

import helpers
import auth
from runner import Job

HEAD = """
import json, os, socket, sys
def send(m): print(json.dumps(m), flush=True)
def receive(): return json.loads(sys.stdin.readline())
start = receive()
def ask(n, tool, **args):
    send({"type": "tool_request", "id": n, "tool": tool, "args": args})
    return receive()
def run(n, **extra):
    send(dict(extra, type="run", id=n))
    return receive()
def report(value): send({"type": "text", "text": json.dumps(value)}); send({"type": "done"})
"""


class Hostile(unittest.TestCase):
    def setUp(self):
        self.s = helpers.Stack()
        self.engine = os.path.realpath(self.s.tmp.name) + "/hostile"
        os.makedirs(self.engine)
        self.s.service.runner.engine = self.engine

    def tearDown(self):
        self.s.close()

    def play(self, body):
        with open(self.engine + "/bridge.py", "w") as f:
            f.write(HEAD + textwrap.dedent(body))
        events = self.s.ask("go")
        text = [e["text"] for e in events if e["type"] == "text"]
        return (json.loads(text[0]) if text else None), events

    def made(self, name):
        return os.path.exists(os.path.join(self.s.project, name))

    def kinds(self):
        return [e["type"] for e in self.s.audit()]

    def test_it_cannot_run_what_was_not_approved(self):
        out, _ = self.play("""
            a = run(1, command="touch never-asked")
            ask(2, "run_bash", command="curl x")
            b = run(2, command="touch after-a-deny")
            report([a, b])
        """)
        self.assertTrue(all("not approved" in m["error"] for m in out), out)
        self.assertFalse(self.made("never-asked") or self.made("after-a-deny"))
        self.assertEqual(self.kinds().count("mismatch"), 2)

    def test_it_cannot_swap_the_command_or_use_an_approval_twice(self):
        out, _ = self.play("""
            ask(1, "run_bash", command="ls cart.py")
            a = run(1, command="touch swapped")
            b = run(1)
            ask(2, "run_bash", command="ls cart.py")
            ask(3, "read_file", path="cart.py")
            c = run(2)
            report([a, b, c])
        """)
        self.assertIn("cart.py", out[0]["text"])
        self.assertIn("not approved", out[1]["error"])
        self.assertIn("not approved", out[2]["error"])           # a newer request ends the old approval
        self.assertFalse(self.made("swapped"))

    def test_nonsense_ends_the_engine_and_is_logged(self):
        for body in ('send({"type": "surprise"})', 'print("hello", flush=True)',
                     'send({"type": "tool_request", "id": "1", "tool": "read_file", "args": {}})',
                     'ask(1, "read_file", path="cart.py"); ask(1, "read_file", path="cart.py")',
                     'send({"type": "tool_request", "id": 1, "tool": "run_bash", "args": "rm"})', 'sys.exit(0)'):
            _, events = self.play(body)
            self.assertEqual(events[-1]["type"], "error", body)
        self.assertEqual(self.kinds().count("engine_error"), 6)
        self.assertTrue(json.loads(self.s.http("/audit/verify", token=self.s.tokens["root"])[1])["ok"])

    def test_it_cannot_flood_the_log(self):
        _, events = self.play("""
            for n in range(100000):
                send({"type": "tool_result", "id": n, "tool": "read_file", "is_error": False})
        """)
        self.assertIn("too many messages", events[-1]["message"])
        self.assertLessEqual(self.kinds().count("tool_result"), 1000)

    def test_an_approved_command_gets_no_token_and_no_key(self):
        job = Job("acme", "lin@acme", "s-1", self.s.user_dir, self.s.project)
        os.makedirs(self.s.user_dir + "/tmp", exist_ok=True)
        code, text = self.s.service.runner.command(job, "env")
        self.assertEqual(code, 0, text)
        for word in ("SESSION_TOKEN", "PROXY_URL", helpers.KEY, "API_KEY"):
            self.assertNotIn(word, text)

    @unittest.skipUnless(helpers.MAC, "needs sandbox-exec (macOS)")
    def test_the_engine_cannot_reach_what_is_outside_its_folder(self):
        home = self.s.home.path
        other = home + "/tenants/acme/sam"
        os.makedirs(other)
        with open(other + "/notes.txt", "w") as f:
            f.write("sam's")
        self.s.service.audit.append(type="request", source="service")      # so that there is a log and an anchor
        targets = {"db": home + "/data/agentd.db", "secret": home + "/data/internal", "anchor": home + "/anchor",
                   "other_user": other + "/notes.txt"}
        out, events = self.play("""
            targets = %r
            result = {}
            for name, path in targets.items():
                for mode in ("r", "a"):
                    try:
                        open(path, mode).close(); result[name + " " + mode] = "OPENED"
                    except PermissionError:
                        result[name + " " + mode] = "blocked"
            for name, port in (("service", %d), ("provider", %d), ("proxy", %d)):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=5).close(); result[name] = "OPENED"
                except PermissionError:
                    result[name] = "blocked"
            try:
                os.listdir(%r); result["list audit"] = "OPENED"
            except PermissionError:
                result["list audit"] = "blocked"
            report(result)
        """ % (targets, self.s.port, self.s.upstream.server_address[1], self.s.proxy_port, home + "/audit"))
        self.assertIsNotNone(out, events)
        self.assertEqual(out.pop("proxy"), "OPENED")             # the one thing it may reach
        self.assertEqual(set(out.values()), {"blocked"}, out)
        self.assertEqual(len(out), 11)

    @unittest.skipUnless(helpers.MAC, "needs sandbox-exec (macOS)")
    def test_a_command_cannot_reach_the_proxy_or_leave_the_folder(self):
        job = Job("acme", "lin@acme", "s-1", self.s.user_dir, self.s.project)
        os.makedirs(self.s.user_dir + "/tmp", exist_ok=True)
        probe = ('python3 -c "import socket; socket.create_connection((\'127.0.0.1\', %d), timeout=5)"'
                 % self.s.proxy_port)
        for command in (probe, "cat %s/data/internal" % self.s.home.path,
                        "touch %s/escaped" % self.s.home.path, "echo x >> %s/anchor" % self.s.home.path):
            code, text = self.s.service.runner.command(job, command)
            self.assertNotEqual(code, 0, command)
            self.assertIn("not permitted", text, command)
        code, text = self.s.service.runner.command(job, "touch inside && echo ran")
        self.assertEqual((code, text.strip()), (0, "ran"))


if __name__ == "__main__":
    unittest.main()
