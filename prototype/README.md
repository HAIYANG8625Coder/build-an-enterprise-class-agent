# agentd prototype (v1.0)

Clickable screens for **agentd**, the enterprise agent service in the [PRD](../docs/PRD.md) and [TRD](../docs/TRD.md). Approved on 2026-11-22. Every number on these screens is sample data, not a real run.

## Open it

**Live:** [https://haiyang8625coder.github.io/build-an-enterprise-class-agent/prototype/](https://haiyang8625coder.github.io/build-an-enterprise-class-agent/prototype/)

Or download the repo and open `prototype/index.html` in a browser. Nothing needs to be installed. The pages load the IBM Plex fonts from Google Fonts, and fall back to system fonts when offline.

| Screen | Page | Try this |
| --- | --- | --- |
| Developer session | [`session.html`](session.html) | Answer the permission box with Yes or No |
| Approvals on the phone | [`approvals.html`](approvals.html) | Approve or deny the waiting requests |
| Admin dashboard | [`dashboard.html`](dashboard.html) | Switch between Today and Last 7 days |
| Admin console | [`admin.html`](admin.html) | Turn on "Company rules only", then publish v8; revoke a token |
| Audit log | [`audit.html`](audit.html) | Edit line 1,281, then verify the chain |

## How it is built

The screens were designed as components on a design canvas. Their sources are in [`src/`](src/): one `.dc.html` file per screen, plus the canvas layout. Each holds a template, its styles and a small `Component` class. [`tools/build_prototype.py`](../tools/build_prototype.py) turns each one into a plain HTML page, and [`dc-runtime.js`](dc-runtime.js) (about 115 lines, no dependencies) renders the template and handles clicks.

To rebuild after changing a source file:

```bash
python3 tools/build_prototype.py
```
