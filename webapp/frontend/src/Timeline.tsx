import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { fmtDur, fmtHM, isCategory, projectColor } from "./api";
import type { AgentLane, Episode, Interval, Timeline } from "./api";
import { durS, endM, hm, hms, laneM, placeOf, startM, t2m, type Visit } from "./visits";

export { t2m, hm, laneM, startM, endM };

/**
 * The day on one timeline, with semantic zoom.
 *
 *   minimap   whole day, activity density + draggable viewport
 *   chapters  work episodes ("RSI · 阅读"), click to zoom in
 *   lanes     one per project: your foreground windows on top, the project's
 *             agents (execution, prompts, commits) underneath; browsing/reading
 *             that belongs to no project shares one "其他活动" lane
 *
 * Windows too narrow to draw on their own (and closer than MERGE_PX) merge
 * into one tinted stretch with the real foreground time drawn inside; zooming
 * in splits them until every visit is its own titled block. Drag pans, ⌘/Ctrl+wheel (or pinch) zooms,
 * double-click zooms in, ←/→ +/− 0 on the keyboard.
 */

export type Selection =
  | { kind: "episode"; key: string }
  | { kind: "unknown"; interval: Interval }
  | { kind: "session"; session: AgentLane };

export const episodeKey = (e: Episode) => `${e.start}|${e.label}`;
export function episodeTitle(e: Episode): string {
  return e.is_category || !e.activity ? e.label : `${e.label} · ${e.activity}`;
}

const LABEL_W = 168;
const HUMAN_H = 54;
const AGENT_H = 20;
const OV_ROW = 9;   // spacing of the overview's thin agent lines
const LANE_GAP = 10;
const BUCKET_PX = 3;     // overview lane column width
const OV_AGENT_ROWS = 4; // agent lines shown in the overview; the rest fold into one
const MIN_SPAN = 3;     // minutes: narrowest view the minimap frame can be resized to
const MERGE_PX = 6;     // gaps narrower than this on screen…
const SOLO_PX = 14;     // …merge visits, unless both are wide enough to stand alone
const OTHER = "__other";

interface Lane {
  key: string;            // project, or OTHER
  label: string;
  humanMin: number;
  sessions: AgentLane[];
  exec: [number, number, number][];   // start, end, concurrency
  execMin: number;
}

interface Stretch { s: number; e: number; visits: Visit[]; active: number }

function sweep(segs: [number, number][]): [number, number, number][] {
  const pts: [number, number][] = [];
  for (const [a, b] of segs) pts.push([a, 1], [Math.max(b, a + 0.3), -1]);
  pts.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
  const out: [number, number, number][] = [];
  let c = 0, prev = 0;
  for (const [t, d] of pts) {
    if (c > 0 && t > prev) {
      const last = out[out.length - 1];
      if (last && last[1] === prev && last[2] === c) last[1] = t;
      else out.push([prev, t, c]);
    }
    c += d; prev = t;
  }
  return out;
}

const MINOR_MIN = 3;   // projects you touched < 3 min with no agent fold into 其他活动

/** Which lane a visit belongs to: its project, or the shared 其他活动 lane. */
export function laneKeyFor(project: string, minor: Set<string>): string {
  return !project || isCategory(project) || minor.has(project) ? OTHER : project;
}

function minorProjects(tl: Timeline, visits: Visit[]): Set<string> {
  const t = new Map<string, number>();
  for (const v of visits) t.set(v.project, (t.get(v.project) ?? 0) + (v.e - v.s));
  const withAgent = new Set(tl.agent.map((a) => a.project || "Unassigned"));
  return new Set([...t.entries()].filter(([p, m]) => m < MINOR_MIN && !withAgent.has(p)).map(([p]) => p));
}

function buildLanes(tl: Timeline, visits: Visit[], minor: Set<string>): Lane[] {
  const day = tl.day_key;
  const human = new Map<string, number>();
  for (const v of visits) {
    const k = laneKeyFor(v.project, minor);
    human.set(k, (human.get(k) ?? 0) + (v.e - v.s));
  }
  const agents = new Map<string, AgentLane[]>();
  for (const a of tl.agent) {
    const k = a.project || "Unassigned";
    agents.set(k, [...(agents.get(k) ?? []), a]);
  }
  const keys = new Set([...human.keys(), ...agents.keys()]);
  const lanes: Lane[] = [...keys].map((k) => {
    const sessions = agents.get(k) ?? [];
    const exec = sweep(sessions.flatMap((s) => s.segments.map((g) =>
      [laneM(g.start, day, "start"), laneM(g.end, day, "end")] as [number, number])));
    return {
      key: k, label: k === OTHER ? "其他活动" : k === "Unassigned" ? "未归属" : k,
      humanMin: human.get(k) ?? 0, sessions, exec,
      execMin: exec.reduce((s, [a, b]) => s + (b - a), 0),
    };
  });
  const rank = (l: Lane) => (l.key === OTHER ? -2 : l.key === "Unassigned" ? -1 : 0);
  return lanes.sort((x, y) => rank(y) - rank(x) || (y.humanMin * 3 + y.execMin) - (x.humanMin * 3 + x.execMin));
}

