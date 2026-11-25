"""The audit log: one JSON line per event, each holding the hash of the one before, one file per day.
The newest hash is also written to an anchor file outside the log folder, so replacing the whole log shows too.
Only the service writes it. Prompts and tool output are never put in (content stays null)."""
import glob
import hashlib
import json
import os
import threading
import time

START = "0" * 64
FIELDS = ("tenant", "user", "session", "type", "tool", "decision", "rule", "source", "detail")


class AuditError(Exception):
    pass


def line_hash(event):
    body = {k: v for k, v in event.items() if k != "hash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((event["prev"] + text).encode()).hexdigest()


class AuditLog:
    def __init__(self, folder, anchor, clock):
        self.folder, self.anchor, self.clock = folder, anchor, clock
        self.lock = threading.Lock()
        os.makedirs(folder, mode=0o700, exist_ok=True)
        state = verify(folder, anchor)
        if state["checked"] == 0 and not os.path.exists(anchor):
            self.n, self.last = 0, START
        elif state["ok"]:
            self.n, self.last = state["checked"], state["last"]
        else:                                  # do not write on top of a log that does not check out
            raise AuditError("the audit log does not check out: line %s, %s" % (state["broken_at"], state["why"]))

    def append(self, **fields):
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise AuditError("unknown audit fields: %s" % ", ".join(sorted(unknown)))
        with self.lock:
            now = time.gmtime(self.clock.now())
            event = {k: fields.get(k) for k in FIELDS}
            event.update(n=self.n + 1, time=time.strftime("%Y-%m-%dT%H:%M:%SZ", now), content=None, prev=self.last)
            event["hash"] = line_hash(event)
            path = os.path.join(self.folder, time.strftime("%Y-%m-%d", now) + ".jsonl")
            with open(path, "a", encoding="ascii") as f:
                f.write(json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            with open(self.anchor + ".new", "w", encoding="ascii") as f:
                json.dump({"n": event["n"], "hash": event["hash"]}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(self.anchor + ".new", self.anchor)
            self.n, self.last = event["n"], event["hash"]
            return event["n"]


def verify(folder, anchor):
    """Reads every line. Returns {"ok", "checked", "last", "broken_at", "why"}. broken_at is a line number n."""
    n, last = 0, START

    def bad(at, why):
        return {"ok": False, "checked": n, "last": last, "broken_at": at, "why": why}

    for path in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
        with open(path, "rb") as f:
            for raw in f:
                try:
                    event = json.loads(raw.decode("ascii"))
                    if event["n"] != n + 1:
                        return bad(n + 1, "a line is missing or out of order")
                    if event["prev"] != last:
                        return bad(n + 1, "this line does not follow the one before")
                    if line_hash(event) != event["hash"]:
                        return bad(n + 1, "this line was changed")
                except (ValueError, KeyError, TypeError):
                    return bad(n + 1, "this line cannot be read")
                n, last = n + 1, event["hash"]
    try:
        with open(anchor, encoding="ascii") as f:
            kept = json.load(f)
        if kept["n"] != n or kept["hash"] != last:
            return bad(min(n, kept["n"]) + 1 if kept["n"] != n else n,
                       "the log does not end where the anchor says (%d lines, anchor %d)" % (n, kept["n"]))
    except (OSError, ValueError, KeyError, TypeError):
        return bad(n, "the anchor is missing or cannot be read")
    return {"ok": True, "checked": n, "last": last, "broken_at": None, "why": None}
