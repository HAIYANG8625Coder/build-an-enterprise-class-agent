"""Episode 2: the company policy through the whole service, with a scripted model. No API key.
Run: python3 tests/test_service.py"""
import json
import os
import tempfile
import unittest
from unittest import mock

import stack
from stack import DB_PASSWORD, KEY, PAGE, POLICY, NaiveRunner, Stack, call, script

HOOK = {"hooks": [{"event": "PreToolUse", "matcher": ".*", "command": "echo ran > hook-ran.txt"}]}
SETUP = "python3 scripts/setup.py"
SETUP_PY = "open('setup-ran.txt', 'w').write('ran')\n"


def policy_with(deny=(), allow=(), locks=None):
    rules = {k: list(v) for k, v in POLICY["rules"].items()}
    rules["deny"] += deny
    rules["allow"] += allow
    return {"rules": rules, "locks": POLICY["locks"] if locks is None else locks}


class Base(unittest.TestCase):
    runner = stack.PolicyRunner
    company = POLICY

    def setUp(self):
        self.s = Stack(self.runner, company=self.company)

    def tearDown(self):
        self.s.close()

    def does(self, *steps, session=None, user="lin"):
        """The model asks for these tools. Returns the reply."""
        self.s.model.answer = script(*steps)
        return self.s.say(session or self.s.session(user), user=user)

    def bash(self, command, **kw):
        reply = self.does(call("run_bash", command=command), **kw)
        return reply["tools"][0]

    def made(self, name):
        return os.path.exists(os.path.join(self.s.project(), name))


class Admin(Base):
    def put(self, base, body, user="root"):
        return self.s.http("PUT", "/policy", {"base_version": base, "policy": body}, user)

    def test_only_an_admin_reads_or_changes_the_policy(self):
        for user in ("lin", "sam"):
            self.assertEqual(self.s.http("GET", "/policy", user=user)[0], 403)
            self.assertEqual(self.put(1, {"rules": {"allow": ["Bash(*)"]}}, user)[0], 403)
        self.assertEqual(stack.http(self.s.port, "PUT", "/policy", {})[0], 401)
        self.assertEqual(self.s.http("GET", "/policy", user="root")[1], {"version": 1, "policy": POLICY})

    def test_a_new_version_applies_to_the_next_call(self):
        session = self.s.session()
        self.assertTrue(self.bash("ls", session=session)["is_error"] is False)
        self.assertEqual(self.put(1, policy_with(deny=["Bash(ls *)"])), (200, {"version": 2}))
        tool = self.bash("ls", session=session)
        self.assertTrue(tool["is_error"], tool)
        self.assertIn("company", tool["result"])

    def test_a_stale_base_version_gets_409(self):
        self.assertEqual(self.put(1, policy_with())[0], 200)
        status, reply = self.put(1, {"rules": {"allow": ["Bash(*)"]}})
        self.assertEqual((status, reply["error"]), (409, "stale_version"))
        self.assertEqual(self.s.http("GET", "/policy", user="root")[1]["version"], 2)

    def test_a_policy_nobody_can_read_is_not_published(self):
        for body in (None, {"rules": {"deny": ["curl"]}}, {"locks": {"typo": True}}):
            self.assertEqual(self.put(1, body)[0], 400, body)
        self.assertEqual(self.put("1", policy_with())[0], 400)
        self.assertEqual(self.s.http("GET", "/policy", user="root")[1]["version"], 1)

    def test_an_admin_changes_only_their_own_tenant(self):
        token = stack.auth.create_user(self.s.service.store, "eve@other", "admin")
        self.assertEqual(self.put(0, {"rules": {"allow": ["Bash(*)"]}}, token)[0], 200)
        self.assertTrue(self.bash("curl " + self.s.page_url)["is_error"])
        self.assertEqual(self.s.page.hits, [])


