"""Episode 1: who is asking, whose folder, whose conversation. No API key. Run: python3 tests/test_service.py"""
import json
import os
import threading
import unittest
from unittest import mock

import anthropic

import stack
from stack import CODENAMES, KEY, ProcessRunner, Stack, ThreadRunner, call, read_then_say

READ = "Read codename.txt and reply with the code name."


def last_tool_result(body):
    last = body["messages"][-1]["content"]
    return last[0] if isinstance(last, list) and last[0].get("type") == "tool_result" else None


class Identity(unittest.TestCase):
    def setUp(self):
        self.s = Stack()

    def tearDown(self):
        self.s.close()

    def test_no_token_and_a_wrong_token_get_401(self):
        for user in (None, "wrong-token"):
            self.assertEqual(stack.http(self.s.port, "GET", "/sessions", token=user)[0], 401)
            self.assertEqual(stack.http(self.s.port, "POST", "/sessions", {"project": "shop"}, user)[0], 401)
        self.assertEqual(self.s.model.seen, [])

    def test_a_user_sees_and_uses_only_their_own_sessions(self):
        mine = self.s.session("lin")
        self.assertEqual(self.s.http("GET", "/sessions/" + mine, user="sam")[0], 404)
        self.assertEqual(self.s.http("POST", "/sessions/%s/messages" % mine, {"text": READ}, "sam")[0], 404)
        self.assertEqual(self.s.http("GET", "/sessions", user="sam")[1], {"sessions": []})
        self.assertEqual([x["id"] for x in self.s.http("GET", "/sessions")[1]["sessions"]], [mine])
        self.assertEqual(self.s.model.seen, [])

    def test_a_project_is_a_name_inside_your_own_folder(self):
        for name in ("../sam/shop", "..", "/etc", "SHOP", None, 7):
            self.assertEqual(self.s.http("POST", "/sessions", {"project": name})[0], 404, name)
        os.symlink(self.s.home + "/tenants/acme/sam/shop", self.s.home + "/tenants/acme/lin/theirs")
        self.assertEqual(self.s.http("POST", "/sessions", {"project": "theirs"})[0], 404)

    def test_only_an_admin_makes_and_revokes_tokens(self):
        new = {"user": "kim@acme"}
        self.assertEqual(self.s.http("POST", "/tokens", new, "lin")[0], 403)
        self.assertEqual(self.s.http("DELETE", "/tokens/sam@acme", user="lin")[0], 403)
        status, reply = self.s.http("POST", "/tokens", new, "root")
        self.assertEqual(status, 201)
        self.assertEqual(self.s.http("GET", "/sessions", user=reply["token"])[0], 200)
        self.assertEqual(self.s.http("DELETE", "/tokens/kim@acme", user="root")[0], 200)
        self.assertEqual(self.s.http("GET", "/sessions", user=reply["token"])[0], 401)

    def test_an_admin_stays_inside_their_tenant(self):
        for user in ("eve@other", "lin", "../x@acme"):
            self.assertIn(self.s.http("POST", "/tokens", {"user": user}, "root")[0], (400, 404), user)
        self.assertEqual(self.s.http("POST", "/tokens", {"user": "kim@acme", "role": "king"}, "root")[0], 400)


class Sessions(unittest.TestCase):
    def setUp(self):
        self.s = Stack()

    def tearDown(self):
        self.s.close()

    def sent(self):
        return json.dumps(self.s.model.seen[-1]["body"]["messages"])

    def saved(self, session):
        return self.s.home + "/tenants/acme/lin/.sessions/%s.json" % session

    def test_each_user_works_in_their_own_folder(self):
        for user, codename in CODENAMES.items():
            self.assertIn(codename, self.s.say(self.s.session(user), READ, user)["text"])

    def test_a_conversation_goes_on_after_a_restart(self):
        session = self.s.session()
        self.s.say(session, "FIRST-MESSAGE " + READ)
        self.s.restart()
        reply = self.s.say(session, "and again")
        self.assertIn("FIRST-MESSAGE", self.sent())
        self.assertEqual(len(reply["tools"]), 1)               # only this turn's calls
        self.assertEqual(self.s.http("GET", "/sessions/" + session)[1]["turns"], 2)

    def test_without_the_saved_file_the_conversation_is_gone(self):
        session = self.s.session()
        self.s.say(session, "FIRST-MESSAGE " + READ)
        os.remove(self.saved(session))                         # a new container has an empty disk
        self.s.say(session, "and again")
        self.assertNotIn("FIRST-MESSAGE", self.sent())

    def test_one_message_at_a_time_in_a_session(self):
        session = self.s.session()
        self.s.service.busy.add(session)
        status, reply = self.s.http("POST", "/sessions/%s/messages" % session, {"text": READ})
        self.assertEqual((status, reply["error"]), (409, "session_busy"))


