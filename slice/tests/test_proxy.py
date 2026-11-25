"""R4 and C4: the key stays in the proxy, and a call that cannot be checked or recorded is not sent."""
import json
import unittest

import helpers
import auth
import keyproxy

GOOD = {"model": helpers.MODEL, "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]}


class Proxy(unittest.TestCase):
    def setUp(self):
        self.s = helpers.Stack(upstream_timeout=1)
        self.s.home.store.add_session("s-1", "lin@acme", "acme", "shop")
        self.token = auth.new_session_token(self.s.home.store, "s-1")

    def tearDown(self):
        self.s.close()

    def post(self, body=GOOD, token=None, path="/v1/messages"):
        return self.s.http(path, body, port=self.s.proxy_port,
                           headers={"x-api-key": self.token if token is None else token})

    def calls(self):
        return self.s.home.store.calls("s-1")

    def test_a_good_call_goes_out_with_the_key_and_is_recorded(self):
        status, text = self.post()
        self.assertEqual(status, 200, text)
        self.assertEqual(self.s.upstream.seen[0]["key"], helpers.KEY)
        self.assertNotIn(self.token, json.dumps(self.s.upstream.seen))
        call = self.calls()[0]
        self.assertEqual((call["state"], call["input_tokens"], call["output_tokens"], call["estimated"]),
                         ("done", 100, 20, 0))
        kinds = [(e["type"], e["source"]) for e in self.s.audit()]
        self.assertEqual(kinds, [("model_call", "proxy"), ("model_usage", "proxy")])

    def test_no_token_wrong_token_user_token_expired_token(self):
        for token in ("", "wrong", self.s.tokens["lin"], helpers.KEY):
            self.assertEqual(self.post(token=token)[0], 401, token)
        self.s.clock.seconds += auth.SESSION_TOKEN_SECONDS + 1
        self.assertEqual(self.post()[0], 401)
        self.assertEqual(self.s.upstream.seen, [])

    def test_only_the_plain_request_the_engine_makes(self):
        url_picture = {"type": "image", "source": {"type": "url", "url": "https://example.com/?secret"}}
        bad = [dict(GOOD, model="another-model"), dict(GOOD, stream=True), dict(GOOD, max_tokens=True),
               dict(GOOD, max_tokens=10 ** 9), dict(GOOD, messages=[]),
               dict(GOOD, tools=[{"type": "web_search_20250305", "name": "web_search"}]),
               dict(GOOD, mcp_servers=[{"url": "https://example.com"}]),
               dict(GOOD, messages=[{"role": "user", "content": [url_picture]}]),
               dict(GOOD, messages=[{"role": "user", "content": [
                   {"type": "tool_result", "tool_use_id": "t", "content": [url_picture]}]}])]
        for body in bad:
            self.assertEqual(self.post(body)[0], 400, body)
        self.assertEqual(self.post(b"not json")[0], 400)
        self.assertEqual(self.post(path="/v1/files")[0], 404)
        self.assertEqual(self.s.upstream.seen, [])

    def test_a_broken_store_means_nothing_is_sent(self):
        def broken(*args):
            raise OSError("disk full")
        for name in ("session_by_token", "start_call"):
            kept = getattr(self.s.home.store, name)
            setattr(self.s.home.store, name, broken)
            self.assertEqual(self.post()[0], 503, name)
            setattr(self.s.home.store, name, kept)
        self.assertEqual(self.s.upstream.seen, [])

    def test_no_audit_means_nothing_is_sent(self):
        self.s.proxy.report = keyproxy.reporter(self.s.port, "wrong-secret")
        self.assertEqual(self.post()[0], 503)
        self.assertEqual(self.s.upstream.seen, [])

    def test_a_call_with_no_answer_keeps_an_estimate(self):
        self.s.upstream.script.append({"sleep": 2})
        self.assertEqual(self.post()[0], 504)
        call = self.calls()[0]
        self.assertEqual((call["state"], call["estimated"], call["output_tokens"]), ("no_answer", 1, 100))
        self.assertGreater(call["input_tokens"], 0)

    def test_an_answer_without_usage_keeps_an_estimate(self):
        self.s.upstream.script.append({"content": [], "no_usage": True})
        self.assertEqual(self.post()[0], 200)
        self.assertEqual((self.calls()[0]["state"], self.calls()[0]["estimated"]), ("no_usage", 1))

    def test_a_redirect_is_not_followed(self):
        elsewhere = helpers.upstream([])
        try:
            for status in (301, 302, 303, 307):
                self.s.upstream.script.append({"status": status, "headers": {
                    "location": "http://127.0.0.1:%d/v1/messages" % elsewhere.server_address[1]}})
                self.assertEqual(self.post()[0], 502, status)
                self.assertEqual(elsewhere.seen, [], status)
        finally:
            elsewhere.shutdown()
            elsewhere.server_close()

    def test_the_key_is_removed_from_what_comes_back_and_never_logged(self):
        self.s.upstream.script.append({"status": 401, "raw": b'{"error": "bad key %s"}' % helpers.KEY.encode()})
        status, text = self.post()
        self.assertEqual(status, 401)
        self.assertNotIn(helpers.KEY, text)
        self.assertNotIn(helpers.KEY, json.dumps(self.s.audit()))

    def test_the_key_is_only_sent_over_https(self):
        for url in ("http://example.com", "ftp://x", "http://localhost.example.com"):
            with self.assertRaises(ValueError, msg=url):
                keyproxy.check_upstream(url)


if __name__ == "__main__":
    unittest.main()
