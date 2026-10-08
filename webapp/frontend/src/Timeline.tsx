import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { fmtDur, fmtHM, projectColor, CATEGORY_COLOR, AGENT_COLORS } from "./api";
import type { AgentLane, Block, Interval, Timeline } from "./api";

/**
 * Core activity timeline.
 *
 * - One row per agent session: hairline = lifecycle (first..last event),
 *   solid bars = execution clusters (backend segments), blank = waiting.
 * - Human lane merges same-project blocks when zoomed out; at high zoom the
 *   real window blocks render individually.
 * - Wheel zooms toward the cursor; drag pans; presets pick ranges.
 */

export const CATEGORY_NAMES = [
  "终端·其他", "网页浏览", "文献阅读", "文本编辑", "文件管理", "个人事务",
  "系统", "PyCharm", "通信", "阅读", "其他",
];

export function isCat(p: string) {
  return CATEGORY_NAMES.includes(p);
}

export interface Segment {
  project: string;
  start: string;
  end: string;
  blocks: Block[];
  switches: number;
}

export type Selection =
  | { kind: "segment"; segment: Segment }
  | { kind: "unknown"; interval: Interval }
  | { kind: "session"; session: AgentLane };

const LABEL_W = 96;
const AXIS_H = 26;
const HUMAN_H = 52;
const STATUS_H = 14;
const EV_H = 18;
const ROW_H = 26;
const ROW_GAP = 7;
const LOD_BLOCK = 1.2; // px per minute; above this, draw real blocks
const MERGE_GAP_MIN = 8;

export function t2m(iso: string): number {
  return +iso.slice(11, 13) * 60 + +iso.slice(14, 16) + +iso.slice(17, 19) / 60;
}

function durSec(start: string, end: string): number {
  return Math.max(0, (t2m(end) - t2m(start)) * 60);
}

function agentColor(agent: string): string {
  return AGENT_COLORS[agent] ?? projectColor(agent);
}

function fmtRange(a: string, b: string) {
  return `${fmtHM(a)}–${fmtHM(b)}`;
}

/** Merge consecutive blocks of the same project (gap <= MERGE_GAP_MIN). */
export function mergeSegments(blocks: Block[]): Segment[] {
  const segs: Segment[] = [];
  for (const b of blocks) {
    const last = segs[segs.length - 1];
    if (last && last.project === b.project &&
        (t2m(b.start) - t2m(last.end)) <= MERGE_GAP_MIN) {
      last.end = b.end > last.end ? b.end : last.end;
      last.blocks.push(b);
      last.switches++;
    } else {
      segs.push({ project: b.project, start: b.start, end: b.end, blocks: [b], switches: 1 });
    }
  }
  return segs;
}

interface Props {
  tl: Timeline;
  selection: Selection | null;
  onSelect: (s: Selection) => void;
  isToday: boolean;
}

