"""The obvious first version, kept to show what is wrong with it. Do not use it.

It imports the series 1 engine into the server and runs each request in a thread. The engine was written for one
person: the folder it works in is one module variable, agent.ROOT, shared by every thread."""
import os
import sys

from runner import ENGINE

sys.path.insert(0, ENGINE)
import agent  # noqa: E402
import turn  # noqa: E402


class ThreadRunner:
    def __init__(self, model, model_url, model_key):
        self.model, self.model_url, self.model_key = model, model_url, model_key

    def run(self, user_dir, project, session_file, task):
        import anthropic
        agent.ROOT = project                      # <- the bug: one variable for all users
        client = anthropic.Anthropic(api_key=self.model_key, base_url=self.model_url)
        try:
            return turn.one_turn(client, self.model, task, session_file)
        except Exception as e:
            return {"error": "%s: %s" % (type(e).__name__, str(e)[:300])}
