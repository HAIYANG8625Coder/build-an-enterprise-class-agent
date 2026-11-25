"""Starts one engine in its sandbox, answers it over the control channel, and runs the commands it was allowed.
Everything the engine sends is untrusted input."""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections import namedtuple

import policy

SLICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(SLICE, "engine")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
TURN_SECONDS = 600
COMMAND_SECONDS = 20
MAX_LINE = 1_000_000
MAX_REQUESTS = 200                  # tool requests in one turn
MAX_MESSAGES = 1000                 # messages of any kind in one turn: the engine cannot flood the log
MAX_OUTPUT = 4000

# user_dir: the one folder the engine may write. project: the folder inside it that the agent works on.
Job = namedtuple("Job", "tenant user session user_dir project")


class Broken(Exception):
    """The engine sent something it never should."""


def sandboxed(mode, profile, **params):
    """The words to put in front of a command. "none" is for tests on machines without sandbox-exec."""
    if mode == "none":
        return []
    if mode != "seatbelt":
        raise ValueError("sandbox mode %r is not built yet (seatbelt, or none for tests)" % mode)
    words = [SANDBOX_EXEC]
    for key, value in params.items():
        words += ["-D", "%s=%s" % (key, value)]
    return words + ["-f", os.path.join(SLICE, "sandbox", profile)]


def sdk_folder():
    spec = importlib.util.find_spec("anthropic")
    if spec is None:
        raise RuntimeError("the anthropic package is not installed")
    return os.path.dirname(os.path.dirname(os.path.realpath(spec.origin)))