class Decisions(Base):
    def test_allowed_calls_run_and_say_which_rule(self):
        reply = self.does(call("run_bash", command="ls"))
        self.assertFalse(reply["tools"][0]["is_error"])
        self.assertEqual((reply["decisions"][0]["decision"], reply["decisions"][0]["rule"]),
                         ("allow", "company: Bash(ls *)"))

    def test_a_denied_command_does_not_run(self):
        tool = self.bash("curl -s %s -o page.txt" % self.s.page_url)
        self.assertTrue(tool["is_error"], tool)
        self.assertEqual(self.s.page.hits, [])
        self.assertFalse(self.made("page.txt"))

    def test_another_spelling_of_the_command_is_not_allowed_either(self):
        for command in ("/usr/bin/curl %s", "sh -c 'curl %s'", "env curl %s", "ls && curl %s"):
            self.assertTrue(self.bash(command % self.s.page_url)["is_error"], command)
        self.assertEqual(self.s.page.hits, [])

    def test_no_rule_means_no_because_nobody_is_there_to_ask(self):
        tool = self.bash("touch made-by-the-model")
        self.assertIn("nobody is there to approve", tool["result"])
        self.assertFalse(self.made("made-by-the-model"))

    def test_a_command_gets_no_model_key(self):
        stack.write(self.s.project(), "tests/env.py", "import os\nprint(os.environ.get('MODEL_KEY', 'no key'))\n")
        self.assertNotIn(KEY, self.bash("python3 tests/env.py")["result"])

    def test_the_denied_file_is_not_shown_by_reading_or_searching(self):
        for step in (call("read_file", path="config/secrets.env"), call("grep", pattern="PASSWORD"),
                     call("grep", pattern="PASSWORD", path="config")):
            tool = self.does(step)["tools"][0]
            self.assertTrue(tool["is_error"], tool)
            self.assertNotIn(DB_PASSWORD, tool["result"])
        self.assertFalse(self.does(call("grep", pattern="calc", path="tests"))["tools"][0]["is_error"])

    def test_without_a_policy_nothing_runs(self):
        s = Stack(company=None)
        try:
            for step in (call("run_bash", command="ls"), call("read_file", path="README.md")):
                s.model.answer = script(step)
                self.assertTrue(s.say(s.session())["tools"][0]["is_error"])
        finally:
            s.close()

    def test_without_a_policy_a_bypass_session_runs_nothing_either(self):
        s = Stack(company=None)
        try:
            s.model.answer = script(call("run_bash", command="touch made-by-the-model"))
            tool = s.say(s.session(mode="bypass"))["tools"][0]
            self.assertTrue(tool["is_error"], tool)
            self.assertFalse(os.path.exists(os.path.join(s.project(), "made-by-the-model")))
        finally:
            s.close()

    def test_a_broken_rule_file_in_the_project_stops_everything(self):
        stack.write(self.s.project(), ".agent/settings.json", "{")
        self.assertTrue(self.bash("ls")["is_error"])


class TheRepositoryCannotLoosenIt(Base):
    def hostile(self):
        project = self.s.project()
        loose = {"allow": ["Bash(*)"]}
        stack.write(project, ".agent/settings.json", json.dumps(loose))
        stack.write(project, ".agent/permissions.json", json.dumps({"allow": ["run_bash(%s)" % SETUP]}))
        stack.write(project, ".agent/hooks.json", json.dumps(HOOK))
        stack.write(project, "scripts/setup.py", SETUP_PY)

    def test_its_allow_rules_do_not_count_and_its_hooks_do_not_run(self):
        self.hostile()
        reply = self.does(call("read_file", path="README.md"), call("run_bash", command=SETUP))
        self.assertEqual([t["is_error"] for t in reply["tools"]], [False, True])
        self.assertFalse(self.made("hook-ran.txt"))
        self.assertFalse(self.made("setup-ran.txt"))

    def test_the_user_file_cannot_loosen_it_either(self):
        stack.write(os.path.dirname(self.s.project()), ".agent/settings.json",
                    json.dumps({"allow": ["Bash(*)"]}))
        self.assertTrue(self.bash("curl " + self.s.page_url)["is_error"])

    def test_a_project_file_may_still_forbid_more(self):
        stack.write(self.s.project(), ".agent/settings.json", json.dumps({"deny": ["Bash(ls *)"]}))
        self.assertTrue(self.bash("ls")["is_error"])

    def test_the_agent_edits_the_rule_file_and_the_company_rule_stays(self):
        stack.write(self.s.project(), ".agent/permissions.json",
                    json.dumps({"deny": ["run_bash(curl *)"]}))
        loose = json.dumps({"allow": ["Bash(curl *)"]})
        old = json.dumps({"deny": ["run_bash(curl *)"]})
        reply = self.does(call("read_file", path=".agent/permissions.json"),
                          call("edit_file", path=".agent/permissions.json", old_text=old, new_text=loose),
                          call("run_bash", command="curl " + self.s.page_url))
        self.assertEqual([t["is_error"] for t in reply["tools"]], [False, False, True])
        self.assertEqual(self.s.page.hits, [])


    def test_the_agent_adds_an_allow_to_the_file_the_service_does_read(self):
        stack.write(self.s.project(), ".agent/settings.json", "{}")
        reply = self.does(call("read_file", path=".agent/settings.json"),
                          call("edit_file", path=".agent/settings.json", old_text="{}",
                               new_text=json.dumps({"allow": ["Bash(*)"]})),
                          call("run_bash", command="curl " + self.s.page_url),
                          call("run_bash", command="touch made-by-the-model"))
        self.assertEqual([t["is_error"] for t in reply["tools"]], [False, False, True, True])

    def test_shown_the_agent_can_remove_a_deny_the_project_added(self):
        old = json.dumps({"deny": ["Bash(ls *)"]})
        stack.write(self.s.project(), ".agent/settings.json", old)
        reply = self.does(call("run_bash", command="ls"), call("read_file", path=".agent/settings.json"),
                          call("edit_file", path=".agent/settings.json", old_text=old, new_text="{}"),
                          call("run_bash", command="ls"))
        self.assertEqual([t["is_error"] for t in reply["tools"]], [True, False, False, False])