export function ActivityTimeline({ tl, selection, onSelect, isToday }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<SVGSVGElement>(null);
  const [width, setWidth] = useState(1100);
  const [view, setView] = useState<[number, number]>([0, 1440]);
  const [fitOnce, setFitOnce] = useState(false);
  const [hover, setHover] = useState<{ x: number; y: number; title: string; lines: string[] } | null>(null);
  const drag = useRef<{ x: number; v: [number, number] } | null>(null);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const activityBounds = useMemo<[number, number] | null>(() => {
    let lo = Infinity, hi = -Infinity;
    for (const b of tl.blocks) { lo = Math.min(lo, t2m(b.start)); hi = Math.max(hi, t2m(b.end)); }
    for (const a of tl.agent) {
      for (const s of a.segments) { lo = Math.min(lo, t2m(s.start)); hi = Math.max(hi, t2m(s.end)); }
    }
    for (const e of tl.evidence) { lo = Math.min(lo, t2m(e.at)); hi = Math.max(hi, t2m(e.at)); }
    if (tl.observed_span) { lo = Math.min(lo, t2m(tl.observed_span.start)); hi = Math.max(hi, t2m(tl.observed_span.end)); }
    return lo === Infinity ? null : [lo, hi];
  }, [tl]);

  // initial view: the human observation window (the main workday);
  // "聚焦活动" (toolbar) fits ALL activity incl. overnight auto runs.
  useEffect(() => {
    if (width && !fitOnce && (activityBounds || tl.agent.length)) {
      setFitOnce(true);
      let lo = 480, hi = 1200;
      if (tl.observed_span) {
        lo = t2m(tl.observed_span.start);
        hi = t2m(tl.observed_span.end);
      } else if (activityBounds) {
        [lo, hi] = activityBounds;
      }
      const pad = 30;
      setView([Math.max(0, lo - pad), Math.min(1440, hi + pad)]);
    }
  }, [width, activityBounds, tl.agent.length, tl.observed_span, fitOnce]);

  const [v0, v1] = view;
  const span = v1 - v0;
  const timeW = Math.max(200, width - LABEL_W);
  const pxPerMin = timeW / span;
  const X = (m: number) => LABEL_W + (m - v0) * pxPerMin;

  const segments = useMemo(() => mergeSegments(tl.blocks), [tl.blocks]);
  const drawBlocks = pxPerMin >= LOD_BLOCK;
  const humanItems: (Segment | Block)[] = drawBlocks ? tl.blocks : segments;

  // agent rows: pack by lifecycle overlap
  const agentRows = useMemo(() => {
    const rows: AgentLane[][] = [];
    for (const s of [...tl.agent].sort((a, b) => a.start.localeCompare(b.start))) {
      let placed = false;
      for (const row of rows) {
        if (row[row.length - 1].end <= s.start) { row.push(s); placed = true; break; }
      }
      if (!placed) rows.push([s]);
    }
    return rows;
  }, [tl.agent]);

  const agentTop = AXIS_H + HUMAN_H + STATUS_H + EV_H + 6;
  const bodyH = agentTop + agentRows.length * (ROW_H + ROW_GAP) + 4;
  const axisW = LABEL_W + timeW;

  const clampView = (nv: [number, number]): [number, number] => {
    let [a, b] = nv;
    const sp = Math.min(Math.max(b - a, 30), 1440);
    a = Math.min(Math.max(a, 0), 1440 - sp);
    return [a, a + sp];
  };

  const zoom = (factor: number, focusMin: number | null) => {
    setView(([a, b]) => {
      const sp = (b - a) / factor;
      const f = focusMin ?? (a + b) / 2;
      return clampView([f - sp / 2, f + sp / 2]);
    });
  };

  // wheel zoom (non-passive so we can preventDefault)
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const focus = v0 + (e.clientX - rect.left - LABEL_W) / pxPerMin;
      zoom(e.deltaY < 0 ? 1.25 : 0.8, focus);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [v0, pxPerMin]);

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    drag.current = { x: e.clientX, v: view };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (drag.current) {
      const dx = e.clientX - drag.current.x;
      const dmin = dx / pxPerMin;
      setView(clampView([drag.current.v[0] - dmin, drag.current.v[1] - dmin]));
    }
  };
  const onPointerUp = () => { drag.current = null; };

  const setHoverAt = (e: React.MouseEvent, title: string, lines: string[]) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    setHover({ x: e.clientX - rect.left, y: e.clientY - rect.top, title, lines });
  };

  const nowMin = isToday ? new Date().getHours() * 60 + new Date().getMinutes() : -1;
  const showNow = nowMin >= v0 && nowMin <= v1;

  // hour label step: keep >= 54px between labels
  const stepH = Math.max(1, Math.ceil(54 / (pxPerMin * 60)));
  const hours = useMemo(() => {
    const out: number[] = [];
    for (let h = Math.floor(v0 / 60); h <= Math.ceil(v1 / 60); h++) {
      if (h % stepH === 0) out.push(h);
    }
    return out;
  }, [v0, v1, stepH]);

  const kindColor = (k: string) =>
    k === "commit" ? "#3f3f46" : k === "agent_user" ? "#5e6ad2" : "#c7c7cf";

  const selKey = (s: Selection) =>
    s.kind === "session" ? `s${s.session.id}` :
    s.kind === "segment" ? `b${s.segment.start}` : `u${s.interval.start}`;
  const isSel = (s: Selection | null, key: string) => s && selKey(s) === key;

  return (
    <div ref={wrapRef} className="relative select-none">
      {/* toolbar */}
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <div className="flex rounded-md border border-neutral-200 p-0.5">
          {([["fit", "聚焦活动"], ["work", "工作时段"], ["day", "全天"]] as const).map(([k, label]) => (
            <button key={k}
              className="rounded px-2 py-0.5 text-[11.5px] font-medium text-neutral-500 hover:bg-neutral-100 hover:text-neutral-800"
              onClick={() => {
                if (k === "fit") setView(clampView([
                  Math.max(0, (activityBounds?.[0] ?? 480) - 30),
                  Math.min(1440, (activityBounds?.[1] ?? 1200) + 30),
                ]));
                else if (k === "work") setView([8 * 60, 20 * 60]);
                else setView([0, 1440]);
              }}>{label}</button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <button className="icon-btn" title="缩小" onClick={() => zoom(1.6, null)}>−</button>
          <span className="w-16 text-center text-[11px] tabular-nums text-neutral-400">
            {pxPerMin >= 1 ? `${Math.round(60 / pxPerMin)} 秒/px` : `${(1 / pxPerMin).toFixed(1)} 分/px`}
          </span>
          <button className="icon-btn" title="放大" onClick={() => zoom(0.62, null)}>+</button>
        </div>
        <div className="ml-auto flex items-center gap-3 text-[11px] text-neutral-400">
          <span className="flex items-center gap-1"><i className="h-2.5 w-2.5 rounded-[3px]" style={{ background: "#5E6AD2" }} />项目</span>
          <span className="flex items-center gap-1"><i className="h-2.5 w-2.5 rounded-[3px]" style={{ background: CATEGORY_COLOR }} />分类</span>
          <span className="flex items-center gap-1"><i className="h-2 w-5 rounded-sm bg-neutral-200" />离开</span>
          <span className="flex items-center gap-1"><i className="h-2 w-5 rounded-sm" style={{ background: "repeating-linear-gradient(45deg,#fbbf24 0 3px,#fff7ed 3px 6px)" }} />未知</span>
          <span className="flex items-center gap-1"><i className="h-1.5 w-5 rounded-full bg-neutral-300" />会话生命周期</span>
          <span className="flex items-center gap-1"><i className="h-2.5 w-6 rounded-sm bg-[#5E6AD2]" /><i className="h-2.5 w-6 rounded-sm bg-[#E8862E]" />执行</span>
          <span className="hidden items-center gap-1 sm:flex"><i className="text-[10px] text-neutral-500">⎇</i>提交 <i className="text-[10px] ev-user">❯</i>输入</span>
        </div>
      </div>

      {!tl.has_observation && (
        <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[12px] text-amber-700">
          该日无前台窗口观察(ActivityWatch);仅显示 Agent 会话与事件证据。
        </div>
      )}

      {/* axis (fixed) */}
      <svg width={axisW} height={AXIS_H} className="block">
        {hours.map((h) => {
          const m = h * 60;
          return (
            <g key={h}>
              <line x1={X(m)} x2={X(m)} y1={AXIS_H - 8} y2={AXIS_H - 2} stroke="#d4d4d8" strokeWidth={1} />
              <text x={X(m) + 3} y={12} fontSize={10.5} fill="#a1a1aa" className="tabular-nums">
                {String(((h % 24) + 24) % 24).padStart(2, "0")}:00
              </text>
            </g>
          );
        })}
        {showNow && <line x1={X(nowMin)} x2={X(nowMin)} y1={0} y2={AXIS_H} stroke="#f43f5e" strokeWidth={1.5} />}
      </svg>

      {/* body (scrollable if agent rows exceed viewport) */}
      <div className="thin-scroll relative max-h-[430px] overflow-y-auto rounded-lg border border-neutral-100 bg-gradient-to-b from-neutral-50/60 to-white">
        <svg
          ref={bodyRef}
          width={axisW}
          height={Math.max(bodyH, 150)}
          className="block cursor-grab active:cursor-grabbing"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={() => { onPointerUp(); setHover(null); }}
        >
          {/* grid lines */}
          {hours.map((h) => (
            <line key={h} x1={X(h * 60)} x2={X(h * 60)} y1={0} y2={bodyH}
              stroke="#ececee" strokeWidth={h % 6 === 0 ? 1 : 0.6} />
          ))}
          {showNow && <line x1={X(nowMin)} x2={X(nowMin)} y1={0} y2={bodyH} stroke="#f43f5e" strokeOpacity={0.5} strokeWidth={1} />}

          {/* ---------- human lane ---------- */}
          <text x={0} y={AXIS_H + 14} fontSize={10} fill="#a1a1aa">人类前台</text>
          {humanItems.map((it, i) => {
            const project = "project" in it ? it.project : "";
            const isSeg = "blocks" in it;
            const key = isSeg ? `b${it.start}` : `b${it.start}`;
            const m0 = t2m(it.start), m1 = Math.max(t2m(it.end), m0 + 1 / pxPerMin);
            const x0 = X(m0), w = Math.max(X(m1) - X(m0), 1);
            const fill = isCat(project) ? CATEGORY_COLOR : projectColor(project);
            const manual = (isSeg ? it.blocks : [it]).some((b) => b.is_manual_override);
            const dur = durSec(it.start, it.end);
            const lines = [
              `${fmtRange(it.start, it.end)} · ${fmtDur(dur)}`,
              `活动 ${"activity_type" in it ? it.activity_type : it.blocks[0].activity_type}`,
              ...(isSeg ? [`${it.blocks.length} 个窗口事件,${it.switches} 次切换`] : []),
              (isSeg ? it.blocks[0] : (it as Block)).titles[0]
                ? `“${(isSeg ? it.blocks[0] : (it as Block)).titles[0].slice(0, 60)}”` : "",
              `${(isSeg ? it.blocks : [it]).reduce((s, b) => s + b.evidence_count, 0)} 条证据 · 置信 ${Math.round((isSeg ? it.blocks : [it]).reduce((s, b) => s + b.confidence, 0) / (isSeg ? it.blocks.length : 1) * 100)}%${manual ? " · 人工修正" : ""}`,
            ].filter(Boolean);
            return (
              <g key={`${key}${i}`}
                className="cursor-pointer"
                onClick={() => onSelect(isSeg
                  ? { kind: "segment", segment: it as Segment }
                  : { kind: "segment", segment: { project: it.project, start: it.start, end: it.end, blocks: [it as Block], switches: 1 } })}
                onMouseMove={(e) => setHoverAt(e, project || "人类活动", lines)}
                onMouseLeave={() => setHover(null)}
              >
                <rect x={x0} y={AXIS_H + 18} width={w} height={drawBlocks ? 32 : 34}
                  rx={drawBlocks ? 2 : 5} fill={fill} fillOpacity={manual ? 1 : 0.88}
                  stroke={isSel(selection, key) ? "#18181b" : "none"} strokeWidth={1.6}
                />
                {!drawBlocks && w >= 44 && (
                  <text x={x0 + 6} y={AXIS_H + 18 + 22} fontSize={10.5} fill="#ffffff" fillOpacity={0.95}
                    className="pointer-events-none font-medium">
                    {project} {fmtDur(dur)}
                  </text>
                )}
              </g>
            );
          })}

          {/* away + unknown strip */}
          <text x={0} y={AXIS_H + HUMAN_H + 10} fontSize={9.5} fill="#c0c0c8">离开/未知</text>
          {tl.away.map((iv, i) => (
            <rect key={`a${i}`} x={X(t2m(iv.start))} width={Math.max(X(t2m(iv.end)) - X(t2m(iv.start)), 0.5)}
              y={AXIS_H + HUMAN_H + 14} height={8} rx={2} fill="#e4e4e7"
              onMouseMove={(e) => setHoverAt(e, "离开", [`${fmtRange(iv.start, iv.end)} · ${fmtDur(durSec(iv.start, iv.end))}`, "AFK:键盘/鼠标无输入"])} />
          ))}
          {tl.unknown.map((iv, i) => (
            <rect key={`u${i}`} x={X(t2m(iv.start))} width={Math.max(X(t2m(iv.end)) - X(t2m(iv.start)), 1.5)}
              y={AXIS_H + HUMAN_H + 14} height={8} rx={2} fill="url(#unk)" stroke="#fcd34d" strokeWidth={0.5}
              className="cursor-pointer"
              onClick={() => onSelect({ kind: "unknown", interval: iv })}
              onMouseMove={(e) => setHoverAt(e, "未知区间", [`${fmtRange(iv.start, iv.end)} · ${fmtDur(durSec(iv.start, iv.end))}`, "观察期内无窗口数据 · 点击标注"])} />
          ))}

          {/* evidence lane */}
          <text x={0} y={AXIS_H + HUMAN_H + STATUS_H + 12} fontSize={9.5} fill="#c0c0c8">事件</text>
          {tl.evidence.map((e) => {
            const x = X(t2m(e.at));
            if (x < LABEL_W - 4 || x > LABEL_W + timeW + 4) return null;
            return (
              <g key={e.id}
                onMouseMove={(ev) => setHoverAt(ev, e.kind === "commit" ? "Git 提交" : e.kind === "agent_user" ? "用户输入" : "会话启动", [
                  fmtHM(e.at),
                  e.label ? e.label.slice(0, 70) : "",
                ].filter(Boolean))}
                onMouseLeave={() => setHover(null)}>
                <circle cx={x} cy={AXIS_H + HUMAN_H + STATUS_H + 16} r={3.4} fill={kindColor(e.kind)} />
                {e.kind === "commit" && <circle cx={x} cy={AXIS_H + HUMAN_H + STATUS_H + 16} r={5.5} fill="none" stroke={kindColor(e.kind)} strokeOpacity={0.3} />}
                <circle cx={x} cy={AXIS_H + HUMAN_H + STATUS_H + 16} r={8} fill="transparent" />
              </g>
            );
          })}

          {/* ---------- agent lanes ---------- */}
          {agentRows.map((row, ri) => row.map((s) => {
            const y = agentTop + ri * (ROW_H + ROW_GAP);
            const lx0 = X(t2m(s.start));
            const lx1 = Math.max(X(t2m(s.end)), lx0 + 2);
            const col = agentColor(s.agent);
            const key = `s${s.id}`;
            const lines = [
              `${s.agent} · ${s.project}${s.status === "running" ? " · 运行中" : ""}`,
              `生命周期 ${fmtRange(s.start, s.end)}`,
              `执行 ${fmtDur(s.exec_sec)} · ${s.segments.length} 段 · 等待中 ${fmtDur(Math.max(0, durSec(s.start, s.end) - s.exec_sec))}`,
              `${s.n_events} 事件 · ${s.n_user} 次用户输入`,
              s.cwd ? s.cwd.split("/").slice(-1)[0] : "",
            ].filter(Boolean);
            return (
              <g key={key} className="cursor-pointer"
                onClick={() => onSelect({ kind: "session", session: s })}
                onMouseMove={(e) => setHoverAt(e, `${s.agent} 会话`, lines)}
                onMouseLeave={() => setHover(null)}
              >
                {/* row label */}
                <text x={0} y={y + 13} fontSize={10} fill={s.status === "running" ? col : "#8b8b95"}
                  className="font-medium">
                  {s.agent === "claude-code" ? "claude" : s.agent} · {(s.project || "?").slice(0, 9)}
                </text>
                <text x={0} y={y + 23} fontSize={9} fill="#b0b0ba" className="tabular-nums">
                  {fmtDur(s.exec_sec)}
                </text>
                {/* lifecycle hairline */}
                <rect x={lx0} y={y + 12} width={lx1 - lx0} height={2} rx={1} fill="#dcdce2" />
                {/* execution segments */}
                {s.segments.map((seg, si) => {
                  const s0 = X(t2m(seg.start));
                  const s1 = Math.max(X(t2m(seg.end)), s0 + 1.5);
                  return (
                    <rect key={si} x={s0} y={y + 7} width={s1 - s0} height={12} rx={3}
                      fill={col} fillOpacity={isSel(selection, key) ? 1 : 0.82}
                      stroke={isSel(selection, key) ? "#18181b" : "none"} strokeWidth={1.4} />
                  );
                })}
                {s.status === "running" && (
                  <circle cx={Math.min(lx1, LABEL_W + timeW)} cy={y + 13} r={3.5} fill={col} className="running-dot" />
                )}
                {/* wide invisible hit area */}
                <rect x={lx0} y={y} width={Math.max(lx1 - lx0, 8)} height={ROW_H} fill="transparent" />
              </g>
            );
          }))}

          <defs>
            <pattern id="unk" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
              <rect width="6" height="6" fill="#fffbeb" />
              <line x1="0" y1="0" x2="0" y2="6" stroke="#fbbf24" strokeWidth="2.5" />
            </pattern>
          </defs>
        </svg>
      </div>

      {hover && hover.title && (
        <div className="tl-tooltip" style={{
          left: Math.min(Math.max(hover.x + 14, 8), Math.max(width - 290, 8)),
          top: Math.min(hover.y + 14, 300),
        }}>
          <div className="tt-title">{hover.title}</div>
          {hover.lines.map((l, i) => <div key={i} className="tt-line">{l}</div>)}
        </div>
      )}
    </div>
  );
}
