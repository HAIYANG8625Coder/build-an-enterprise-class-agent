"""One message of one session, in its own process. agent.py is not edited.

The engine decides nothing. Before every tool call it asks the service, and it runs only what the service allowed.
run_bash does not run here: the service runs the command it approved. No hooks, permission rules, memory, skills or
MCP servers are loaded from the project or the user folder.

The two talk with one JSON object per line on stdin and stdout."""
import json
import os
import sys

import agent


class Channel:
    def __init__(self, read, write):
        self.read, self.write = read, write

    def send(self, message):
        self.write.write(json.dumps(message) + "\n")
        self.write.flush()

    def receive(self, kind, number=None):
        line = self.read.readline()
        if not line:
            raise SystemExit("the service closed the channel")
        message = json.loads(line)
        if message.get("type") != kind or (number is not None and message.get("id") != number):
            raise SystemExit("unexpected message from the service: %s" % message.get("type"))
        return message


class Gate:
    """Stands where series 1 had Permissions. It holds no rules: it asks."""
    last_asked = False

    def __init__(self, channel):
        self.channel, self.number = channel, 0

    def check(self, tool, args):
        self.number += 1
        self.channel.send({"type": "tool_request", "id": self.number, "tool": tool, "args": args})
        answer = self.channel.receive("decision", self.number)
        return ("allow" if answer.get("value") == "allow" else "deny"), answer.get("reason", "")

    def remember(self, tool, args):
        pass


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        messages = json.load(f)
    for m in messages:
        if isinstance(m["content"], list):
            m["content"] = [agent.Block(b) for b in m["content"]]
    return messages


def save(path, messages):
    with open(path + ".new", "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False)
    os.replace(path + ".new", path)          # never a half-written conversation


def blocks(messages, kind):
    return [b for m in messages if isinstance(m["content"], list) for b in m["content"] if b["type"] == kind]


def one_turn(client, job, perms, hooks):
    """Returns {"text": the answer, "tools": what was called, with what result}."""
    messages = load(job["session_file"])
    earlier = {b["tool_use_id"] for b in blocks(messages or [], "tool_result")}
    messages = agent.run(job["task"], client, job["model"], messages=messages, perms=perms,
                         approver=lambda *a: "n", hooks=hooks, quiet=True)     # nobody is there to approve
    save(job["session_file"], messages)
    results = {b["tool_use_id"]: b for b in blocks(messages, "tool_result") if b["tool_use_id"] not in earlier}
    tools = [{"name": c["name"], "args": c["input"], "is_error": results[c["id"]]["is_error"],
              "result": results[c["id"]]["content"][:500]}
             for c in blocks(messages, "tool_use") if c["id"] in results]
    return {"text": agent.final_text(messages), "tools": tools, "tokens_read": agent.run.spent}


def client():
    import anthropic
    # both given on purpose: nothing about where to send, or as whom, is left to a variable we did not set
    return anthropic.Anthropic(api_key=os.environ["MODEL_KEY"], base_url=os.environ["MODEL_URL"])


def main():
    # keep the control channel for ourselves: nothing else may read from it or print into it
    channel = Channel(os.fdopen(os.dup(0), "r", encoding="utf-8"), os.fdopen(os.dup(1), "w", encoding="utf-8"))
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    os.dup2(2, 1)
    job = channel.receive("start")
    agent.ROOT = os.path.realpath(os.getcwd())
    gate = Gate(channel)

    def run_bash(command):
        channel.send({"type": "run", "id": gate.number})       # the service runs what it approved, not what we say
        answer = channel.receive("output", gate.number)
        if answer.get("error"):
            raise agent.ToolError(answer["error"])
        return answer["text"]

    agent.FUNCS["run_bash"] = run_bash
    try:
        result = one_turn(client(), job, gate, agent.Hooks())
        channel.send({"type": "result", "result": result})
    except Exception as e:
        channel.send({"type": "error", "message": "%s: %s" % (type(e).__name__, str(e)[:300])})


if __name__ == "__main__":
    main()
