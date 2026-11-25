"""The only process that holds the model API key. The engine sends it a session token; it checks the token, records
the call, adds the key and passes the request on. When it cannot check or cannot record, it sends nothing (TRD v1.2).
This version counts calls and tokens. Prices and budgets come in episode 8.

Run: AGENTD_HOME=... MODEL_KEY_FILE=... python3 proxy/keyproxy.py"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SLICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SLICE, "service"))
import auth  # noqa: E402
from store import Store  # noqa: E402

MAX_BODY = 2 * 1024 * 1024
MAX_TOKENS = 8192
FIELDS = {"model", "max_tokens", "system", "tools", "messages"}          # what the engine sends
BLOCKS = {"text", "tool_use", "tool_result", "thinking", "redacted_thinking"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None                         # a redirect would carry the key somewhere else


def check_upstream(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname == "127.0.0.1"):
        raise ValueError("the key is only sent over https (or to 127.0.0.1 in tests)")
    return url.rstrip("/")


def blocks_ok(content):
    if isinstance(content, str):
        return True
    return isinstance(content, list) and all(
        isinstance(b, dict) and b.get("type") in BLOCKS
        and (b["type"] != "tool_result" or blocks_ok(b.get("content", ""))) for b in content)


def request_ok(body, model):
    """Only the plain shape the engine uses. The provider can be asked to reach the internet for a caller (its own
    server tools, or a picture given as a URL); a hijacked engine must not get that through us."""
    if not isinstance(body, dict) or set(body) - FIELDS or body.get("model") != model:
        return False
    most, tools, messages = body.get("max_tokens"), body.get("tools", []), body.get("messages")
    if type(most) is not int or not 0 < most <= MAX_TOKENS:
        return False
    if not isinstance(tools, list) or not all(
            isinstance(t, dict) and "input_schema" in t and t.get("type", "custom") == "custom" for t in tools):
        return False
    if not blocks_ok(body.get("system", "")):
        return False
    return isinstance(messages, list) and bool(messages) and all(
        isinstance(m, dict) and blocks_ok(m.get("content")) for m in messages)


def read_key(path, name):
    """The file holds the bare key, or lines like NAME=value (the series 1 keys.env)."""
    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    for line in lines:
        left, equals, value = line.partition("=")
        if equals and left.replace("export ", "").strip() == name:
            return value.strip().strip("\"'")
    if len(lines) == 1 and "=" not in lines[0]:
        return lines[0]
    raise ValueError("no key named %s in the key file" % name)


class KeyProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, store, key, upstream, model, report, timeout=120):
        """report(session, kind, detail) writes an audit event through the service; it raises if it cannot."""
        super().__init__(("127.0.0.1", port), Handler)
        self.store, self.key, self.model, self.report, self.upstream_timeout = store, key, model, report, timeout
        self.upstream = check_upstream(upstream)
        self.opener = urllib.request.build_opener(NoRedirect)


class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def refuse(self, status, code, message):
        self.reply(status, json.dumps({"type": "error", "error": {"type": code, "message": message}}).encode())

    def do_POST(self):
        server = self.server
        if self.path.split("?")[0] != "/v1/messages":
            return self.refuse(404, "not_found", "only /v1/messages")
        try:
            length = int(self.headers.get("content-length", ""))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            return self.refuse(413, "invalid_request", "a body of 1 byte to 2 MB is needed")
        raw = self.rfile.read(length)
        try:
            session = auth.session_from_token(server.store, self.headers.get("x-api-key"))
        except Exception as e:
            return self.fail_closed("checking the token", e)
        if session is None:
            return self.refuse(401, "authentication_error", "unknown or expired session token")
        try:
            body = json.loads(raw)
            good = request_ok(body, server.model)
        except (ValueError, RecursionError):
            good = False
        if not good:
            return self.refuse(400, "invalid_request",
                               "only a plain Messages request for the model %s, without streaming" % server.model)
        try:                    # record first: a call that cannot be recorded is not made
            call = server.store.start_call(session["id"])
            server.report(session, "model_call", {"call": call})
        except Exception as e:
            return self.fail_closed("recording the call", e)
        self.forward(session, call, body)

    def fail_closed(self, doing, error):
        sys.stderr.write("keyproxy refused a call, %s failed: %s\n" % (doing, type(error).__name__))
        self.refuse(503, "unavailable", "the call was not sent: %s failed" % doing)

    def forward(self, session, call, body):
        server = self.server
        data = json.dumps(body).encode()
        headers = {"content-type": "application/json", "x-api-key": server.key,
                   "anthropic-version": self.headers.get("anthropic-version") or "2023-06-01"}
        request = urllib.request.Request(server.upstream + "/v1/messages", data=data, headers=headers)
        # if no usage ever comes back: assume the most this call could have cost
        estimate = (len(data) // 3, body["max_tokens"])
        state, tokens, estimated = "no_answer", estimate, True
        status, answer = 504, None
        try:
            with server.opener.open(request, timeout=server.upstream_timeout) as response:
                status, answer = response.status, response.read()
            try:
                used = json.loads(answer)["usage"]
                tokens = (int(used["input_tokens"]), int(used["output_tokens"]))
                state, estimated = "done", False
            except (ValueError, KeyError, TypeError):
                state = "no_usage"
        except urllib.error.HTTPError as e:         # the provider refused the request: nothing was generated
            status, answer = (502 if e.code < 400 else e.code), e.read()
            state, tokens, estimated = "refused_upstream", (0, 0), False
        except Exception as e:                      # timeout, dropped connection, anything else
            sys.stderr.write("keyproxy: no answer from the provider: %s\n" % type(e).__name__)
        try:
            server.store.settle_call(call, state, tokens[0], tokens[1], estimated)
            server.report(session, "model_usage", {"call": call, "state": state, "input_tokens": tokens[0],
                                                   "output_tokens": tokens[1], "estimated": estimated})
        except Exception as e:                      # the call stays "open" in the store; the next one is refused
            sys.stderr.write("keyproxy: call %d could not be settled: %s\n" % (call, type(e).__name__))
        if answer is None:
            return self.refuse(504, "timeout_error", "no answer from the provider")
        self.reply(status, answer.replace(server.key.encode(), b"[key]"))


def reporter(port, secret):
    """Sends the proxy's events to the service, which is the only writer of the audit log."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def report(session, kind, detail):
        data = json.dumps({"session": session["id"], "type": kind, "detail": detail}).encode()
        request = urllib.request.Request("http://127.0.0.1:%d/internal/audit" % port, data=data,
                                         headers={"x-internal-secret": secret, "content-type": "application/json"})
        with opener.open(request, timeout=5) as response:
            response.read()
    return report


def main():
    home = os.path.abspath(os.environ["AGENTD_HOME"])
    key = read_key(os.environ["MODEL_KEY_FILE"], os.environ.get("MODEL_KEY_NAME", "DEEPSEEK_API_KEY"))
    with open(os.path.join(home, "data", "internal"), encoding="ascii") as f:
        secret = f.read().strip()
    server = KeyProxy(int(os.environ.get("KEYPROXY_PORT", "8701")),
                      Store(os.path.join(home, "data", "agentd.db"), auth.Clock()), key,
                      os.environ.get("MODEL_UPSTREAM", "https://api.deepseek.com/anthropic"),
                      os.environ.get("MODEL_NAME", "deepseek-flash"),
                      reporter(int(os.environ.get("AGENTD_PORT", "8700")), secret))
    print("keyproxy listening on 127.0.0.1:%d" % server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
