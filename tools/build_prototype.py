"""Build the clickable prototype in prototype/ from the design files in prototype/src/.

Each source screen (*.dc.html) holds a <helmet> (fonts and styles), a template
inside <x-dc>, and a Component class. This script turns each one into a plain
HTML page that runs with prototype/dc-runtime.js, so the screens open in any
browser with no build tools. Standard library only.

    python3 tools/build_prototype.py
"""
import html
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.join(HERE, "..", "prototype")
SRC = os.path.join(PROTO, "src")

# source file -> (output page, title shown in the index)
SCREENS = [
    ("Main.dc.html", "session.html", "Developer session",
     "Talk to the agent. A prompt injection is blocked, then an edit waits for your answer."),
    ("Approvals.dc.html", "approvals.html", "Approvals on the phone",
     "Approve or deny waiting tool calls before the countdown denies them."),
    ("Dashboard.dc.html", "dashboard.html", "Admin dashboard",
     "Spend, tokens, sessions, approvals and safety checks, today or for the last 7 days."),
    ("Admin.dc.html", "admin.html", "Admin console",
     "Lock the company policy, publish a new version, revoke a token."),
    ("Audit.dc.html", "audit.html", "Audit log",
     "Filter events by who wrote them, verify the chain, then tamper with a line."),
]
LINKS = {src: out for src, out, _, _ in SCREENS}

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · agentd prototype</title>
{head}
<style>
body{{background:#1F1E1C}}
.dc-back{{position:fixed;right:16px;bottom:16px;padding:8px 14px;border-radius:999px;background:#30302E;border:1px solid #4A4943;color:#E8E6DC;font:500 13px 'IBM Plex Sans',system-ui,sans-serif;text-decoration:none}}
.dc-back:hover{{background:#3A3935;color:#E8E6DC}}
{extra}
</style>
</head>
<body>
<div id="app"></div>
<a class="dc-back" href="index.html">All screens</a>
<script type="text/x-template" id="dc-template">{template}</script>
<script src="dc-runtime.js"></script>
<script>
{logic}
DC.mount(Component);
</script>
</body>
</html>
"""

INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentd prototype</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&amp;family=IBM+Plex+Sans:wght@400;500;600&amp;display=swap" rel="stylesheet">
<style>
body{{margin:0;background:#262624;color:#E8E6DC;font:15px/1.5 'IBM Plex Sans',system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:56px 24px}}
h1{{font:600 30px 'IBM Plex Mono',monospace;color:#E39A7E;margin:0 0 8px}}
p.lead{{color:#C9C6BA;margin:0 0 32px}}
a.screen{{display:block;padding:18px 20px;border:1px solid #3E3D39;border-radius:10px;margin-bottom:12px;color:#E8E6DC;text-decoration:none}}
a.screen:hover,a.screen:focus-visible{{border-color:#D97757;outline:none}}
a.screen strong{{display:block;font-weight:600;margin-bottom:2px}}
a.screen span{{color:#A19F94;font-size:14px}}
footer{{color:#A19F94;font-size:13px;margin-top:32px}}
footer a{{color:#E39A7E}}
</style>
</head>
<body>
<main>
<h1>agentd</h1>
<p class="lead">The clickable prototype of the enterprise agent service, version 1.0, approved 2026-11-22. Every number is sample data, not a real run.</p>
{items}
<footer>Read the <a href="https://github.com/HAIYANG8625Coder/build-an-enterprise-class-agent/blob/main/docs/PRD.md">PRD</a> and the <a href="https://github.com/HAIYANG8625Coder/build-an-enterprise-class-agent/blob/main/docs/TRD.md">TRD</a>. Built from <code>src/</code> by <code>tools/build_prototype.py</code>.</footer>
</main>
</body>
</html>
"""


def between(text, start, end):
    i = text.index(start) + len(start)
    return text[i:text.index(end, i)]


def build(src_name, out_name, title):
    text = open(os.path.join(SRC, src_name), encoding="utf-8").read()
    xdc = between(text, "<x-dc>", "</x-dc>")
    head = between(xdc, "<helmet>", "</helmet>").strip()
    template = re.sub(r"<helmet>.*?</helmet>", "", xdc, flags=re.S).strip()
    for src, out in LINKS.items():
        template = template.replace('href="%s"' % src, 'href="%s"' % out)
    assert "</script" not in template
    logic = re.search(r'<script type="text/x-dc"[^>]*>(.*?)</script>', text, re.S).group(1).strip()
    extra = ""
    if src_name == "Approvals.dc.html":   # a fixed-size phone screen: centre it
        extra = "#app{display:flex;justify-content:center;padding:32px 16px}"
    page = PAGE.format(title=html.escape(title), head=head, extra=extra,
                       template=template, logic=logic)
    open(os.path.join(PROTO, out_name), "w", encoding="utf-8").write(page)


def main():
    items = []
    for src, out, title, blurb in SCREENS:
        build(src, out, title)
        items.append('<a class="screen" href="%s"><strong>%s</strong><span>%s</span></a>'
                     % (out, html.escape(title), html.escape(blurb)))
    index = INDEX.format(items="\n".join(items))
    open(os.path.join(PROTO, "index.html"), "w", encoding="utf-8").write(index)
    print("built", len(SCREENS), "screens and index.html")


if __name__ == "__main__":
    main()
