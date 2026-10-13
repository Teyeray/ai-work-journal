"""Work episodes: the narrative layer of the day ("10:20–11:45 ai-work-journal ·
terminal, 2 agents, 3 commits").

Derived purely from human blocks + agent lanes + evidence, deterministic:
  1. Bin the day into BIN-minute slots; each slot takes the label (project or
     category) holding most human-active time in it.
  2. Runs shorter than MIN_RUN slots are absorbed into the longer neighbour
     (brief switch-outs stay inside the surrounding episode).
  3. Empty gaps shorter than GAP_BREAK slots between equal labels are bridged;
     longer gaps end the episode.
Human time is still counted from blocks (exclusive), agent work is attached
alongside — never added to human time.
"""
import datetime as dt
from collections import Counter

from . import projects

BIN = dt.timedelta(minutes=5)
MIN_RUN = 3     # slots (15 min)
GAP_BREAK = 3   # empty slots (15 min) that end an episode
MIN_SLOT_SEC = 30
ACTIVITY_NAMES = {"terminal": "终端/Agent 交互", "browser": "浏览", "reading": "阅读",
                  "coding": "编码", "writing": "写作", "files": "文件",
                  "communication": "沟通", "system": "系统", "other": "其他"}


def _ov(a0, a1, b0, b1) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def _runs(labels):
    runs = []
    for i, lab in enumerate(labels):
        if runs and runs[-1][0] == lab:
            runs[-1][2] = i + 1
        else:
            runs.append([lab, i, i + 1])
    return runs


def _merge_equal(runs):
    out = []
    for r in runs:
        if out and out[-1][0] == r[0]:
            out[-1][2] = r[2]
        else:
            out.append(list(r))
    return out


def _smooth(runs):
    """Absorb short labelled runs into the longer labelled neighbour."""
    changed = True
    while changed:
        changed = False
        for k, r in enumerate(runs):
            if r[0] is None or r[2] - r[1] >= MIN_RUN:
                continue
            nb = [x for x in (runs[k - 1] if k else None,
                              runs[k + 1] if k + 1 < len(runs) else None)
                  if x and x[0] is not None]
            if nb:
                r[0] = max(nb, key=lambda x: x[2] - x[1])[0]
                changed = True
        runs = _merge_equal(runs)
    # bridge short empty gaps between equal labels
    out = []
    for r in runs:
        if (r[0] is not None and len(out) >= 2 and out[-1][0] is None
                and out[-1][2] - out[-1][1] < GAP_BREAK and out[-2][0] == r[0]):
            out.pop()
            out[-1][2] = r[2]
        else:
            out.append(r)
    return out


def build(day_start, blocks, agent, prompts, commits):
    """blocks: [{start,end,project,activity,titles}], agent: lanes with
    segments [(s,e)], prompts: [{at,text,project,session_id}],
    commits: [{at,repo,subject}] — all datetimes tz-aware."""
    n = 24 * 60 // 5
    slots = [Counter() for _ in range(n)]
    for b in blocks:
        i = max(0, int((b["start"] - day_start) / BIN))
        while i < n and day_start + i * BIN < b["end"]:
            s0 = day_start + i * BIN
            slots[i][b["project"]] += _ov(b["start"], b["end"], s0, s0 + BIN)
            i += 1
    labels = [c.most_common(1)[0][0] if c and sum(c.values()) >= MIN_SLOT_SEC else None
              for c in slots]
    eps = []
    for lab, i0, i1 in _smooth(_runs(labels)):
        if lab is None:
            continue
        s, e = day_start + i0 * BIN, day_start + i1 * BIN
        inside = [b for b in blocks if b["end"] > s and b["start"] < e]
        if not inside:
            continue
        s = max(s, min(b["start"] for b in inside))
        e = min(e, max(b["end"] for b in inside))
        eps.append(_describe(lab, s, e, inside, agent, prompts, commits))
    return eps


_NOISE_TITLES = ("new tab", "downloads", "home", "untitled", "sign in", "loading")


def _informative(t: str) -> bool:
    """Titles that say what you were doing — not shells, blank tabs or file URLs."""
    low = t.lower().replace(" - google chrome", "").strip()
    return (len(low) > 8 and not t.startswith(("π - ", "t ", "file://", "/", "~"))
            and not low.startswith(_NOISE_TITLES))


def _evidence_project(s, e, prompts, commits):
    """A browsing/reading stretch is project work when what you sent and shipped
    during it points to one project: commits weigh 3, prompts 1. Needs >= 3
    points and >= 60% of them."""
    w = Counter()
    for p in prompts:
        if s <= p["at"] < e and p["project"] != "Unassigned":
            w[p["project"]] += 1
    for c in commits:
        if s <= c["at"] < e and c["project"] and c["project"] != "Unassigned":
            w[c["project"]] += 3
    if not w:
        return None
    top, n = w.most_common(1)[0]
    return top if n >= 3 and n / sum(w.values()) >= 0.6 else None


def _describe(lab, s, e, inside, agent, prompts, commits):
    context = None
    if projects.is_category(lab):
        named = _evidence_project(s, e, prompts, commits)
        if named:
            context, lab = lab, named   # e.g. 网页浏览 → ai-work-journal (via 网页浏览)
    is_cat = projects.is_category(lab)
    by, act, titles = Counter(), Counter(), Counter()
    for b in inside:
        d = _ov(b["start"], b["end"], s, e)
        by[b["project"]] += d
        if b["project"] == lab or b["project"] == context:
            act[b["activity"]] += d
        for t in b.get("titles", [])[:2]:
            if _informative(t):
                titles[t] += d
    human = sum(by.values())
    switches = sum(1 for a, b in zip(inside, inside[1:]) if a["project"] != b["project"])
    sessions = []
    for a in agent:
        ex = sum(_ov(x0, x1, s, e) for x0, x1 in a["segments"])
        if ex >= 30:
            sessions.append({"id": a["id"], "agent": a["agent"], "project": a["project"],
                             "exec_sec": round(ex)})
    sessions.sort(key=lambda x: -x["exec_sec"])
    mine = lambda p: is_cat or (p or "").lower() == lab.lower()  # noqa: E731
    # half-open windows: an instant on a boundary belongs to the later episode
    ps = [p for p in prompts if s <= p["at"] < e and mine(p["project"])]
    cs = [c for c in commits if s <= c["at"] < e + BIN and mine(c["project"])]
    top_act = act.most_common(1)[0][0] if act else ""
    if is_cat:
        summary = titles.most_common(1)[0][0] if titles else ""
    elif cs:
        summary = cs[0]["subject"]
    elif top_act in ("reading", "browser") and titles:
        summary = titles.most_common(1)[0][0]
    elif ps:
        summary = ps[0]["text"]
    else:
        summary = titles.most_common(1)[0][0] if titles else ""
    return {
        "start": s, "end": e, "label": lab, "is_category": is_cat, "context": context,
        "activity": "" if is_cat else ACTIVITY_NAMES.get(top_act, top_act),
        "summary": summary[:120],
        "human_sec": round(human),
        "share": round((by[lab] + (by[context] if context else 0)) / human, 2) if human else 0,
        "mix": [{"label": k, "sec": round(v)} for k, v in by.most_common(6)],
        "switches": switches,
        "sessions": sessions[:10], "n_sessions": len(sessions),
        "agents_by_project": dict(Counter(x["project"] for x in sessions)),
        "prompts": [{"at": p["at"], "text": p["text"][:300], "session_id": p["session_id"]}
                    for p in ps[:8]],
        "n_prompts": len(ps),
        "commits": cs,
        "titles": [t for t, _ in titles.most_common(6)],
    }