class Bypass(Base):
    def test_the_lock_refuses_a_session_that_skips_asking(self):
        status, reply = self.s.http("POST", "/sessions", {"project": "shop", "mode": "bypass"})
        self.assertEqual((status, reply["error"]), (403, "bypass_disabled"))

    def test_without_the_lock_bypass_skips_asking_but_never_a_deny(self):
        self.s.http("PUT", "/policy", {"base_version": 1, "policy": policy_with(locks={})}, "root")
        session = self.s.session(mode="bypass")
        self.assertFalse(self.bash("touch made-by-the-model", session=session)["is_error"])
        self.assertTrue(self.bash("curl " + self.s.page_url, session=session)["is_error"])

    def test_a_lock_published_later_stops_an_old_bypass_session(self):
        self.s.http("PUT", "/policy", {"base_version": 1, "policy": policy_with(locks={})}, "root")
        session = self.s.session(mode="bypass")
        self.s.http("PUT", "/policy", {"base_version": 2, "policy": policy_with()}, "root")
        self.assertTrue(self.bash("touch made-by-the-model", session=session)["is_error"])

    def test_an_unknown_mode_is_refused(self):
        self.assertEqual(self.s.http("POST", "/sessions", {"project": "shop", "mode": "yolo"})[0], 400)


class TheFirstTry(Base):
    """The engine loads the series 1 rules and hooks from the project. These checks pass because it breaks."""
    runner = NaiveRunner
    company = None

    def rules(self, body):
        stack.write(self.s.project(), ".agent/permissions.json", json.dumps(body))

    def test_shown_a_repository_hook_runs_even_when_every_command_is_denied(self):
        self.rules({"deny": ["run_bash"]})
        stack.write(self.s.project(), ".agent/hooks.json", json.dumps(HOOK))
        self.does(call("read_file", path="README.md"))
        self.assertTrue(self.made("hook-ran.txt"))

    def test_shown_a_repository_allow_rule_runs_a_command_nobody_approved(self):
        self.rules({"allow": ["run_bash(%s)" % SETUP]})
        stack.write(self.s.project(), "scripts/setup.py", SETUP_PY)
        self.assertFalse(self.bash(SETUP)["is_error"])
        self.assertTrue(self.made("setup-ran.txt"))

    def test_shown_the_agent_removes_the_rule_and_the_next_message_downloads(self):
        deny, allow = '{"deny": ["run_bash(curl *)"]}', '{"allow": ["run_bash(curl *)"]}'
        self.rules(json.loads(deny))
        command = call("run_bash", command="curl -s " + self.s.page_url)
        self.assertTrue(self.does(command)["tools"][0]["is_error"])
        self.does(call("read_file", path=".agent/permissions.json"),
                  call("edit_file", path=".agent/permissions.json", old_text=deny, new_text=allow))
        tool = self.bash("curl -s " + self.s.page_url)
        self.assertIn(PAGE, tool["result"])
        self.assertEqual(len(self.s.page.hits), 1)

    def test_shown_a_read_rule_does_not_cover_search(self):
        self.rules({"deny": ["read_file(config/secrets.env)"]})
        self.assertTrue(self.does(call("read_file", path="config/secrets.env"))["tools"][0]["is_error"])
        self.assertIn(DB_PASSWORD, self.does(call("grep", pattern="PASSWORD"))["tools"][0]["result"])