interface Props {
  tl: Timeline;
  visits: Visit[];
  view: [number, number];
  onView: (v: [number, number]) => void;
  extent: [number, number];     // everything that happened (incl. overnight agents)
  workday: [number, number];    // your observed window span
  selection: Selection | null;
  onSelect: (s: Selection | null) => void;
  isToday: boolean;
  hoverVisit: number | null;
  onHoverVisit: (i: number | null) => void;
  onPickVisit: (i: number) => void;
}

export function ActivityTimeline({ tl, visits, view, onView, extent, workday, selection, onSelect, isToday,
  hoverVisit, onHoverVisit, onPickVisit }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(1000);
  const [cursor, setCursor] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [showLinks, setShowLinks] = useState(true);
  // project lanes are the detail view under the overview lane; remember the choice per browser
  const [detail, setDetail] = useState<boolean>(() => {
    try { return localStorage.getItem("tl.detail") === "1"; } catch { return false; }
  });
  const toggleDetail = () => setDetail((d) => {
    try { localStorage.setItem("tl.detail", d ? "0" : "1"); } catch { /* private mode */ }
    return !d;
  });
  const drag = useRef<{ x0: number; v: [number, number]; moved: boolean } | null>(null);
  // minimap viewport: drag the body to pan, drag either edge to resize (= zoom)
  const miniDrag = useRef<{ x0: number; v: [number, number]; mode: "move" | "l" | "r"; scale: number } | null>(null);
  const [resizing, setResizing] = useState(false);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const day = tl.day_key;
  const [a, b] = view;
  const W = Math.max(200, width - LABEL_W);
  const ppm = W / (b - a);
  const x = (m: number) => (Math.max(a, Math.min(b, m)) - a) * ppm;
  const w = (s: number, e: number) => Math.max(0, x(e) - x(s));
  const inView = (s: number, e: number) => e > a && s < b;

  const minor = useMemo(() => minorProjects(tl, visits), [tl, visits]);
  const lanes = useMemo(() => buildLanes(tl, visits, minor), [tl, visits, minor]);
  const laneIndex = useMemo(() => new Map(lanes.map((l, i) => [l.key, i])), [lanes]);
  const laneKeyOf = (v: Visit) => laneKeyFor(v.project, minor);

  // per-lane stretches at the current zoom
  const stretches = useMemo(() => {
    const by = new Map<string, Stretch[]>();
    for (const v of visits) {
      if (!inView(v.s, v.e)) continue;
      const k = laneKeyOf(v);
      const arr = by.get(k) ?? [];
      const last = arr[arr.length - 1];
      const lastV = last?.visits[last.visits.length - 1];
      // only slivers merge with slivers: a visit wide enough to draw always stands alone
      const narrow = !!lastV && (lastV.e - lastV.s) * ppm < SOLO_PX && (v.e - v.s) * ppm < SOLO_PX;
      if (last && (v.s - last.e) * ppm < MERGE_PX && narrow) {
        last.e = Math.max(last.e, v.e); last.visits.push(v); last.active += v.e - v.s;
      } else arr.push({ s: v.s, e: v.e, visits: [v], active: v.e - v.s });
      by.set(k, arr);
    }
    return by;
  }, [visits, a, b, ppm]); // eslint-disable-line

  // vertical layout
  const laneLayout = useMemo(() => {
    let y = 0;
    return lanes.map((l) => {
      const hasHuman = l.humanMin > 0, hasAgent = l.sessions.length > 0;
      const top = y;
      const hy = top, ay = top + (hasHuman ? HUMAN_H + 3 : 10);
      const sessRows = expanded.has(l.key) ? l.sessions.length : 0;
      const h = Math.max(58, (hasHuman ? HUMAN_H + 3 : 0) + (hasAgent ? AGENT_H : 0)) + sessRows * 20;
      y += h + LANE_GAP;
      return { top, hy, ay, h, hasHuman, hasAgent };
    });
  }, [lanes, expanded]);
  const lanesH = laneLayout.length ? laneLayout[laneLayout.length - 1].top + laneLayout[laneLayout.length - 1].h : 0;

  // ---- overview lane: the whole day on one track ----
  // Your time: slice the view into BUCKET_PX-wide columns, give each column to the
  // project holding most of its foreground time, then join equal neighbours — wide
  // calm blocks when zoomed out, single windows when zoomed in. Agents: one thin
  // stacked line per project with an agent executing, inside the same lane.
  const overview = useMemo(() => {
    const n = Math.max(1, Math.floor(W / BUCKET_PX)), bw = (b - a) / n;
    const cols: Map<string, number>[] = Array.from({ length: n }, () => new Map());
    for (const v of visits) {
      if (v.e <= a || v.s >= b) continue;
      const k = laneKeyOf(v);
      const i0 = Math.max(0, Math.floor((v.s - a) / bw)), i1 = Math.min(n - 1, Math.floor((v.e - a) / bw));
      for (let i = i0; i <= i1; i++) {
        const ov = Math.min(v.e, a + (i + 1) * bw) - Math.max(v.s, a + i * bw);
        if (ov > 0) cols[i].set(k, (cols[i].get(k) ?? 0) + ov);
      }
    }
    const runs: { key: string; s: number; e: number; active: number; lead: Visit | null; n: number }[] = [];
    cols.forEach((col, i) => {
      let best = "", bv = 0, tot = 0;
      for (const [k, v] of col) { tot += v; if (v > bv) { bv = v; best = k; } }
      if (tot < bw * 0.2) return;                       // mostly away / no window
      const last = runs[runs.length - 1];
      if (last && last.key === best && Math.abs(last.e - (a + i * bw)) < bw / 2) { last.e = a + (i + 1) * bw; last.active += tot; }
      else runs.push({ key: best, s: a + i * bw, e: a + (i + 1) * bw, active: tot, lead: null, n: 0 });
    });
    for (const r of runs) {
      let lead: Visit | null = null, ld = 0, cnt = 0;
      for (const v of visits) {
        if (v.e <= r.s || v.s >= r.e || laneKeyOf(v) !== r.key) continue;
        cnt++;
        const d = Math.min(v.e, r.e) - Math.max(v.s, r.s);
        if (d > ld) { ld = d; lead = v; }
      }
      r.lead = lead; r.n = cnt;
      if (lead && cnt === 1) { r.s = Math.max(r.s, lead.s); r.e = Math.min(r.e, lead.e); }   // a lone window: its true edges
    }
    const agentRows = lanes.filter((l) => l.exec.some(([s0, e0]) => e0 > a && s0 < b)).map((l) => l.key);
    const shown = agentRows.slice(0, OV_AGENT_ROWS);
    const extra = agentRows.slice(OV_AGENT_ROWS);
    return { runs, agentRows: shown, extra };
  }, [visits, lanes, a, b, W]); // eslint-disable-line
  const ovH = HUMAN_H + 7 + Math.max(1, overview.agentRows.length + (overview.extra.length ? 1 : 0)) * OV_ROW + 6;

  // ---- view helpers ----
  const clamp = ([s, e]: [number, number]): [number, number] => {
    const sp = Math.min(Math.max(e - s, 3), 1440);
    const s2 = Math.min(Math.max(s, 0), 1440 - sp);
    return [s2, s2 + sp];
  };
  const toMin = (clientX: number) => {
    const r = wrapRef.current!.getBoundingClientRect();
    return a + (clientX - r.left - LABEL_W) / ppm;
  };
  const zoomAt = (f: number, k: number) => onView(clamp([f - (f - a) * k, f + (b - f) * k]));
  const setSpan = (m: number) => { const c = cursor ?? (a + b) / 2; onView(clamp([c - m / 2, c + m / 2])); };

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        zoomAt(toMin(e.clientX), e.deltaY < 0 ? 0.8 : 1.25);
      } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        e.preventDefault();
        const d = ((e.shiftKey ? e.deltaY : e.deltaX) / W) * (b - a);
        onView(clamp([a + d, b + d]));
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [a, b, W]); // eslint-disable-line

  const onKey = (e: React.KeyboardEvent) => {
    const sp = b - a;
    if (e.key === "ArrowLeft") onView(clamp([a - sp * 0.2, b - sp * 0.2]));
    else if (e.key === "ArrowRight") onView(clamp([a + sp * 0.2, b + sp * 0.2]));
    else if (e.key === "+" || e.key === "=") zoomAt(cursor ?? (a + b) / 2, 0.66);
    else if (e.key === "-") zoomAt(cursor ?? (a + b) / 2, 1.5);
    else if (e.key === "0") onView(workday);
    else return;
    e.preventDefault();
  };

  // ---- readout: where you were at the cursor second ----
  const readout = useMemo(() => {
    if (cursor == null) return null;
    let cur: Visit | undefined, idx = visits.length;
    for (let i = 0; i < visits.length; i++) {
      if (visits[i].s <= cursor && visits[i].e > cursor) { cur = visits[i]; idx = i; break; }
      if (visits[i].s > cursor) { idx = i; break; }
    }
    let prev: Visit | undefined;
    for (let i = idx - 1; i >= 0; i--) {
      if (!cur || laneKeyOf(visits[i]) !== laneKeyOf(cur) || visits[i].title !== cur.title) { prev = visits[i]; break; }
    }
    const running = tl.agent.filter((s) => s.segments.some((g) =>
      laneM(g.start, day, "start") <= cursor && laneM(g.end, day, "end") >= cursor));
    const away = tl.away.some((iv) => startM(iv.start, day) <= cursor && endM(iv.end, day) > cursor);
    return { cur, prev, running, away };
  }, [cursor, visits, tl, day]); // eslint-disable-line

  // ---- ticks ----
  const span = b - a;
  const step = span <= 12 ? 1 : span <= 30 ? 5 : span <= 75 ? 10 : span <= 150 ? 15 : span <= 360 ? 30 : span <= 720 ? 60 : 120;
  const ticks: number[] = [];
  for (let t = Math.ceil(a / step) * step; t <= b + 1e-6; t += step) ticks.push(t);
  const nowM = isToday ? new Date().getHours() * 60 + new Date().getMinutes() : -1;

  // ---- minimap ----
  const [m0, m1] = [Math.min(extent[0], a), Math.max(extent[1], b)];
  const mx = (m: number) => ((m - m0) / (m1 - m0)) * W;
  const execUnion = useMemo(() => sweep(lanes.flatMap((l) => l.exec.map(([s, e]) => [s, e] as [number, number])))
    .reduce<[number, number][]>((acc, [s, e]) => {
      const last = acc[acc.length - 1];
      if (last && s <= last[1]) last[1] = Math.max(last[1], e); else acc.push([s, e]);
      return acc;
    }, []), [lanes]);

  const selEp = selection?.kind === "episode" ? selection.key : "";
  const selSession = selection?.kind === "session" ? selection.session.id : -1;
  const presets: [number, string][] = [[10, "10 分"], [30, "30 分"], [120, "2 时"]];
  const drawLinks = showLinks && ppm >= 4;
  const ordered = useMemo(() => visits.filter((v) => inView(v.s, v.e)), [visits, a, b]); // eslint-disable-line

  return (
    <div className="tl2" tabIndex={0} onKeyDown={onKey}>
      {/* toolbar */}
      <div className="tl2-bar">
        <div className="seg">
          <button className={Math.abs(a - extent[0]) < 1 && Math.abs(b - extent[1]) < 1 ? "on" : ""} onClick={() => onView(extent)}>全天</button>
          {(workday[0] !== extent[0] || workday[1] !== extent[1]) && (
            <button className={Math.abs(a - workday[0]) < 1 && Math.abs(b - workday[1]) < 1 ? "on" : ""} onClick={() => onView(workday)}>工作时段</button>
          )}
          {presets.map(([m, l]) => (
            <button key={m} className={Math.abs(span - m) < 0.5 ? "on" : ""} onClick={() => setSpan(m)}>{l}</button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <button className="icon-btn" title="往前 (←)" onClick={() => onView(clamp([a - span * 0.3, b - span * 0.3]))}>‹</button>
          <span className="w-[100px] text-center text-[12.5px] font-medium tabular-nums text-neutral-700">{hm(a)}–{hm(b)}</span>
          <button className="icon-btn" title="往后 (→)" onClick={() => onView(clamp([a + span * 0.3, b + span * 0.3]))}>›</button>
          <button className="icon-btn ml-1" title="缩小 (−)" onClick={() => zoomAt((a + b) / 2, 1.5)}>−</button>
          <button className="icon-btn" title="放大 (+)" onClick={() => zoomAt((a + b) / 2, 0.66)}>+</button>
        </div>
        <label className="tl-check ml-auto text-[12.5px] text-neutral-500">
          <input type="checkbox" checked={showLinks} onChange={(e) => setShowLinks(e.target.checked)} />切换连线
        </label>
        <span className="hidden text-[11.5px] text-neutral-400 2xl:inline">拖拽平移 · ⌘/Ctrl+滚轮缩放 · 双击放大 · ←/→/+/−/0</span>
      </div>

      {/* readout */}
      <div className="tl2-readout">
        {readout ? (<>
          <span className="font-semibold tabular-nums text-neutral-900">{hms(cursor!)}</span>
          {readout.cur ? (<>
            <span className="tl2-chip" style={{ background: isCategory(readout.cur.project) ? "#71717a" : projectColor(readout.cur.project) }}>
              {readout.cur.project}
            </span>
            <span className="shrink-0 text-neutral-500">{readout.cur.appLabel}</span>
            <span className="min-w-0 truncate text-neutral-800">{placeOf(readout.cur)}</span>
            <span className="shrink-0 text-neutral-400">已停留 {durS(cursor! - readout.cur.s)} / {durS(readout.cur.e - readout.cur.s)}</span>
            {readout.cur.reason && <span className="shrink-0 rounded bg-white px-1.5 text-[11.5px] text-neutral-400 ring-1 ring-neutral-200" title="为什么归到这里">{readout.cur.reason}</span>}
          </>) : <span className="text-neutral-400">{readout.away ? "离开电脑" : "无前台窗口"}</span>}
          <span className="ml-auto flex shrink-0 items-center gap-3">
            {readout.prev && (
              <span className="max-w-[300px] truncate text-neutral-400">
                ← {hms(readout.prev.e)} 从 {readout.prev.appLabel} · {placeOf(readout.prev).slice(0, 26)}
              </span>
            )}
            {readout.running.length > 0 && (
              <span className="rounded-md bg-neutral-100 px-1.5 text-neutral-600">
                {readout.running.length} agent 执行中 · {[...new Set(readout.running.map((s) => s.project))].join(" / ")}
              </span>
            )}
          </span>
        </>) : (
          <span className="text-neutral-400">把鼠标放到时间轴上,读出那一秒你在哪个窗口、停留多久、从哪跳来、哪些 agent 在跑。</span>
        )}
      </div>

      {!tl.has_observation && (
        <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[12px] text-amber-700">
          该日没有前台窗口观察(ActivityWatch),只显示 Agent 执行与证据。
        </div>
      )}

      <div ref={wrapRef} className="tl2-body"
        onPointerMove={(e) => {
          const r = wrapRef.current!.getBoundingClientRect();
          const inTrack = e.clientX - r.left >= LABEL_W;
          setCursor(inTrack ? toMin(e.clientX) : null);
          if (miniDrag.current) {
            const { x0, v, mode, scale } = miniDrag.current;   // scale frozen at drag start
            const d = (e.clientX - x0) * scale;
            if (mode === "move") onView(clamp([v[0] + d, v[1] + d]));
            else if (mode === "l") onView([Math.max(0, Math.min(v[0] + d, v[1] - MIN_SPAN)), v[1]]);
            else onView([v[0], Math.min(1440, Math.max(v[1] + d, v[0] + MIN_SPAN))]);
            return;
          }
          if (!drag.current) return;
          const dx = e.clientX - drag.current.x0;
          if (!drag.current.moved && Math.abs(dx) < 4) return;
          drag.current.moved = true;
          (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
          const d = -dx / ppm;
          onView(clamp([drag.current.v[0] + d, drag.current.v[1] + d]));
        }}
        onPointerUp={() => { miniDrag.current = null; setResizing(false); setTimeout(() => { drag.current = null; }); }}
        onPointerCancel={() => { drag.current = null; miniDrag.current = null; setResizing(false); }}
        onPointerLeave={() => setCursor(null)}>

        {/* minimap */}
        <div className="tl-row tl2-mini-row">
          <div className="tl-lab"><small>全天</small></div>
          <div className="tl-track tl2-mini"
            onPointerDown={(e) => {
              e.stopPropagation();
              const scale = (m1 - m0) / W;
              const edge = (e.target as HTMLElement).dataset.edge as "l" | "r" | undefined;
              (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
              if (edge) {
                miniDrag.current = { x0: e.clientX, v: [a, b], mode: edge, scale };
                setResizing(true);
                return;
              }
              const r = e.currentTarget.getBoundingClientRect();
              const m = m0 + (e.clientX - r.left) * scale;
              const v: [number, number] = m < a || m > b ? clamp([m - span / 2, m + span / 2]) : [a, b];
              if (v[0] !== a) onView(v);
              miniDrag.current = { x0: e.clientX, v, mode: "move", scale };
            }}>
            {visits.map((v) => (
              <div key={v.i} className="tl2-mini-v" style={{ left: mx(v.s), width: Math.max(0.6, mx(v.e) - mx(v.s)),
                background: isCategory(v.project) ? "#c4c7ce" : projectColor(v.project) }} />
            ))}
            {execUnion.map(([s, e], i) => (
              <div key={i} className="tl2-mini-a" style={{ left: mx(s), width: Math.max(0.6, mx(e) - mx(s)) }} />
            ))}
            <div className={`tl2-mini-view ${resizing ? "resizing" : ""}`} style={{ left: mx(a), width: Math.max(10, mx(b) - mx(a)) }}>
              <span className="tl2-mini-h l" data-edge="l" title="拖动调整开始时间" />
              <span className="tl2-mini-h r" data-edge="r" title="拖动调整结束时间" />
            </div>
          </div>
        </div>

        {/* axis */}
        <div className="tl-row tl2-axis">
          <div className="tl-lab" />
          <div className="tl-track">
            {ticks.map((t) => <div key={t} className="tl-tick" style={{ left: x(t) }}>{hm(t)}</div>)}
          </div>
        </div>

        <div className="relative"
          onPointerDown={(e) => {
            if (e.button !== 0 || e.clientX - wrapRef.current!.getBoundingClientRect().left < LABEL_W) return;
            drag.current = { x0: e.clientX, v: [a, b], moved: false };
          }}
          onDoubleClick={(e) => { if (e.clientX - wrapRef.current!.getBoundingClientRect().left >= LABEL_W) zoomAt(toMin(e.clientX), 0.5); }}>
          <div className="tl-grid" style={{ left: LABEL_W, top: 0 }}>
            {ticks.map((t) => <div key={t} className="tl-gl" style={{ left: x(t) }} />)}
          </div>

          {/* chapters: episodes + unknown gaps */}
          {tl.has_observation && (
            <div className="tl-row tl2-chapters">
              <div className="tl-lab"><b>工作段</b></div>
              <div className="tl-track">
                {tl.unknown.filter((u) => endM(u.end, day) - startM(u.start, day) >= 2).map((u, i) => {
                  const s = startM(u.start, day), e = endM(u.end, day);
                  return inView(s, e) && (
                    <div key={`u${i}`} className="tl2-unk" style={{ left: x(s), width: Math.max(3, w(s, e)) }} title={`未知 ${hm(s)}–${hm(e)} · 点击标注`}
                      onClick={(ev) => { ev.stopPropagation(); if (!drag.current?.moved) onSelect({ kind: "unknown", interval: u }); }} />
                  );
                })}
                {tl.episodes.map((ep) => {
                  const s = startM(ep.start, day), e = endM(ep.end, day);
                  if (!inView(s, e)) return null;
                  const ww = w(s, e), k = episodeKey(ep);
                  return (
                    <div key={k} className={`tl2-ep ${ep.is_category ? "cat" : ""} ${selEp === k ? "sel" : ""}`}
                      style={{ left: x(s), width: Math.max(3, ww - 2), ["--c" as string]: ep.is_category ? "#a1a1aa" : projectColor(ep.label) }}
                      title={`${episodeTitle(ep)} ${fmtHM(ep.start)}–${fmtHM(ep.end)}\n${ep.summary}`}
                      onClick={(ev) => { ev.stopPropagation(); if (!drag.current?.moved) onSelect({ kind: "episode", key: k }); }}>
                      {ww > 56 && <span>{episodeTitle(ep)}{ww > 150 ? ` · ${fmtDur(ep.human_sec)}` : ""}</span>}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* overview: everything on one lane */}
          <div className="tl2-lane tl2-ov" style={{ position: "relative", height: ovH }}>
            <div className="tl2-lab" style={{ height: ovH }}>
              <span className="flex items-center gap-1.5"><b>全部</b></span>
              <small>你 {fmtDur(tl.metrics.human_active_sec)}{tl.agent.length ? ` · agent ${fmtDur(tl.metrics.agent_exec_union_sec)}` : ""}</small>
            </div>
            <div className="tl2-track" style={{ left: LABEL_W, width: W }}>
              <div className="tl2-hbg" style={{ top: 0, height: HUMAN_H }} />
              {tl.away.map((iv, i) => {
                const s0 = startM(iv.start, day), e0 = endM(iv.end, day);
                return inView(s0, e0) && w(s0, e0) > 1.5 && (
                  <div key={i} className="tl2-away" style={{ left: x(s0), width: w(s0, e0), height: HUMAN_H, borderRadius: 6 }} />
                );
              })}
              {overview.runs.map((r, k) => {
                const ww = w(r.s, r.e), other = r.key === OTHER;
                const col = other ? "#c9ccd3" : projectColor(r.key);
                const one = r.n === 1 && r.lead;
                const name = other ? (r.lead?.project ?? "其他活动") : r.key;
                const hot = r.lead != null && r.lead.i === hoverVisit;
                return (
                  <div key={k} className={`tl2-ovr ${other ? "cat" : ""} ${hot ? "hot" : ""}`}
                    style={{ left: x(r.s), width: Math.max(1.5, ww - (ww > 4 ? 1 : 0)), height: HUMAN_H, background: col }}
                    onMouseEnter={() => r.lead && onHoverVisit(r.lead.i)} onMouseLeave={() => onHoverVisit(null)}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      if (drag.current?.moved) return;
                      if (one) onPickVisit(r.lead!.i);
                      else { const pad = (r.e - r.s) * 0.08 + 0.5; onView(clamp([r.s - pad, r.e + pad])); }
                    }}>
                    {ww >= 40 && (
                      <span className="lbl">
                        <span className="t">{one ? placeOf(r.lead!) : name}</span>
                        {ww >= 120 && <span className="m">{one ? `${r.lead!.appLabel} · ${durS(r.lead!.e - r.lead!.s)}` : `${durS(r.active)}${r.lead ? ` · ${placeOf(r.lead)}` : ""}`}</span>}
                      </span>
                    )}
                  </div>
                );
              })}
              {/* agents, one thin line per project, stacked inside the lane */}
              {[...overview.agentRows, ...(overview.extra.length ? ["__more"] : [])].map((key, ri) => {
                const ls = key === "__more" ? lanes.filter((l) => overview.extra.includes(l.key)) : lanes.filter((l) => l.key === key);
                const col = key === "__more" ? "#a1a1aa" : projectColor(key);
                return ls.flatMap((l) => l.exec.filter(([s0, e0]) => e0 > a && s0 < b).map(([s0, e0, c], k) => (
                  <div key={`${key}${l.key}${k}`} className="tl2-ova"
                    style={{ left: x(s0), width: Math.max(1.5, w(s0, e0)), top: HUMAN_H + 8 + ri * OV_ROW, background: col, opacity: Math.min(1, 0.55 + 0.15 * c) }}
                    title={`${l.label} agent ${hm(s0)}–${hm(e0)}${c > 1 ? ` · ${c} 个并行` : ""}`} />
                )));
              })}
              {tl.evidence.filter((ev) => ev.kind === "agent_user").map((p) => {
                const m = t2m(p.at);
                return m >= a && m <= b && (
                  <div key={p.id} className="tl2-pr" style={{ left: x(m), top: HUMAN_H + 1, borderBottomColor: projectColor(p.project ?? "Unassigned") }}
                    title={`${p.at.slice(11, 19)} 你 → ${p.project ?? ""} agent\n${p.label}`} />
                );
              })}
              {tl.evidence.filter((ev) => ev.kind === "commit").map((c) => {
                const m = t2m(c.at);
                return m >= a && m <= b && (
                  <div key={c.id} className="tl2-cm" style={{ left: x(m), top: HUMAN_H - 4 }} title={`${c.at.slice(11, 19)} 提交 · ${c.repo}\n${c.label}`} />
                );
              })}
            </div>
          </div>

          <div className="tl-row">
            <div className="tl-lab" />
            <button className="tl2-toggle" onClick={(e) => { e.stopPropagation(); toggleDetail(); }}>
              {detail ? "▾ 收起项目泳道" : `▸ 按项目展开 · ${lanes.length} 条泳道`}
              {!detail && overview.agentRows.length > 0 && <span className="text-neutral-400"> · 总览底部细线 = 各项目的 agent 执行</span>}
            </button>
          </div>

          {/* lanes (detail) */}
          {detail && <div className="relative" style={{ height: lanesH }}>
            {tl.away.map((iv, i) => {
              const s = startM(iv.start, day), e = endM(iv.end, day);
              return inView(s, e) && w(s, e) > 1.5 && (
                <div key={i} className="tl2-away" style={{ left: LABEL_W + x(s), width: w(s, e), height: lanesH }} />
              );
            })}

            {lanes.map((l, li) => {
              const L = laneLayout[li], open = expanded.has(l.key);
              // colour resolved at render: project colours load after the first paint
              const color = l.key === OTHER ? "#a1a1aa" : projectColor(l.key);
              const st = stretches.get(l.key) ?? [];
              return (
                <div key={l.key} className="tl2-lane" style={{ top: L.top, height: L.h }}>
                  <button className="tl2-lab" style={{ height: L.h }}
                    onClick={(e) => { e.stopPropagation(); if (!l.sessions.length) return;
                      setExpanded((s) => { const n = new Set(s); if (n.has(l.key)) n.delete(l.key); else n.add(l.key); return n; }); }}>
                    <span className="flex items-center gap-1.5">
                      <i style={{ background: color }} />
                      <b>{l.label}</b>
                      {l.sessions.length > 0 && <span className="tl-caret">{open ? "▾" : "▸"}</span>}
                    </span>
                    <small>
                      {l.humanMin > 0 && <>你 {fmtDur(l.humanMin * 60)}</>}
                      {l.humanMin > 0 && l.sessions.length > 0 && " · "}
                      {l.sessions.length > 0 && <>agent {fmtDur(l.execMin * 60)}</>}
                    </small>
                  </button>
                  <div className="tl2-track" style={{ left: LABEL_W, width: W }}>
                    {L.hasHuman && <div className="tl2-hbg" style={{ top: 0, height: HUMAN_H }} />}
                    {L.hasHuman && st.map((g, k) => {
                      const ww = w(g.s, g.e);
                      const single = g.visits.length === 1;
                      const lead = g.visits.reduce((p, q) => (q.e - q.s > p.e - p.s ? q : p));
                      const cat = l.key === OTHER;
                      const col = cat ? "#9ca3af" : color;
                      const hot = g.visits.some((v) => v.i === hoverVisit);
                      const label = cat ? `${lead.project} · ${placeOf(lead)}` : placeOf(lead);
                      return (
                        <div key={k}
                          className={`tl2-st ${single ? "one" : "many"} ${cat ? "cat" : ""} ${hot ? "hot" : ""}`}
                          style={{ left: x(g.s), width: Math.max(2, ww), height: HUMAN_H, ["--c" as string]: col }}
                          onMouseEnter={() => onHoverVisit(lead.i)}
                          onMouseLeave={() => onHoverVisit(null)}
                          onClick={(ev) => {
                            ev.stopPropagation();
                            if (drag.current?.moved) return;
                            if (single) onPickVisit(g.visits[0].i);
                            else { const pad = (g.e - g.s) * 0.08 + 0.5; onView(clamp([g.s - pad, g.e + pad])); }
                          }}>
                          {!single && g.visits.map((v) => (
                            <i key={v.i} style={{ left: x(v.s) - x(g.s), width: Math.max(1, w(v.s, v.e)) }} />
                          ))}
                          {ww >= 44 && (
                            <span className="lbl">
                              <span className="t">{label}</span>
                              {ww >= 110 && <span className="m">{single ? `${lead.appLabel} · ${durS(g.e - g.s)}` : `${durS(g.active)} · ${g.visits.length} 个窗口`}</span>}
                            </span>
                          )}
                        </div>
                      );
                    })}
                    {L.hasAgent && (
                      <div className="tl2-arow" style={{ top: L.ay - L.top, height: AGENT_H }}>
                        {l.exec.map(([s, e, c], k) => inView(s, e) && (
                          <div key={k} className="tl2-ex" style={{ left: x(s), width: Math.max(1.5, w(s, e)), background: color, opacity: Math.min(1, 0.35 + 0.18 * c) }}
                            title={`${l.label} agent ${hm(s)}–${hm(e)} · ${c} 个并行`}>
                            {c > 1 && w(s, e) > 22 && <span>×{c}</span>}
                          </div>
                        ))}
                        {tl.evidence.filter((ev) => ev.kind === "agent_user" && (ev.project ?? "Unassigned") === l.key).map((p) => {
                          const m = t2m(p.at);
                          return m >= a && m <= b && (
                            <div key={p.id} className="tl2-pr" style={{ left: x(m), borderBottomColor: color }} title={`${p.at.slice(11, 19)} 你 → agent\n${p.label}`} />
                          );
                        })}
                        {tl.evidence.filter((ev) => ev.kind === "commit" && (ev.project ?? ev.repo) === l.key).map((c) => {
                          const m = t2m(c.at);
                          return m >= a && m <= b && (
                            <div key={c.id} className="tl2-cm" style={{ left: x(m) }} title={`${c.at.slice(11, 19)} 提交 · ${c.repo}\n${c.label}`} />
                          );
                        })}
                      </div>
                    )}
                    {open && l.sessions.map((s, si) => {
                      const s0 = laneM(s.start, day, "start"), s1 = laneM(s.end, day, "end");
                      return (
                        <div key={s.id} className="tl2-srow" style={{ top: L.ay - L.top + AGENT_H + si * 20 }}
                          onClick={(ev) => { ev.stopPropagation(); if (!drag.current?.moved) onSelect({ kind: "session", session: s }); }}>
                          {inView(s0, s1) && <div className="tl-life" style={{ left: x(s0), width: w(s0, s1), background: color }} />}
                          {s.segments.map((g, k) => {
                            const g0 = laneM(g.start, day, "start"), g1 = laneM(g.end, day, "end");
                            return inView(g0, g1) && (
                              <div key={k} className={`tl2-sx ${selSession === s.id ? "sel" : ""}`}
                                style={{ left: x(g0), width: Math.max(1.5, w(g0, g1)), background: color }}
                                title={`${s.agent}${s.subagent ? " 子代理" : ""} #${s.id} · ${hm(g0)}–${hm(g1)}`} />
                            );
                          })}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}

            {drawLinks && (
              <svg className="tl2-links" style={{ left: LABEL_W, width: W, height: lanesH }}>
                {ordered.map((v, k) => {
                  if (k === 0) return null;
                  const p = ordered[k - 1];
                  const lp = laneIndex.get(laneKeyOf(p)), lv = laneIndex.get(laneKeyOf(v));
                  if (lp == null || lv == null || lp === lv || v.s - p.e > 1) return null;
                  const y1 = laneLayout[lp].hy + HUMAN_H / 2, y2 = laneLayout[lv].hy + HUMAN_H / 2;
                  const x1 = x(p.e), x2 = x(v.s);
                  const hot = hoverVisit === v.i || hoverVisit === p.i;
                  return <path key={k} className={hot ? "hot" : ""} d={`M${x1},${y1} C${x1 + 12},${y1} ${x2 - 12},${y2} ${x2},${y2}`} />;
                })}
              </svg>
            )}

          </div>}

          {/* now + cursor span chapters, overview and lanes alike */}
          {nowM >= a && nowM <= b && <div className="tl2-now" style={{ left: LABEL_W + x(nowM), top: 0, bottom: 0 }} />}
          {cursor != null && cursor >= a && cursor <= b && (
            <div className="tl2-cursor" style={{ left: LABEL_W + x(cursor), top: -40, bottom: 0 }} />
          )}
        </div>
      </div>

      <div className="tl-legend">
        <span>总览:色块 = 那段时间你主要在哪个项目(放大后拆成单个窗口);下方细线 = 正在执行的 agent,每个项目一条</span>
        <span>▲ 你发的 prompt · ◆ 提交</span>
        <span><i className="away" />离开</span>
      </div>
    </div>
  );
}
