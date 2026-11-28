"""Starts one engine for one message, answers every tool request from the policy, and runs the commands it allowed.
Everything the engine sends is treated as untrusted.

There is no sandbox yet (episode 4): an allowed command runs as the same operating system user as this service."""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading

import policy

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine")
TURN_SECONDS = 600
COMMAND_SECONDS = 20
MAX_LINE = 1_000_000
MAX_REQUESTS = 200
MAX_OUTPUT = 4000


class Broken(Exception):
    """The engine sent something it never should."""


def sdk_folder():
    return os.path.dirname(os.path.dirname(importlib.util.find_spec("anthropic").origin))


def kill(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class PolicyRunner:
    def __init__(self, model, model_url, model_key, store=None):
        self.model, self.model_url, self.model_key, self.store = model, model_url, model_key, store

    def environment(self, user_dir):
        """Built from nothing. The engine still holds the model key: episode 5."""
        return {"PATH": "/usr/bin:/bin", "HOME": user_dir, "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join([ENGINE, sdk_folder()]),
                "AGENT_HOME": os.path.join(user_dir, ".agent"),
                "AGENT_MEMORY": os.path.join(user_dir, ".agent", "memory"),
                "MODEL_URL": self.model_url, "MODEL_KEY": self.model_key}

    # ------------------------------------------------------------ the decision
    def decide(self, job, tool, args):
        """The company policy is read again for every call, so a new version applies to the very next one."""
        try:
            row = self.store.policy(job["tenant"])
            if row is None:                         # also for a session that skips asking: there is nothing to skip to
                raise policy.PolicyError("your company has published no policy")
            company, locks = policy.parse_company(json.loads(row["body"]))
            others = []
            for source, base in (("project", job["project"]), ("user", job["user_dir"])):
                others += policy.file_rules(os.path.join(base, ".agent", "settings.json"), base, source)
            verdict = policy.decide(company, others, locks, tool, args, job["project"])
        except Exception as e:                      # whatever cannot be judged does not run
            reason = str(e) if isinstance(e, policy.PolicyError) else \
                "the call could not be checked (%s)" % type(e).__name__
            return {"decision": "deny", "rule": None, "reason": reason, "version": None}
        value, reason = verdict.effect, verdict.reason
        if value == "ask":
            if job["mode"] == "bypass" and not locks.get("disable_bypass"):
                value, reason = "allow", "this session runs without asking"
            else:
                value, reason = "deny", "%s; it needs approval and nobody is there to approve" % reason
        return {"decision": value, "rule": verdict.rule, "reason": reason, "version": row["version"]}

    def command(self, job, command):
        """One approved command. Its environment holds no model key."""
        env = {"PATH": os.pathsep.join([os.path.dirname(os.path.realpath(sys.executable)), "/usr/bin", "/bin"]),
               "HOME": job["user_dir"], "PYTHONDONTWRITEBYTECODE": "1"}
        with tempfile.TemporaryFile() as out:
            process = subprocess.Popen(["/bin/sh", "-c", command], cwd=job["project"], env=env,
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                code = process.wait(timeout=COMMAND_SECONDS)
            except subprocess.TimeoutExpired:
                code = None
            finally:
                kill(process)                   # also anything it left running in the background
                process.wait()
            out.seek(0)
            text = out.read(MAX_OUTPUT + 1).decode("utf-8", "replace")
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + "\n... cut off. Ask for less."
        return code, text

    # ------------------------------------------------------------ one message
    def run(self, user_dir, project, session_file, task, tenant, mode="ask"):
        """Returns {"text", "tools", "tokens_read", "decisions"} or {"error"}."""
        job = {"tenant": tenant, "user_dir": user_dir, "project": project, "mode": mode}
        errors = tempfile.TemporaryFile()
        process = subprocess.Popen([sys.executable, os.path.join(ENGINE, "turn.py")], cwd=project,
                                   env=self.environment(user_dir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, start_new_session=True)
        timer = threading.Timer(TURN_SECONDS, kill, [process])
        timer.start()
        approved, seen, decisions = {}, set(), []

        def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        try:
            send({"type": "start", "task": task, "model": self.model, "session_file": session_file})
            while True:
                line = process.stdout.readline(MAX_LINE)
                if not line:
                    raise Broken("the engine stopped without finishing")
                message = json.loads(line)
                kind = message["type"]
                if kind == "tool_request":
                    number, tool, args = message["id"], message["tool"], message["args"]
                    if (type(number) is not int or number in seen or not isinstance(tool, str)
                            or not isinstance(args, dict) or len(seen) >= MAX_REQUESTS):
                        raise Broken("a bad tool request")
                    seen.add(number)
                    approved.clear()            # an approval is only good for the call it was given for
                    verdict = self.decide(job, tool, args)
                    decisions.append(dict(verdict, tool=tool[:64], args=args))
                    if verdict["decision"] == "allow" and tool == "run_bash":
                        approved[number] = args["command"]
                    send({"type": "decision", "id": number, "value": verdict["decision"], "reason": verdict["reason"]})
                elif kind == "run":
                    number = message["id"]
                    command = approved.pop(number, None) if type(number) is int else None
                    if command is None:
                        send({"type": "output", "id": number, "error": "that command was not approved"})
                        continue
                    code, text = self.command(job, command)
                    if code is None:
                        send({"type": "output", "id": number, "error": "the command took too long"})
                    else:
                        send({"type": "output", "id": number, "text": "exit code %d\n%s" % (code, text)})
                elif kind == "result":
                    return dict(message["result"], decisions=decisions)
                elif kind == "error":
                    return {"error": str(message["message"])[:300], "decisions": decisions}
                else:
                    raise Broken("an unknown message")
        except (Broken, BrokenPipeError, ValueError, KeyError, TypeError, RecursionError) as e:
            errors.seek(0)
            sys.stderr.write(errors.read()[-2000:].decode("utf-8", "replace"))
            return {"error": "the engine failed: %s" % type(e).__name__, "decisions": decisions}
        finally:
            timer.cancel()
            kill(process)
            process.wait()
            for pipe in (process.stdin, process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
            errors.close()


class NaiveRunner(PolicyRunner):
    """The first try: the engine loads rules and hooks from the folders and decides for itself."""

    def run(self, user_dir, project, session_file, task, tenant, mode="ask"):
        job = {"model": self.model, "task": task, "session_file": session_file}
        try:
            done = subprocess.run([sys.executable, os.path.join(ENGINE, "naive_turn.py")], input=json.dumps(job),
                                  cwd=project, env=self.environment(user_dir), capture_output=True, text=True,
                                  timeout=TURN_SECONDS)
            return json.loads(done.stdout)
        except subprocess.TimeoutExpired:
            return {"error": "the turn took too long"}
        except ValueError:
            sys.stderr.write(done.stderr[-2000:])
            return {"error": "the engine failed"}