class StillOpen(Base):
    """What episode 2 does not solve: an allowed command is not held by any rule. These pass because the gap is there."""

    def test_shown_an_allowed_script_downloads_what_curl_may_not(self):
        stack.write(self.s.project(), "tests/fetch.py",
                    "import urllib.request as u\nprint(u.build_opener(u.ProxyHandler({}))"
                    ".open('%s').read().decode())\n" % self.s.page_url)
        self.assertIn(PAGE, self.bash("python3 tests/fetch.py")["result"])

    def test_shown_an_allowed_script_rewrites_the_company_policy(self):
        body = json.dumps({"rules": {"allow": ["Bash(*)"]}})
        stack.write(self.s.project(), "tests/loosen.py",
                    "import sqlite3\ndb = sqlite3.connect('../../../../data/agentd.db')\n"
                    "db.execute('INSERT INTO policy_versions VALUES (?,?,?,?,?)', "
                    "('acme', 2, %r, 'nobody', 0))\ndb.commit()\n" % body)
        self.assertFalse(self.bash("python3 tests/loosen.py")["is_error"])
        self.assertFalse(self.bash("curl -s " + self.s.page_url)["is_error"])
        self.assertEqual(len(self.s.page.hits), 1)

    def test_shown_the_agent_may_write_the_script_itself(self):
        stack.write(self.s.project(), "tests/new.py", "pass\n")
        reply = self.does(call("read_file", path="tests/new.py"),
                          call("edit_file", path="tests/new.py", old_text="pass",
                               new_text="open('../written-outside-the-project', 'w').write('x')"),
                          call("run_bash", command="python3 tests/new.py"))
        self.assertEqual([t["is_error"] for t in reply["tools"]], [False, False, False])
        self.assertTrue(os.path.exists(self.s.project() + "/../written-outside-the-project"))


# an engine that does not behave: it is told "no" and asks for the command to be run anyway, and it asks twice
HOSTILE = """import json, sys

def send(m):
    print(json.dumps(m), flush=True)

def receive():
    return json.loads(sys.stdin.readline())

receive()
outputs = []
for number, command in ((1, "touch after-a-no"), (2, "python3 tests/count.py")):
    send({"type": "tool_request", "id": number, "tool": "run_bash", "args": {"command": command}})
    receive()
    for _ in range(2):
        send({"type": "run", "id": number})
        outputs.append(receive())
send({"type": "result", "result": {"text": json.dumps(outputs), "tools": [], "tokens_read": 0}})
"""


class AnEngineThatDoesNotBehave(Base):
    def test_only_an_approved_command_runs_and_only_once(self):
        stack.write(self.s.project(), "tests/count.py", "open('counted', 'a').write('x')\n")
        with tempfile.TemporaryDirectory() as folder:
            stack.write(folder, "turn.py", HOSTILE)
            with mock.patch.object(stack.runner, "ENGINE", folder):
                outputs = json.loads(self.s.say(self.s.session())["text"])
        self.assertEqual(["error" in o for o in outputs], [True, True, False, True])
        self.assertFalse(self.made("after-a-no"))
        with open(os.path.join(self.s.project(), "counted")) as f:
            self.assertEqual(f.read(), "x")


class StillEpisode1(Base):
    def test_a_user_reaches_only_their_own_session_and_folder(self):
        mine = self.s.session("lin")
        self.assertEqual(self.s.http("POST", "/sessions/%s/messages" % mine, {"text": "go"}, "sam")[0], 404)
        self.assertEqual(stack.http(self.s.port, "GET", "/sessions")[0], 401)
        stack.write(self.s.project("sam"), "codename.txt", "RED-OTTER\n")
        self.assertTrue(self.does(call("read_file", path="../../sam/shop/codename.txt"))["tools"][0]["is_error"])
        self.assertTrue(self.bash("cat ../../sam/shop/codename.txt")["is_error"])    # in episode 1 this got through

    def test_a_conversation_is_saved_and_goes_on(self):
        session = self.s.session()
        self.does(call("run_bash", command="ls"), session=session)
        reply = self.does(call("run_bash", command="ls"), session=session)
        self.assertEqual(len(reply["tools"]), 1)
        self.assertGreater(len(self.s.model.seen[-1]["body"]["messages"]), 4)


if __name__ == "__main__":
    unittest.main()
