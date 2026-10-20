import { useEffect, useMemo, useRef, useState } from "react";
import { api, fmtDur, isCategory } from "./api";
import { tr, label, locale } from "./i18n";

/** Day navigator: ← [10-07 周三 ▾] →, opening a month calendar whose cells are
 *  shaded by how much you worked that day (one-hue sequential ramp). Stays the
 *  same size however many days have been recorded. */

export type DayInfo = { day: string; human_sec: number; agent_sec: number; has_observation: boolean; top: string[] };

// sequential, one hue light→dark (accent); text flips to white on the two darkest
export const RAMP = ["#e9ebfa", "#c5caf1", "#97a0e4", "#6872d4", "#424bab"];
export const STEPS = [3600, 3 * 3600, 5 * 3600, 7 * 3600]; // <1h, <3h, <5h, <7h, ≥7h of your time

const cache = new Map<string, Promise<Record<string, DayInfo>>>();
export const iso = (d: Date) => d.toLocaleDateString("sv");
export const addDays = (s: string, n: number) => { const d = new Date(s + "T12:00:00"); d.setDate(d.getDate() + n); return iso(d); };
export const monthKey = (s: string) => s.slice(0, 7);
export const shiftMonth = (m: string, n: number) => { const d = new Date(m + "-15T12:00:00"); d.setMonth(d.getMonth() + n); return iso(d).slice(0, 7); };

/** The 6×7 grid of a month (Monday first). */
export function grid(m: string): string[] {
  const first = new Date(m + "-01T12:00:00");
  const lead = (first.getDay() + 6) % 7;
  const start = addDays(m + "-01", -lead);
  return Array.from({ length: 42 }, (_, i) => addDays(start, i));
}

export function loadMonth(m: string, fresh = false): Promise<Record<string, DayInfo>> {
  if (!fresh && cache.has(m)) return cache.get(m)!;
  const g = grid(m);
  const p = api.analyticsTime(g[0], g[41]).then((r) => {
    const out: Record<string, DayInfo> = {};
    for (const d of r.days) {
      const top = Object.entries(d.allocation)
        .filter(([k]) => k !== "Unassigned" && k !== "其他活动" && !isCategory(k))
        .sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k]) => k);
      out[d.day] = { day: d.day, human_sec: d.human_sec, agent_sec: d.agent_sec, has_observation: d.has_observation, top };
    }
    return out;
  }).catch(() => { cache.delete(m); return {}; });
  cache.set(m, p);
  return p;
}

export function fmtDay(d: string) {
  const wd = new Date(d + "T12:00:00").getDay();
  return tr(`${d.slice(5)} 周${["日", "一", "二", "三", "四", "五", "六"][wd]}`,
    `${d.slice(5)} ${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][wd]}`);
}

