"""One message of one session: load the saved conversation, run the series 1 agent loop, save it again.

As a program it is "one process per session": it reads the job on stdin, runs in the user's project folder and prints
one JSON result. agent.py is not edited. Until episode 2 has a policy, the agent may only read."""
import json
import os
import sys

import agent


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


def one_turn(client, model, task, session_file):
    """Returns {"text": the answer, "tools": what was called, with what result}."""
    messages = load(session_file)
    earlier = {b["tool_use_id"] for b in blocks(messages or [], "tool_result")}
    messages = agent.run(task, client, model, messages=messages, perms=agent.Permissions(mode="read-only"),
                         approver=lambda *a: "n", hooks=agent.Hooks(), quiet=True)
    save(session_file, messages)
    results = {b["tool_use_id"]: b for b in blocks(messages, "tool_result") if b["tool_use_id"] not in earlier}
    tools = [{"name": c["name"], "args": c["input"], "is_error": results[c["id"]]["is_error"],
              "result": results[c["id"]]["content"][:500]}
             for c in blocks(messages, "tool_use") if c["id"] in results]
    return {"text": agent.final_text(messages), "tools": tools, "tokens_read": agent.run.spent}


if __name__ == "__main__":
    job = json.load(sys.stdin)
    out = os.fdopen(os.dup(1), "w")          # the result goes here; anything else that prints goes to stderr
    os.dup2(2, 1)
    import anthropic
    agent.ROOT = os.path.realpath(os.getcwd())
    try:
        # both given on purpose: nothing about where to send, or as whom, is left to a variable we did not set
        client = anthropic.Anthropic(api_key=os.environ["MODEL_KEY"], base_url=os.environ["MODEL_URL"])
        result = one_turn(client, job["model"], job["task"], job["session_file"])
    except Exception as e:
        result = {"error": "%s: %s" % (type(e).__name__, str(e)[:300])}
    json.dump(result, out)
    out.close()
