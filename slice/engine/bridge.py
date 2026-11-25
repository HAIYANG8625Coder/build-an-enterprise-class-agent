"""The engine's entry point inside the sandbox. It runs one message through the series 1 agent loop and talks to the
runner with one JSON object per line on stdin and stdout.

What changes compared with series 1 (agent.py itself is not edited):
- the engine decides nothing: every tool call is asked to the service;
- run_bash does not run here: the runner runs the approved command outside this sandbox;
- no hooks, permission rules, memory, skills or MCP servers are loaded from the project or the user folder."""
import json
import os
import sys

# keep the control channel for ourselves: nothing else may read from it or print into it
CHANNEL_IN = os.fdopen(os.dup(0), "r", encoding="utf-8")
CHANNEL_OUT = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
os.dup2(2, 1)

import anthropic  # noqa: E402
import agent  # noqa: E402


def send(message):
    CHANNEL_OUT.write(json.dumps(message) + "\n")
    CHANNEL_OUT.flush()


def receive(kind, number=None):
    line = CHANNEL_IN.readline()
    if not line:
        raise SystemExit("the runner closed the channel")
    message = json.loads(line)
    if message.get("type") != kind or (number is not None and message.get("id") != number):
        raise SystemExit("unexpected message from the runner: %s" % message.get("type"))
    return message


class Gate:
    """Stands where series 1 had Permissions. It has no rules: it asks."""
    last_asked = False

    def __init__(self):
        self.number = 0

    def check(self, tool, args):
        self.number += 1
        send({"type": "tool_request", "id": self.number, "tool": tool, "args": args})
        answer = receive("decision", self.number)
        return ("allow" if answer.get("value") == "allow" else "deny"), answer.get("reason", "")

    def remember(self, tool, args):
        pass


def main():
    start = receive("start")
    agent.ROOT = os.path.realpath(os.getcwd())
    gate = Gate()

    def run_bash(command):
        send({"type": "run", "id": gate.number})       # the runner runs what it approved, not what we send
        answer = receive("output", gate.number)
        if answer.get("error"):
            raise agent.ToolError(answer["error"])
        return answer["text"]

    original = agent.run_tool

    def run_tool(name, args):
        text, bad = original(name, args)
        send({"type": "tool_result", "id": gate.number, "tool": name, "is_error": bad})
        return text, bad

    agent.FUNCS["run_bash"] = run_bash
    agent.run_tool = run_tool
    try:
        client = anthropic.Anthropic(api_key=os.environ["SESSION_TOKEN"], base_url=os.environ["PROXY_URL"],
                                     max_retries=0, timeout=130)
        messages = agent.run(start["task"], client, start["model"], perms=gate, approver=lambda *a: "n",
                             hooks=agent.Hooks(), quiet=True)
        send({"type": "text", "text": agent.final_text(messages)})
        send({"type": "done"})
    except Exception as e:
        send({"type": "error", "message": "%s: %s" % (type(e).__name__, str(e)[:300])})


if __name__ == "__main__":
    main()
