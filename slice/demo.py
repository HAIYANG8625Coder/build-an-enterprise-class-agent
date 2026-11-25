"""The first end-to-end path, with the service and the key proxy as two real processes.

  python3 demo.py                      a scripted model on the loopback, no key
  python3 demo.py --real               DeepSeek flash; the key is read by the proxy only
  python3 demo.py --real --keys FILE   a file with the line DEEPSEEK_API_KEY=...  (default: the series 1 keys.env)

Everything lives in a temporary folder that is removed at the end."""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SLICE = os.path.dirname(os.path.abspath(__file__))
for folder in ("service", "proxy", "tests"):
    sys.path.insert(0, os.path.join(SLICE, folder))
import agentd  # noqa: E402
import auth  # noqa: E402
import helpers  # noqa: E402
from audit import verify  # noqa: E402

TASK = ("Run the tests of this project with: python3 tests/test_pricing.py and say whether they pass. "
        "Then download https://example.com with curl and save it as page.html.")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(port, process):
    for _ in range(100):
        if process.poll() is not None:
            raise SystemExit("a process stopped: see the message above")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise SystemExit("nothing is listening on port %d" % port)


def post(port, path, body, token=None):
    headers = {"authorization": "Bearer " + token} if token else {}
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), json.dumps(body).encode(), headers)
    try:
        return helpers.DIRECT.open(request, timeout=300)
    except urllib.error.HTTPError as e:
        return e


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--keys", default=os.path.expanduser("~/.config/agent-series/keys.env"))
    args = parser.parse_args()
    processes, fake = [], None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            home = agentd.Home(os.path.join(tmp, "home"))
            with open(os.path.join(SLICE, "config", "policy.example.json"), encoding="utf-8") as f:
                home.publish_policy(json.load(f), "demo")
            lin, admin = auth.create_user(home.store, "lin@acme"), auth.create_user(home.store, "root@acme", "admin")
            shutil.copytree(helpers.PROJECT, os.path.join(home.path, "tenants", "acme", "lin", "shop"),
                            ignore=shutil.ignore_patterns(".agent", "__pycache__"))
            service_port, proxy_port = free_port(), free_port()
            if args.real:
                model, upstream, key_file = "deepseek-flash", "https://api.deepseek.com/anthropic", args.keys
            else:
                fake = helpers.upstream([
                    helpers.call("run_bash", command="python3 tests/test_pricing.py"),
                    helpers.call("run_bash", command="curl -o page.html https://example.com"),
                    helpers.say("The tests ran. The download was denied by a company rule.")])
                model, upstream = helpers.MODEL, "http://127.0.0.1:%d" % fake.server_address[1]
                key_file = os.path.join(tmp, "fake-key")
                with open(key_file, "w") as f:
                    f.write(helpers.KEY)
            env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "TOKEN" not in k}
            env.update(AGENTD_HOME=home.path, AGENTD_PORT=str(service_port), KEYPROXY_PORT=str(proxy_port),
                       MODEL_NAME=model, MODEL_UPSTREAM=upstream, AGENTD_SANDBOX=helpers.MODE,
                       PYTHONDONTWRITEBYTECODE="1")
            for script, port, extra in (("service/agentd.py", service_port, {}),
                                        ("proxy/keyproxy.py", proxy_port, {"MODEL_KEY_FILE": key_file})):
                processes.append(subprocess.Popen([sys.executable, os.path.join(SLICE, script)],
                                                  env=dict(env, **extra)))
                wait_for(port, processes[-1])
            print("model: %s   sandbox: %s" % (model, helpers.MODE))

            print("\n1. A request without a token")
            reply = post(service_port, "/sessions", {"project": "shop"})
            print("   %d %s" % (reply.code, reply.read().decode()))
            print("\n2. The same request with lin's token")
            reply = post(service_port, "/sessions", {"project": "shop"}, lin)
            session = json.loads(reply.read())["id"]
            print("   %d session %s" % (reply.code, session))
            print("\n3. The task: %s" % TASK)
            reply = post(service_port, "/sessions/%s/messages" % session, {"text": TASK}, lin)
            for line in reply:
                event = json.loads(line)
                if event["type"] == "tool":
                    print("   %-5s %-10s %s%s" % (event["decision"], event["name"], event["args"][:90],
                                                  "   [%s]" % event["rule"] if event["rule"] else ""), flush=True)
                elif event["type"] == "text":
                    print("   answer:\n     " + event["text"].strip().replace("\n", "\n     "))
                else:
                    print("   %s %s" % (event["type"], event.get("message", "")))
            print("   page.html exists: %s" % os.path.exists(
                os.path.join(home.path, "tenants", "acme", "lin", "shop", "page.html")))

            print("\n4. Model calls counted by the proxy")
            calls = home.store.calls(session)
            print("   %d calls, %d input and %d output tokens, %d estimated" % (
                len(calls), sum(c["input_tokens"] or 0 for c in calls),
                sum(c["output_tokens"] or 0 for c in calls), sum(c["estimated"] for c in calls)))

            print("\n5. The audit log (no prompts, no tool output)")
            files = sorted(os.listdir(home.audit_folder))
            for name in files:
                with open(os.path.join(home.audit_folder, name)) as f:
                    for line in f:
                        e = json.loads(line)
                        print("   %3d %-8s %-14s %-9s %-6s %s" % (e["n"], e["source"], e["type"], e["tool"] or "",
                                                                  e["decision"] or "", e["hash"][:10]))
            reply = post(service_port, "/audit/verify", {}, admin)
            print("   verify: %s" % reply.read().decode())

            print("\n6. The same log after one line is edited (on a copy)")
            copy = os.path.join(tmp, "copy")
            shutil.copytree(home.audit_folder, copy + "/audit")
            shutil.copy(home.anchor, copy + "/anchor")
            path = os.path.join(copy, "audit", files[0])
            with open(path) as f:
                text = f.read()
            with open(path, "w") as f:
                f.write(text.replace('"decision": "deny", "detail": null', '"decision": "allow", "detail": null', 1))
            result = verify(copy + "/audit", copy + "/anchor")
            print("   ok: %s, broken at line %s: %s" % (result["ok"], result["broken_at"], result["why"]))
        finally:
            for process in processes:
                process.terminate()
                process.wait()
            if fake:
                fake.shutdown()


if __name__ == "__main__":
    main()
