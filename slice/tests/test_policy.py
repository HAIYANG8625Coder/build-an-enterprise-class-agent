"""R2: may this call run."""
import json
import os
import tempfile
import unittest

import helpers  # noqa: F401  (puts the service folder on the path)
import policy


def rules(source="company", **lists):
    return policy.parse_rules(lists, source)


class Decide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name) + "/shop"
        os.makedirs(self.root + "/docs")
        for name in ("cart.py", "secret.txt", "docs/a.md"):
            open(os.path.join(self.root, name), "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def effect(self, tool, args, company, others=(), locks=None):
        return policy.decide(company, list(others), locks or {}, tool, args, self.root).effect

    def bash(self, command, company, **kw):
        return self.effect("run_bash", {"command": command}, company, **kw)

    def read(self, path, company, tool="read_file", **kw):
        return self.effect(tool, {"path": path, "pattern": "."}, company, **kw)

    def test_deny_then_ask_then_allow_and_no_rule_asks(self):
        r = rules(deny=["Bash(rm *)"], ask=["Bash(git push *)"], allow=["Bash(*)"])
        self.assertEqual(self.bash("rm -r build", r), "deny")
        self.assertEqual(self.bash("git push origin", r), "ask")
        self.assertEqual(self.bash("ls", r), "allow")
        self.assertEqual(self.bash("ls", rules()), "ask")

    def test_a_company_deny_beats_a_user_allow(self):
        mine = rules("user", allow=["Bash(curl *)"])
        self.assertEqual(self.bash("curl x", rules(deny=["Bash(curl *)"]), others=mine), "deny")

    def test_the_lock_drops_other_allow_rules_but_keeps_their_deny(self):
        mine = rules("user", allow=["Bash(make *)"], deny=["Bash(ls *)"])
        locked = {"company_rules_only": True}
        self.assertEqual(self.bash("make all", rules(), others=mine), "allow")
        self.assertEqual(self.bash("make all", rules(), others=mine, locks=locked), "ask")
        self.assertEqual(self.bash("ls -l", rules(allow=["Bash(ls *)"]), others=mine, locks=locked), "deny")

    def test_every_part_of_a_command_is_judged(self):
        r = rules(deny=["Bash(curl *)"], allow=["Bash(ls *)"])
        for command in ("ls && curl x", "ls; curl x", "ls | curl x", "ls\ncurl x", "ls & curl x", "CURL x"):
            self.assertEqual(self.bash(command, r), "deny", command)
        self.assertEqual(self.bash("ls && make", r), "ask")

    def test_a_command_that_writes_or_builds_commands_is_never_allowed_by_itself(self):
        r = rules(allow=["Bash(ls *)", "Bash(echo *)"])
        for command in ("echo x > cart.py", "ls $(make)", "echo `make`", "ls <(make)"):
            self.assertEqual(self.bash(command, r), "ask", command)

    def test_read_rules_cover_read_file_and_grep(self):
        r = rules(deny=["Read(secret.txt)"], allow=["Read(*)"])
        self.assertEqual(self.read("cart.py", r), "allow")
        self.assertEqual(self.read("secret.txt", r), "deny")
        self.assertEqual(self.read("secret.txt", r, tool="grep"), "deny")
        self.assertEqual(self.read(".", r, tool="grep"), "deny")          # the folder holds the denied file
        self.assertEqual(self.effect("grep", {"pattern": "x"}, r), "deny")  # no path means the whole project
        self.assertEqual(self.read("docs", r, tool="grep"), "allow")

    def test_a_denied_file_cannot_be_reached_by_another_spelling(self):
        r = rules(deny=["Read(secret.txt)"], allow=["Read(*)"])
        os.symlink(self.root + "/secret.txt", self.root + "/docs/alias")
        for path in ("./secret.txt", "docs/../secret.txt", self.root + "/secret.txt", "SECRET.TXT", "docs/alias"):
            self.assertEqual(self.read(path, r), "deny", path)
        self.assertEqual(self.read("docs", r, tool="grep"), "deny")       # the link inside the folder

    def test_outside_the_project_is_denied_whatever_the_rules_say(self):
        r = rules(allow=["Read(*)", "Edit(*)"])
        os.symlink("/etc/hosts", self.root + "/out")
        for path in ("../x", "/etc/hosts", "out", "a\0b"):
            self.assertEqual(self.read(path, r), "deny", repr(path))
        self.assertEqual(self.effect("edit_file", {"path": "../x"}, r), "deny")
        self.assertEqual(self.read(".", r, tool="grep"), "deny")          # it holds the link that leads outside

    def test_an_unknown_tool_is_denied(self):
        everything = rules(allow=["Read(*)", "Edit(*)", "Bash(*)"])
        for tool in ("send_message", "delegate", "write_file", "Bash"):
            self.assertEqual(self.effect(tool, {}, everything), "deny", tool)

    def test_a_rule_the_policy_does_not_understand_is_an_error(self):
        for text in ("Write(*)", "run_bash(ls)", "bash(ls)", "Bash", "Bash()", 7):
            with self.assertRaises(policy.PolicyError, msg=repr(text)):
                rules(deny=[text])


class RuleFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.tmp.name)
        os.makedirs(self.base + "/.agent")
        self.path = self.base + "/.agent/settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, body):
        with open(self.path, "w") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def load(self):
        return policy.file_rules(self.path, self.base, "project")

    def test_only_deny_and_ask_count(self):
        self.write({"allow": ["Bash(*)"], "ask": ["Bash(make *)"], "deny": ["Bash(rm *)"]})
        self.assertEqual(sorted(r.effect for r in self.load()), ["ask", "deny"])

    def test_no_file_is_no_rules(self):
        self.assertEqual(self.load(), [])

    def test_a_broken_file_is_an_error_that_does_not_quote_it(self):
        self.write("SECRET-CONTENT {")
        with self.assertRaises(policy.PolicyError) as caught:
            self.load()
        self.assertNotIn("SECRET", str(caught.exception))

    def test_a_link_to_somewhere_else_is_not_followed(self):
        os.symlink("/etc/hosts", self.path)
        with self.assertRaises(policy.PolicyError):
            self.load()


if __name__ == "__main__":
    unittest.main()
