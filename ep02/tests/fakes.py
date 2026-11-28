"""A model and a clock that tests control. No API key, no network, no waiting.

FakeModel stands in for anthropic.Anthropic: the engine calls client.messages.create() or .stream() and gets the next
step of a script. FakeClock stands in for the time module, so a 5 minute approval timeout or an 8 hour token expiry can
be tested in no time."""
import json
import os
import sys

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine")
sys.path.insert(0, ENGINE)
import agent  # noqa: E402

Block = agent.Block


def say(text):
    """A step: the model answers with text and ends its turn."""
    return {"text": text, "tool": None}


def call(name, args, text=""):
    """A step: the model asks for one tool."""
    return {"text": text, "tool": (name, args)}


def usage(input_tokens=10, output_tokens=5):
    return Block(input_tokens=input_tokens, output_tokens=output_tokens,
                 cache_read_input_tokens=0, cache_creation_input_tokens=0)


class _Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        for block in self.message.content:
            if block.get("type") == "text":
                yield block["text"]

    def get_final_message(self):
        return self.message


class FakeModel:
    """script: a list of steps made with say() and call(). An exception in the list is raised instead of an answer.
    A call past the end of the script fails the test: a loop that never ends must not pass quietly."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []          # what the engine sent, one dict per call
        self.messages = self        # so that client.messages.create(...) works

    def _next(self, request):
        self.requests.append(json.loads(json.dumps(request, default=lambda block: dict(block))))
        if len(self.requests) > len(self.script):
            raise AssertionError("the script has %d steps and the engine asked for one more" % len(self.script))
        step = self.script[len(self.requests) - 1]
        if isinstance(step, BaseException):
            raise step
        content = []
        if step["text"]:
            content.append(Block(type="text", text=step["text"]))
        if step["tool"]:
            name, args = step["tool"]
            content.append(Block(type="tool_use", id="t%d" % len(self.requests), name=name, input=args))
        return Block(stop_reason="tool_use" if step["tool"] else "end_turn", usage=usage(), content=content)

    def create(self, **request):
        return self._next(request)

    def stream(self, **request):
        return _Stream(self._next(request))


class FakeClock:
    """now() returns seconds that move only when the test says so. sleep() returns at once and moves the clock."""

    def __init__(self, start=0.0):
        self.seconds = float(start)

    def now(self):
        return self.seconds

    def sleep(self, seconds):
        self.advance(seconds)

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("a clock does not go backwards")
        self.seconds += seconds