export function DayPicker({ value, days, onChange, hotkeys = false }: {
  value: string; days: string[]; onChange: (d: string) => void; hotkeys?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [month, setMonth] = useState(monthKey(value));
  const [info, setInfo] = useState<Record<string, DayInfo>>({});
  const [hover, setHover] = useState<string | null>(null);
  const [focus, setFocus] = useState(value);
  const root = useRef<HTMLDivElement>(null);
  const known = useMemo(() => new Set(days), [days]);
  const idx = days.indexOf(value);
  const today = iso(new Date());
  const yesterday = addDays(today, -1);
  const latest = days[days.length - 1];
  const minMonth = days.length ? monthKey(days[0]) : monthKey(value);
  const maxMonth = monthKey(latest && latest > today ? latest : today);

  const prev = idx > 0 ? days[idx - 1] : null;
  const next = idx >= 0 && idx < days.length - 1 ? days[idx + 1] : null;
  const pick = (d: string) => { if (known.has(d)) { onChange(d); setOpen(false); } };

  useEffect(() => { if (open) { setMonth(monthKey(value)); setFocus(value); } }, [open]); // eslint-disable-line
  useEffect(() => {
    if (!open) return;
    let live = true;
    // the current month keeps changing; refetch it on every open
    loadMonth(month, month === monthKey(today)).then((r) => { if (live) setInfo((o) => ({ ...o, ...r })); });
    return () => { live = false; };
  }, [open, month]); // eslint-disable-line

  // close on outside click
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);

  // page-level ←/→ step between recorded days; skipped when something else handled the key
  useEffect(() => {
    if (!hotkeys) return;
    const h = (e: KeyboardEvent) => {
      if (open || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select, [contenteditable=true], .chat-panel")) return;
      if (e.key === "ArrowLeft" && prev) { e.preventDefault(); onChange(prev); }
      else if (e.key === "ArrowRight" && next) { e.preventDefault(); onChange(next); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [hotkeys, open, prev, next, onChange]);

  const onGridKey = (e: React.KeyboardEvent) => {
    const step: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
    if (e.key in step) {
      const f = addDays(focus, step[e.key]);
      setFocus(f);
      if (monthKey(f) !== month) setMonth(monthKey(f));
    } else if (e.key === "Enter") pick(focus);
    else if (e.key === "Escape") setOpen(false);
    else if (e.key === "PageUp") setMonth(shiftMonth(month, -1));
    else if (e.key === "PageDown") setMonth(shiftMonth(month, 1));
    else return;
    e.preventDefault();
    e.stopPropagation();
  };

  const shade = (d: DayInfo | undefined) => {
    if (!d || !d.has_observation) return null;
    let i = 0;
    while (i < STEPS.length && d.human_sec >= STEPS[i]) i++;
    return i;
  };
  const hv = hover ?? focus;
  const hi = info[hv];
  const cells = grid(month);

  return (
    <div ref={root} className="relative flex items-center gap-1">
      <button className="icon-btn" disabled={!prev} onClick={() => prev && onChange(prev)}
        title={prev ? tr(`前一天 ${fmtDay(prev)}(←)`, `Previous day ${fmtDay(prev)} (←)`) : tr("没有更早的记录", "No earlier records")}>←</button>
      <button className="dp-trigger" aria-haspopup="dialog" aria-expanded={open}
        onClick={() => setOpen((o) => !o)} title={tr("选择日期", "Pick a date")}>
        <span className="tabular-nums">{fmtDay(value)}</span>
        <span className="text-neutral-400">▾</span>
      </button>
      <button className="icon-btn" disabled={!next} onClick={() => next && onChange(next)}
        title={next ? tr(`后一天 ${fmtDay(next)}(→)`, `Next day ${fmtDay(next)} (→)`) : tr("已是最新一天", "Already the latest day")}>→</button>

      {open && (
        <div className="dp-pop" role="dialog" aria-label={tr("选择日期", "Pick a date")} tabIndex={-1}
          onKeyDown={onGridKey} ref={(el) => el?.focus({ preventScroll: true })}>
          <div className="flex items-center gap-1">
            <button className="icon-btn h-6 w-6" disabled={month <= minMonth} onClick={() => setMonth(shiftMonth(month, -1))} title={tr("上个月", "Previous month")}>‹</button>
            <div className="min-w-[84px] text-center text-[13px] font-medium text-neutral-800">
              {tr(`${+month.slice(0, 4)}年${+month.slice(5)}月`, new Date(month + "-15T12:00:00").toLocaleDateString(locale(), { year: "numeric", month: "short" }))}
            </div>
            <button className="icon-btn h-6 w-6" disabled={month >= maxMonth} onClick={() => setMonth(shiftMonth(month, 1))} title={tr("下个月", "Next month")}>›</button>
            <div className="ml-auto flex gap-1">
              {known.has(today) && <button className="dp-chip" onClick={() => pick(today)}>{tr("今天", "Today")}</button>}
              {known.has(yesterday) && <button className="dp-chip" onClick={() => pick(yesterday)}>{tr("昨天", "Yesterday")}</button>}
              {latest && latest !== today && <button className="dp-chip" onClick={() => pick(latest)}>{tr("最新", "Latest")}</button>}
            </div>
          </div>

          <div className="mt-2 grid grid-cols-7 gap-[3px]">
            {(tr("一,二,三,四,五,六,日", "M,T,W,T,F,S,S")).split(",").map((w, wi) => (
              <div key={wi} className="pb-0.5 text-center text-[10.5px] text-neutral-400">{w}</div>
            ))}
            {cells.map((d) => {
              const di = info[d], s = shade(di), has = known.has(d);
              const out = monthKey(d) !== month;
              const agentOnly = has && di && !di.has_observation && di.agent_sec > 0;
              const cls = ["dp-cell", out && "out", !has && "off", d === value && "sel",
                d === today && "today", d === focus && "focus", agentOnly && "agent"].filter(Boolean).join(" ");
              return (
                <button key={d} className={cls} disabled={!has} tabIndex={-1}
                  style={s != null ? { background: RAMP[s], color: s >= 3 ? "#fff" : undefined } : undefined}
                  onMouseEnter={() => setHover(d)} onMouseLeave={() => setHover(null)}
                  onClick={() => pick(d)} aria-label={d}>
                  {+d.slice(8)}
                </button>
              );
            })}
          </div>

          <div className="mt-2 min-h-[34px] border-t border-neutral-100 pt-1.5 text-[11.5px] leading-snug">
            <div className="font-medium text-neutral-700">{fmtDay(hv)}</div>
            <div className="truncate text-neutral-500">
              {!known.has(hv) ? tr("无记录", "No records")
                : !hi ? "…"
                : !hi.has_observation
                  ? (hi.agent_sec > 0 ? tr(`无窗口记录 · agent ${fmtDur(hi.agent_sec)}`, `No window data · agent ${fmtDur(hi.agent_sec)}`) : tr("无窗口记录", "No window data"))
                  : tr(`你 ${fmtDur(hi.human_sec)} · agent ${fmtDur(hi.agent_sec)}`, `You ${fmtDur(hi.human_sec)} · agent ${fmtDur(hi.agent_sec)}`) + (hi.top.length ? " · " + hi.top.map(label).join(tr("、", ", ")) : "")}
            </div>
          </div>

          <div className="mt-1.5 flex items-center gap-1 text-[10.5px] text-neutral-400">
            {tr("少", "Less")}{RAMP.map((c) => <span key={c} className="dp-key" style={{ background: c }} />)}{tr("多", "More")}
            <span className="dp-key agent ml-2" />{tr("仅 agent", "Agent only")}
            <span className="dp-key ml-2 border border-neutral-200 bg-white" />{tr("未采集", "No capture")}
          </div>
        </div>
      )}
    </div>
  );
}
