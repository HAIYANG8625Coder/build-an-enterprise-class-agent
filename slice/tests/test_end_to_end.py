"""The whole path with a scripted model: token, policy, sandboxed engine, command, proxy, audit, chain check."""
import json
import os
import unittest

import helpers
from helpers import call, say


class EndToEnd(unittest.TestCase):
    def tearDown(self):
        self.s.close()

    def tools(self, events):
        return [(e["name"], e["decision"]) for e in events if e["type"] == "tool"]

    def test_read_run_deny_and_the_log(self):
        self.s = helpers.Stack([
            call("read_file", path="cart.py"),
            call("run_bash", command="ls && python3 tests/test_pricing.py"),
            call("run_bash", command="curl https://example.com"),
            call("edit_file", path="cart.py", old_text="def", new_text="DEF"),
            say("Finished."),
        ])
        open(self.s.project + "/MARK-ONLY-LS-CAN-SEE", "w").close()
        with open(self.s.project + "/cart.py") as f:
            before = f.read()
        events = self.s.ask("PROMPT-WORDS: check the cart")
        self.assertEqual(events[-1], {"type": "done"}, events)
        self.assertEqual(self.tools(events), [("read_file", "allow"), ("run_bash", "allow"),
                                              ("run_bash", "deny"), ("edit_file", "deny")], events)
        self.assertEqual([e["text"] for e in events if e["type"] == "text"], ["Finished."])
        with open(self.s.project + "/cart.py") as f:
            self.assertEqual(f.read(), before)                   # "ask" with nobody to ask is no
        seen = self.s.sent_back()
        self.assertIn("MARK-ONLY-LS-CAN-SEE", seen)             # the command ran and its output reached the model
        self.assertIn("permission denied", seen)
        self.assertEqual({s["key"] for s in self.s.upstream.seen}, {helpers.KEY})

        status, text = self.s.http("/audit/verify", token=self.s.tokens["root"])
        self.assertTrue(json.loads(text)["ok"], text)
        log = self.s.audit()
        mine = [(e["type"], e["source"]) for e in log if e["session"]]
        self.assertEqual(mine.count(("model_call", "proxy")), 5)
        self.assertEqual(mine.count(("tool_decision", "service")), 4)
        self.assertEqual(mine.count(("command", "service")), 1)
        self.assertEqual(mine.count(("tool_result", "engine")), 2)
        self.assertTrue(all(e["user"] == "lin@acme" for e in log if e["session"]))
        text = json.dumps(log)
        for secret in ("PROMPT-WORDS", "MARK-ONLY-LS-CAN-SEE", "def ", helpers.KEY):
            self.assertNotIn(secret, text)

    def test_the_session_token_is_dead_after_the_message(self):
        self.s = helpers.Stack([say("hi")])
        self.s.ask("hi")
        rows = self.s.home.store._db().execute("SELECT COUNT(*) FROM session_tokens").fetchone()
        self.assertEqual(rows[0], 0)

    def test_files_in_the_repository_cannot_run_or_allow_anything(self):
        self.s = helpers.Stack([
            call("run_bash", command="touch allowed-by-the-repo"),
            call("read_file", path="cart.py"),
            say("ok"),
        ])
        os.makedirs(self.s.project + "/.agent")
        with open(self.s.project + "/.agent/hooks.json", "w") as f:
            json.dump({"hooks": [{"event": "PreToolUse", "command": "touch made-by-a-hook"}]}, f)
        for name, rule in (("settings.json", "Bash(*)"), ("permissions.json", "run_bash")):    # new and old format
            with open(self.s.project + "/.agent/" + name, "w") as f:
                json.dump({"allow": [rule]}, f)
        events = self.s.ask("go")
        self.assertEqual(self.tools(events), [("run_bash", "deny"), ("read_file", "allow")], events)
        self.assertFalse(os.path.exists(self.s.project + "/allowed-by-the-repo"))
        self.assertFalse(os.path.exists(self.s.project + "/made-by-a-hook"))

    def test_a_project_deny_counts_and_a_broken_rule_file_stops_everything(self):
        self.s = helpers.Stack([call("read_file", path="cart.py"), say("a"),
                                call("read_file", path="cart.py"), say("b")])
        os.makedirs(self.s.project + "/.agent")
        path = self.s.project + "/.agent/settings.json"
        with open(path, "w") as f:
            json.dump({"deny": ["Read(cart.py)"]}, f)
        self.assertEqual(self.tools(self.s.ask("go")), [("read_file", "deny")])
        with open(path, "w") as f:
            f.write("{")
        self.assertEqual(self.tools(self.s.ask("go")), [("read_file", "deny")])

    def test_with_no_published_policy_nothing_is_allowed(self):
        self.s = helpers.Stack([call("read_file", path="cart.py"), say("a")], policy_body=None)
        self.assertEqual(self.tools(self.s.ask("go")), [("read_file", "deny")])

    def test_an_edit_a_person_approves_runs(self):
        self.s = helpers.Stack([call("read_file", path="cart.py"),
                                call("edit_file", path="cart.py", old_text="from", new_text="# changed\nfrom"),
                                say("done")])
        with open(self.s.project + "/cart.py") as f:
            original = f.read()
        self.assertIn("from", original)
        self.s.service.runner.approver = lambda job, tool, args: tool == "edit_file"
        events = self.s.ask("edit")
        self.assertEqual(self.tools(events), [("read_file", "allow"), ("edit_file", "allow")], events)

    def test_a_provider_error_ends_the_turn_cleanly(self):
        self.s = helpers.Stack([{"status": 500, "raw": b'{"type":"error","error":{"type":"api","message":"x"}}'}])
        events = self.s.ask("go")
        self.assertEqual(events[-1]["type"], "error", events)
        self.assertEqual(len(self.s.upstream.seen), 1)           # no hidden retries inside the engine


if __name__ == "__main__":
    unittest.main()
