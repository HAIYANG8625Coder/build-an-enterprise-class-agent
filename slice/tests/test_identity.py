"""R1: who is asking."""
import glob
import pathlib
import unittest

import helpers
import auth


class Identity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = helpers.Stack()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_no_token_and_wrong_token_get_401(self):
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": "Bearer "},
                        {"authorization": self.s.tokens["lin"]}):
            status, _ = self.s.http("/sessions", {"project": "shop"}, headers=headers)
            self.assertEqual(status, 401, headers)

    def test_revoked_token_gets_401_and_its_session_token_dies(self):
        token = auth.create_user(self.s.home.store, "kim@acme")
        self.s.home.store.add_session("s-kim", "kim@acme", "acme", "shop")
        live = auth.new_session_token(self.s.home.store, "s-kim")
        self.assertIsNotNone(auth.session_from_token(self.s.home.store, live))
        self.s.home.store.revoke_user("kim@acme")
        self.assertEqual(self.s.http("/sessions", {"project": "shop"}, token)[0], 401)
        self.assertIsNone(auth.session_from_token(self.s.home.store, live))

    def test_a_user_cannot_use_another_users_session(self):
        mine = self.s.session("lin")
        status, _ = self.s.http("/sessions/%s/messages" % mine, {"text": "hello"}, self.s.tokens["sam"])
        self.assertEqual(status, 404)
        self.assertEqual(self.s.upstream.seen, [])

    def test_another_users_project_and_paths_as_names_are_refused(self):
        for name, user in (("shop", "sam"), ("../lin/shop", "sam"), ("..", "lin"), ("/etc", "lin"), (None, "lin")):
            status, _ = self.s.http("/sessions", {"project": name}, self.s.tokens[user])
            self.assertEqual(status, 404, name)

    def test_only_an_admin_checks_the_audit_log(self):
        self.assertEqual(self.s.http("/audit/verify", token=self.s.tokens["lin"])[0], 403)
        status, text = self.s.http("/audit/verify", token=self.s.tokens["root"])
        self.assertEqual(status, 200)
        self.assertIn('"ok": true', text)

    def test_the_internal_endpoint_needs_its_secret(self):
        body = {"session": "x", "type": "model_call", "detail": {}}
        self.assertEqual(self.s.http("/internal/audit", body, self.s.tokens["root"])[0], 401)

    def test_tokens_are_not_stored_in_clear(self):
        stored = b"".join(pathlib.Path(f).read_bytes() for f in glob.glob(self.s.home.path + "/data/agentd.db*"))
        for token in self.s.tokens.values():
            self.assertNotIn(token.encode(), stored)

    def test_a_session_token_expires(self):
        self.s.home.store.add_session("s-old", "lin@acme", "acme", "shop")
        token = auth.new_session_token(self.s.home.store, "s-old", seconds=60)
        self.s.clock.seconds += 59
        self.assertIsNotNone(auth.session_from_token(self.s.home.store, token))
        self.s.clock.seconds += 2
        self.assertIsNone(auth.session_from_token(self.s.home.store, token))

    def test_bad_user_names_are_refused(self):
        for name in ("../x@acme", "lin@..", "lin", "Lin@acme", "a/b@acme", "@acme"):
            with self.assertRaises(ValueError, msg=name):
                auth.create_user(self.s.home.store, name)


if __name__ == "__main__":
    unittest.main()
