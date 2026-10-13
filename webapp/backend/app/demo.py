"""Synthetic demo data: a believable week of a developer working with coding
agents — window focus, AFK, Claude Code + pi sessions, git commits — for
`journal demo` and the README screenshots. Nothing here touches your real
data: the demo runs with its own database and source folders (JOURNAL_DEMO).

    python -m app.demo <dir>      build <dir>/demo.db from scratch
"""
import datetime as dt
import json
import os
import random
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

PROJECTS = {
    "atlas-api": {
        "files": ["rate_limiter.py", "routes/orders.py", "models.py", "tests/test_orders.py"],
        "browser": ["Fix token-bucket refill · Pull Request #128 · acme/atlas-api", "Redis docs: INCR",
                    "atlas-api · Actions · CI #412"],
        "prompts": ["the rate limiter lets bursts through after a deploy — find out why and add a regression test",
                    "add pagination to GET /orders, keep the old response shape for v1 clients",
                    "CI is red on test_orders, fix it",
                    "review the diff and tighten the error messages",
                    "write a migration for the new order_status column"],
        "commits": ["fix: refill token bucket from last timestamp", "feat: cursor pagination for /orders",
                    "test: regression for burst after deploy", "chore: tighten 4xx messages",
                    "feat: order_status migration"],
    },
    "lumen-ui": {
        "files": ["src/Button.tsx", "src/Table.tsx", "src/theme.css", "src/hooks/useOrders.ts"],
        "browser": ["localhost:5173 — Lumen", "Storybook — Table / Sorting", "Figma — Lumen components"],
        "prompts": ["the table re-renders on every keystroke, memoize the rows",
                    "add dark mode tokens to theme.css and wire them into Button",
                    "build a sortable Table story with 1k rows",
                    "useOrders should cancel the fetch when the page changes"],
        "commits": ["perf: memoize table rows", "feat: dark mode tokens", "docs: sortable table story",
                    "fix: abort stale order fetches"],
    },
    "rag-eval": {
        "files": ["eval.ipynb", "metrics.py", "datasets/qa.jsonl"],
        "browser": ["arXiv · Evaluating Retrieval-Augmented Generation", "Weights & Biases — rag-eval runs"],
        "prompts": ["run the eval on the new chunker and compare recall@5 against last week",
                    "plot faithfulness vs. context length, one line per retriever",
                    "summarize what changed between run 41 and run 44"],
        "commits": ["eval: chunker v2 baseline", "plot: faithfulness vs context length"],
        "reading": ["Retrieval-Augmented Generation for Knowledge-Intensive NLP - Lewis et al. - Zotero",
                    "Lost in the Middle: How Language Models Use Long Contexts - Zotero"],
    },
}
OTHER = [("Slack", "#team-platform - Acme - Slack"), ("Slack", "DM · Priya - Acme - Slack"),
         ("Google-chrome", "Inbox (3) - Gmail"), ("zoom", "Zoom Meeting"),
         ("Google-chrome", "Q4 planning - Notion")]
AUTHOR = "Demo Dev"
SAYS = ["Let me look at how this is wired first.", "Found it — the cause is in the refill step.",
        "Running the tests to confirm.", "Two tests fail; fixing the fixture.", "Applying the change.",
        "All green. Summarizing the diff.", "Checking the call sites before editing.",
        "That approach breaks v1 clients; trying a compatible one."]