def kill(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Runner:
    def __init__(self, audit, load_rules, proxy_port, model, mode="seatbelt", approver=None):
        """load_rules() returns (company rules, locks), read again for every call so a new policy applies at once.
        approver(job, tool, args) answers an "ask"; without one the answer is no."""
        self.audit, self.load_rules, self.approver = audit, load_rules, approver
        self.proxy_port, self.model, self.mode = proxy_port, model, mode
        self.engine = ENGINE                # the folder with bridge.py; a test puts a hostile one here

    def record(self, job, kind, source="service", **fields):
        return self.audit.append(tenant=job.tenant, user=job.user, session=job.session, type=kind, source=source,
                                 **fields)

    def decide(self, job, tool, args):
        try:
            company, locks = self.load_rules()
            others = []
            for source, base in (("project", job.project), ("user", job.user_dir)):
                others += policy.file_rules(os.path.join(base, ".agent", "settings.json"), base, source)
            return policy.decide(company, others, locks, tool, args, job.project)
        except policy.PolicyError as e:
            return policy.Verdict("deny", None, str(e))
        except (OSError, RecursionError) as e:          # whatever cannot be judged does not run
            return policy.Verdict("deny", None, "the call could not be checked (%s)" % type(e).__name__)

    def command(self, job, command):
        """One command in the command sandbox: no network, writes only in the user's folder, no session token."""
        env = {"PATH": os.pathsep.join([os.path.dirname(os.path.realpath(sys.executable)), "/usr/bin", "/bin"]),
               "HOME": job.user_dir, "TMPDIR": os.path.join(job.user_dir, "tmp"), "PYTHONDONTWRITEBYTECODE": "1"}
        words = sandboxed(self.mode, "command.sb", TENANT=job.user_dir) + ["/bin/sh", "-c", command]
        with tempfile.TemporaryFile() as out:
            process = subprocess.Popen(words, cwd=job.project, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = process.wait(timeout=COMMAND_SECONDS)
            except subprocess.TimeoutExpired:
                code = None
            finally:
                kill(process)               # also anything it left running in the background
                process.wait()
            out.seek(0)
            text = out.read(MAX_OUTPUT + 1).decode("utf-8", "replace")
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + "\n... cut off. Ask for less."
        return code, text

    def run(self, job, task, session_token):
        """Yields the events a user may see: tool, text, done, error."""
        os.makedirs(os.path.join(job.user_dir, "tmp"), exist_ok=True)
        sdk = sdk_folder()
        env = {"PATH": "/usr/bin:/bin", "HOME": job.user_dir, "TMPDIR": os.path.join(job.user_dir, "tmp"),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": os.pathsep.join([self.engine, sdk]),
               "AGENT_HOME": os.path.join(job.user_dir, ".agent"),
               "AGENT_MEMORY": os.path.join(job.user_dir, ".agent", "memory"),
               "SESSION_TOKEN": session_token, "PROXY_URL": "http://127.0.0.1:%d" % self.proxy_port}
        words = sandboxed(self.mode, "engine.sb", ENGINE=self.engine, DEPS=sdk, TENANT=job.user_dir,
                          PROXY="localhost:%d" % self.proxy_port)
        words += [os.path.realpath(sys.executable), os.path.join(self.engine, "bridge.py")]
        errors = tempfile.TemporaryFile()
        process = subprocess.Popen(words, cwd=job.project, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, start_new_session=True)
        timer = threading.Timer(TURN_SECONDS, kill, [process])
        timer.start()
        approved, seen, finished, count = {}, set(), False, 0

        def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        try:
            send({"type": "start", "task": task, "model": self.model})
            while not finished:
                line = process.stdout.readline(MAX_LINE)
                if not line:
                    raise Broken("the engine stopped without finishing")
                count += 1
                if count > MAX_MESSAGES:
                    raise Broken("too many messages")
                try:
                    message = json.loads(line)
                    kind = message["type"]
                    if kind == "tool_request":
                        number, tool, args = message["id"], message["tool"], message["args"]
                        if (type(number) is not int or number in seen or not isinstance(tool, str)
                                or not isinstance(args, dict) or len(seen) >= MAX_REQUESTS):
                            raise Broken("a bad tool request")
                        seen.add(number)
                        approved.clear()                    # an approval is only good for the call it was given for
                        verdict = self.decide(job, tool, args)
                        value, reason = verdict.effect, verdict.reason
                        if value == "ask":
                            yes = bool(self.approver and self.approver(job, tool, args))
                            value = "allow" if yes else "deny"
                            reason = ("a person approved it" if yes else
                                      "%s; it needs approval and nobody approved it" % reason)
                        # written before the engine hears it: the log may show more than ran, never less
                        event = self.record(job, "tool_decision", tool=tool[:64], decision=value, rule=verdict.rule)
                        if value == "allow" and tool == "run_bash":
                            approved[number] = args["command"]
                        send({"type": "decision", "id": number, "value": value, "reason": reason})
                        shown = args.get("command") or args.get("path") or args.get("pattern") or ""
                        yield {"type": "tool", "name": tool, "args": str(shown)[:200], "decision": value,
                               "rule": verdict.rule, "event": event}
                    elif kind == "run":
                        number = message["id"]
                        command = approved.pop(number, None) if type(number) is int else None
                        if command is None:
                            self.record(job, "mismatch", decision="deny", detail={"asked_to_run": "not approved"})
                            send({"type": "output", "id": number, "error": "that command was not approved"})
                            continue
                        code, text = self.command(job, command)
                        self.record(job, "command", tool="run_bash", detail={"exit_code": code})
                        if code is None:
                            send({"type": "output", "id": number, "error": "the command took too long"})
                        else:
                            send({"type": "output", "id": number, "text": "exit code %d\n%s" % (code, text)})
                    elif kind == "tool_result":
                        self.record(job, "tool_result", source="engine", tool=str(message["tool"])[:64],
                                    detail={"is_error": bool(message["is_error"])})
                    elif kind == "text":
                        yield {"type": "text", "text": str(message["text"])}
                    elif kind == "done":
                        finished = True
                        self.record(job, "turn_done")
                        yield {"type": "done"}
                    elif kind == "error":
                        finished = True
                        self.record(job, "engine_error", source="engine")
                        yield {"type": "error", "message": str(message["message"])[:300]}
                    else:
                        raise Broken("an unknown message")
                except (ValueError, KeyError, TypeError, RecursionError):
                    raise Broken("a message that cannot be read")
        except (Broken, BrokenPipeError) as e:
            finished = True
            errors.seek(0)
            sys.stderr.write("engine %s: %s\n%s\n" % (job.session, e, errors.read()[-2000:].decode("utf-8", "replace")))
            self.record(job, "engine_error")
            yield {"type": "error", "message": "the engine failed: %s" % e}
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
            if not finished:
                self.record(job, "turn_abandoned")