class TwoUsersAtOnce(unittest.TestCase):
    """lin asks first. The model answers lin only after sam's request has arrived, so the two always overlap."""

    def overlap(self, runner):
        lin_in, sam_in = threading.Event(), threading.Event()

        def answer(body):
            if last_tool_result(body) is None:
                if "[lin]" in json.dumps(body["messages"][0]):
                    lin_in.set()
                    sam_in.wait(20)
                else:
                    sam_in.set()
            return read_then_say(body)

        s = Stack(runner, answer)
        try:
            replies = {}

            def ask(user):
                replies[user] = s.say(s.session(user), "[%s] %s" % (user, READ), user)["text"]

            threads = [threading.Thread(target=ask, args=(u,)) for u in ("lin", "sam")]
            threads[0].start()
            self.assertTrue(lin_in.wait(20))
            threads[1].start()
            for t in threads:
                t.join(60)
            return replies
        finally:
            s.close()

    def test_shown_the_naive_service_gives_lin_sams_file(self):
        replies = self.overlap(ThreadRunner)
        self.assertIn(CODENAMES["sam"], replies["lin"], replies)
        self.assertNotIn(CODENAMES["lin"], replies["lin"])

    def test_one_process_per_session_keeps_them_apart(self):
        replies = self.overlap(ProcessRunner)
        for user, codename in CODENAMES.items():
            self.assertIn(codename, replies[user], replies)


class NotYetProtected(unittest.TestCase):
    """What episode 1 does not solve. These checks pass because the gap is there."""

    def run_tool(self, name, **args):
        def answer(body):
            result = last_tool_result(body)
            return [{"type": "text", "text": result["content"]}] if result else call(name, **args)

        s = Stack(ProcessRunner, answer)
        try:
            return s.say(s.session("lin"), "go")["tools"][0]
        finally:
            s.close()

    def test_the_file_tool_stays_in_the_project(self):
        tool = self.run_tool("read_file", path="../../sam/shop/codename.txt")
        self.assertTrue(tool["is_error"], tool)
        self.assertNotIn(CODENAMES["sam"], tool["result"])

    def test_shown_a_read_only_command_reads_another_users_file(self):
        tool = self.run_tool("run_bash", command="cat ../../sam/shop/codename.txt")
        self.assertIn(CODENAMES["sam"], tool["result"], tool)

    def test_a_command_that_changes_things_is_refused(self):
        tool = self.run_tool("run_bash", command="touch made-by-the-model")
        self.assertTrue(tool["is_error"], tool)
        self.assertIn("read-only", tool["result"])


class LeftoverVariables(unittest.TestCase):
    """Variables that someone set in the service's shell, long ago. Measured with the real anthropic SDK."""

    def setUp(self):
        self.elsewhere = stack.model(lambda body: [{"type": "text", "text": "from elsewhere"}])
        self.leftover = {"ANTHROPIC_AUTH_TOKEN": "leftover-company-token",
                         "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % self.elsewhere.server_address[1],
                         "ANTHROPIC_CUSTOM_HEADERS": "x-leftover: yes"}

    def tearDown(self):
        self.elsewhere.shutdown()
        self.elsewhere.server_close()

    def ask(self, env, **client_args):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        with mock.patch.dict(os.environ, dict(clean, **env), clear=True):
            client = anthropic.Anthropic(max_retries=0, **client_args)
            client.messages.create(model="stub", max_tokens=10, messages=[{"role": "user", "content": "hi"}])

    def test_shown_a_key_given_in_code_still_goes_where_the_variable_says(self):
        self.ask(self.leftover, api_key=KEY)
        headers = self.elsewhere.seen[0]["headers"]
        self.assertEqual(headers["x-api-key"], KEY)                       # our key went to the old address
        self.assertEqual(headers["x-leftover"], "yes")
        self.assertNotIn("authorization", headers)                        # a key given in code hides the token

    def test_shown_a_key_taken_from_a_variable_travels_with_the_old_token(self):
        self.ask(dict(self.leftover, ANTHROPIC_API_KEY=KEY))
        headers = self.elsewhere.seen[0]["headers"]
        self.assertEqual((headers["x-api-key"], headers["authorization"]), (KEY, "Bearer leftover-company-token"))

    def test_the_engine_process_gets_none_of_it(self):
        with mock.patch.dict(os.environ, self.leftover):
            s = Stack(ProcessRunner)
            try:
                s.say(s.session(), READ)
                self.assertEqual(self.elsewhere.seen, [])
                for request in s.model.seen:
                    for name in ("authorization", "x-leftover"):
                        self.assertNotIn(name, request["headers"])
                    self.assertEqual(request["headers"]["x-api-key"], KEY)
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