def _ts(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _aw(rows, t, dur, app, title):
    rows.append(("aw_window", t, dur, {"app": app, "title": title}))


def _day_windows(rng, day0: dt.datetime, focus: list, until: dt.datetime) -> list:
    """Window focus for one workday: project blocks, chat, meetings, lunch."""
    rows, t = [], day0.replace(hour=9, minute=rng.randint(10, 40))
    afk = [(t.replace(hour=12, minute=30), t.replace(hour=13, minute=25))]
    end = t.replace(hour=18, minute=rng.randint(10, 50))
    meeting = t.replace(hour=rng.choice([10, 11, 15, 16]), minute=0)
    while t < min(end, until):
        if afk[0][0] <= t < afk[0][1]:
            rows.append(("aw_afk", t, (afk[0][1] - t).total_seconds(), {"status": "afk"}))
            t = afk[0][1]
            continue
        if meeting <= t < meeting + dt.timedelta(minutes=30):
            _aw(rows, t, 1800, "zoom", "Zoom Meeting")
            t += dt.timedelta(minutes=30)
            continue
        if rng.random() < 0.2:
            app, title = rng.choice(OTHER[:3] + OTHER[4:])
            d = rng.randint(40, 300)
            _aw(rows, t, d, app, title)
            t += dt.timedelta(seconds=d)
            continue
        p = rng.choices(focus, weights=[5, 3, 2][:len(focus)])[0]
        spec = PROJECTS[p]
        block_end = t + dt.timedelta(minutes=rng.randint(8, 35))
        while t < block_end and t < min(end, until):
            r = rng.random()
            if r < 0.45:
                app, title = "Code", f"{rng.choice(spec['files'])} — {p} — Visual Studio Code"
            elif r < 0.7:
                app, title = "com.mitchellh.ghostty", f"claude · ~/Projects/{p}"
            elif r < 0.9 or "reading" not in spec:
                app, title = "Google-chrome", rng.choice(spec["browser"]) + " - Google Chrome"
            else:
                app, title = "Zotero", rng.choice(spec["reading"])
            d = rng.randint(30, 420)
            _aw(rows, t, d, app, title)
            t += dt.timedelta(seconds=d)
    return rows


def _session(rng, start: dt.datetime, proj: str, cwd: Path, kind: str, until: dt.datetime) -> list:
    """JSONL lines of one agent session: prompts, tool calls, results."""
    sid, lines, t = str(uuid.uuid4()), [], start
    spec = PROJECTS[proj]
    if kind == "pi":
        lines.append({"type": "session", "timestamp": _ts(t), "cwd": str(cwd), "sessionId": sid})
    for k in range(rng.randint(2, 5)):
        if t > until:
            break
        prompt = spec["prompts"][(k + rng.randint(0, 9)) % len(spec["prompts"])]
        if kind == "pi":
            lines.append({"type": "message", "timestamp": _ts(t),
                          "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
        else:
            lines.append({"type": "user", "timestamp": _ts(t), "cwd": str(cwd), "sessionId": sid,
                          "message": {"role": "user", "content": prompt}})
        for _ in range(rng.randint(10, 40)):         # the agent works on its own for a while
            t += dt.timedelta(seconds=rng.randint(15, 140))
            if t > until:
                break
            tool = rng.choice(["Read", "Edit", "Bash", "Grep", "Edit", "Bash"])
            arg = {"file_path": str(cwd / rng.choice(spec["files"]))} if tool in ("Read", "Edit") else \
                {"command": rng.choice(["pytest -q", "npm test", "git diff --stat", "ruff check ."])}
            usage = {"input_tokens": rng.randint(800, 9000), "output_tokens": rng.randint(80, 900)}
            if kind == "pi":
                lines.append({"type": "message", "timestamp": _ts(t), "message": {
                    "role": "assistant", "model": "glm-5.3", "usage": usage,
                    "content": [{"type": "toolCall", "name": tool.lower(), "arguments": arg}]}})
            else:
                lines.append({"type": "assistant", "timestamp": _ts(t), "message": {
                    "role": "assistant", "model": "claude-sonnet-4-5", "usage": usage,
                    "content": [{"type": "text", "text": rng.choice(SAYS)},
                                {"type": "tool_use", "name": tool, "input": arg}]}})
                lines.append({"type": "user", "timestamp": _ts(t + dt.timedelta(seconds=2)), "sessionId": sid,
                              "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}})
        t += dt.timedelta(minutes=rng.randint(2, 14))   # you read the result, think, reply
    return lines


def _git(repo: Path, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **(env or {})})


def build(root: Path, days: int = 14, seed: int = 7) -> dict:
    rng = random.Random(seed)
    if root.exists():
        shutil.rmtree(root)
    (root / "claude").mkdir(parents=True)
    (root / "pi").mkdir()
    tz = dt.datetime.now().astimezone().tzinfo
    now = dt.datetime.now(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    repos = {}
    for p in PROJECTS:
        repo = root / "projects" / p
        repo.mkdir(parents=True)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.name", AUTHOR)
        _git(repo, "config", "user.email", "demo@example.com")
        repos[p] = repo

    aw_rows, n_sessions, n_commits = [], 0, 0
    for back in range(days - 1, -1, -1):
        day0 = today - dt.timedelta(days=back)
        if day0.isoweekday() > 5 and back:
            continue                                       # weekend off
        focus = rng.sample(list(PROJECTS), k=3)
        aw_rows += _day_windows(rng, day0, focus, now)
        for i in range(rng.randint(6, 9)):                 # agent sessions, several in parallel
            p = focus[i % len(focus)] if i < 4 else rng.choice(focus)
            start = day0.replace(hour=rng.choice([9, 10, 10, 11, 13, 14, 14, 15, 16]), minute=rng.randint(0, 59))
            if start > now:
                continue
            kind = "pi" if rng.random() < 0.3 else "claude"
            lines = _session(rng, start, p, repos[p], kind, now)
            d = (root / kind / ("-" + str(repos[p]).strip("/\\").replace("/", "-").replace("\\", "-").replace(":", "")))
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{uuid.UUID(int=rng.getrandbits(128))}.jsonl"
            f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
            last = dt.datetime.fromisoformat(lines[-1]["timestamp"].replace("Z", "+00:00")).timestamp()
            os.utime(f, (last, last))                    # finished sessions must not look "running"
            n_sessions += 1
        for p in focus[:2]:
            for _ in range(rng.randint(1, 3)):
                at = day0.replace(hour=rng.randint(10, 18), minute=rng.randint(0, 59))
                if at > now:
                    continue
                f = repos[p] / "CHANGELOG.md"
                f.write_text(f.read_text(encoding="utf-8") + f"- {at:%m-%d %H:%M}\n" if f.exists() else "# changes\n",
                             encoding="utf-8")
                _git(repos[p], "add", "-A")
                stamp = at.isoformat()
                _git(repos[p], "commit", "-q", "-m", rng.choice(PROJECTS[p]["commits"]),
                     env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
                n_commits += 1

    # windows go straight into the store (no ActivityWatch needed for the demo)
    from . import db, ingest
    from .timeutil import iso, local_day_key, now_utc
    with db.Tx():
        for i, (etype, t, dur, data) in enumerate(sorted(aw_rows, key=lambda r: r[1])):
            u = t.astimezone(dt.timezone.utc)
            db.ex("INSERT INTO source_events(source_key,event_key,event_type,occurred_at,observed_at,day_key,"
                  "payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  ("activitywatch", f"demo-{i}", etype, iso(u), iso(now_utc()), local_day_key(u),
                   json.dumps({"bucket": "demo", "data": data, "duration": dur}), iso(now_utc())))
    r = ingest.run_all()
    return {"windows": len(aw_rows), "sessions": n_sessions, "commits": n_commits,
            "ingested": r.get("total_new"), "dir": str(root)}


if __name__ == "__main__":
    if not os.environ.get("JOURNAL_DEMO"):
        sys.exit("set JOURNAL_DEMO=<dir> (the demo must not write to your real database)")
    print(json.dumps(build(Path(os.environ["JOURNAL_DEMO"])), ensure_ascii=False))
