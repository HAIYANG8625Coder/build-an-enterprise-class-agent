"""May this tool call run? One function, decide(), used by the service for every call.

Three sources: the company policy (in the service's database, changed only by an admin), and a project and a user
file (in folders the agent can write, so only their deny and ask rules count).
Order: deny, then ask, then allow. No rule: ask. Anything the policy does not understand: deny.
Rule names: Read covers read_file and grep, Edit covers edit_file, Bash covers run_bash."""
import json
import os
import re
import stat
import unicodedata
from collections import namedtuple

NAMES = {"read_file": "Read", "grep": "Read", "edit_file": "Edit", "run_bash": "Bash"}
EFFECTS = ("deny", "ask", "allow")
SPLIT = re.compile(r"&&|\|\||;|\||&|\n")
# a command holding one of these cannot be judged by its words, so no allow rule covers it
UNSAFE = ("$(", "`", "<(", ">(", ">")
MAX_FILES = 2000                    # a grep over more files than this is refused
MAX_RULE_FILE = 64 * 1024
# company_rules_only: allow rules from anyone else are ignored. disable_bypass: no session may skip the asking.
LOCKS = ("company_rules_only", "disable_bypass")

Rule = namedtuple("Rule", "effect kind spec source")
Verdict = namedtuple("Verdict", "effect rule reason")


class PolicyError(Exception):
    pass


def parse_rules(body, source):
    """body: {"deny": [...], "ask": [...], "allow": [...]}. A rule that cannot be understood is an error."""
    if not isinstance(body, dict):
        raise PolicyError("rules must be an object")
    rules = []
    for effect in EFFECTS:
        entries = body.get(effect, [])
        if not isinstance(entries, list):
            raise PolicyError("%s must be a list" % effect)
        for text in entries:
            m = re.fullmatch(r"(Read|Edit|Bash)\((.+)\)", text, re.S) if isinstance(text, str) else None
            if not m:
                raise PolicyError("not a rule: %r (use Read(...), Edit(...) or Bash(...))" % (text,))
            rules.append(Rule(effect, m.group(1), m.group(2), source))
    return rules


def parse_company(body):
    """body: {"rules": {...}, "locks": {...}}. Returns (rules, locks). Anything unknown is an error."""
    if not isinstance(body, dict) or set(body) - {"rules", "locks"}:
        raise PolicyError('a policy is {"rules": {...}, "locks": {...}}')
    rules, locks = body.get("rules", {}), body.get("locks", {})
    if not isinstance(rules, dict) or set(rules) - set(EFFECTS):
        raise PolicyError("rules are deny, ask and allow")
    if not isinstance(locks, dict) or set(locks) - set(LOCKS) or not all(type(v) is bool for v in locks.values()):
        raise PolicyError("locks are %s, each true or false" % " and ".join(LOCKS))
    return parse_rules(rules, "company"), locks


def file_rules(path, root, source):
    """Rules from a file in a folder the agent can write: only deny and ask count, never allow.
    A missing file is fine. A file that cannot be read safely is an error, and the caller denies everything."""
    if not os.path.lexists(path):
        return []
    real = os.path.realpath(path)
    if real != os.path.join(os.path.realpath(root), os.path.relpath(path, root)):
        raise PolicyError("%s rule file is a link" % source)
    info = os.stat(real)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RULE_FILE:
        raise PolicyError("%s rule file is not a small plain file" % source)
    try:
        with open(real, encoding="utf-8") as f:
            rules = parse_rules(json.load(f), source)
    except (OSError, ValueError, RecursionError):
        raise PolicyError("%s rule file cannot be read" % source)     # never quote its content
    return [r for r in rules if r.effect != "allow"]


def _plain(text):
    return unicodedata.normalize("NFC", text)


def matches(rule, text):
    """* matches any text. A trailing " *" also matches the bare command. deny and ask ignore upper and lower case,
    because the Mac's file system does: Read(secret.txt) must also stop SECRET.TXT. allow does not."""
    def esc(part):
        return ".*".join(re.escape(x) for x in part.split("*"))
    spec = _plain(rule.spec)
    regex = esc(spec[:-2]) + "(?: .*)?" if spec.endswith(" *") else esc(spec)
    flags = re.S if rule.effect == "allow" else re.S | re.I
    return re.fullmatch(regex, _plain(text), flags) is not None


def _inside(path, root):
    return path == root or path.startswith(root + os.sep)


def _subjects(tool, args, root):
    """Returns (parts, whole). Every part must be allowed; a deny or ask on any part or on the whole counts."""
    if tool == "run_bash":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise PolicyError("no command")
        parts = [p.strip() for p in SPLIT.split(command) if p.strip()]
        return parts or [command], [command.strip()]
    path = args.get("path", "." if tool == "grep" else None)
    if not isinstance(path, str) or "\0" in path:
        raise PolicyError("no path")
    full = os.path.realpath(os.path.join(root, path))
    if not _inside(full, root):
        raise PolicyError("the path is outside the project")
    if tool != "grep" or not os.path.isdir(full):
        return [os.path.relpath(full, root)], []
    # grep shows lines of every file under the folder, so every one of those files is a subject
    parts, names = [], []
    for folder, _, files in os.walk(full):
        for name in files:
            shown = os.path.join(folder, name)
            target = os.path.realpath(shown)
            if not _inside(target, root):
                raise PolicyError("the folder holds a link that leads outside the project: %s"
                                  % os.path.relpath(shown, root))
            parts.append(os.path.relpath(target, root))
            names.append(os.path.relpath(shown, root))
            if len(parts) > MAX_FILES:
                raise PolicyError("too many files; search a smaller folder")
    return parts or [os.path.relpath(full, root)], names + [os.path.relpath(full, root)]


def decide(company, others, locks, tool, args, root):
    """company, others: lists of Rule. root: the project folder, already a real path."""
    kind = NAMES.get(tool)
    if kind is None:
        return Verdict("deny", None, "the policy has no rule name for the tool %s" % tool)
    try:
        parts, whole = _subjects(tool, args, root)
    except PolicyError as e:
        return Verdict("deny", None, str(e))
    if locks.get("company_rules_only"):
        others = [r for r in others if r.effect != "allow"]      # their deny and ask still count
    rules = [r for r in list(company) + list(others) if r.kind == kind]
    for effect in ("deny", "ask"):
        for rule in rules:
            if rule.effect == effect:
                for text in parts + whole:
                    if matches(rule, text):
                        return Verdict(effect, label(rule), "%s matches %s" % (label(rule), text))
    if kind == "Bash" and any(mark in args["command"] for mark in UNSAFE):
        return Verdict("ask", None, "this command writes a file or builds another command")
    used = []
    for text in parts:
        hit = next((r for r in rules if r.effect == "allow" and matches(r, text)), None)
        if hit is None:
            return Verdict("ask", None, "no rule allows %s" % text)
        used.append(hit)
    return Verdict("allow", label(used[0]), "allowed by rule")


def label(rule):
    return "%s: %s(%s)" % (rule.source, rule.kind, rule.spec)
