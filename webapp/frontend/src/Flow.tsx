import { useEffect, useMemo, useRef } from "react";
import { cleanTitle, isCategory, projectColor } from "./api";
import type { Timeline } from "./api";
import { tr, label, reason } from "./i18n";
import { durS, hm, hms, placeOf, t2m, type Visit } from "./visits";

/**
 * Activity flow: everything inside the magnifier window in time order —
 * each window visit (where you were, for how long), with the prompts you sent
 * and commits that landed interleaved. Rows highlight with the swimlane.
 */

type Item =
  | { k: "visit"; t: number; v: Visit }
  | { k: "burst"; t: number; vs: Visit[] }
  | { k: "prompt"; t: number; text: string; project: string }
  | { k: "commit"; t: number; text: string; repo: string };

const BURST_SEC = 6;   // visits shorter than this, back to back, fold into one row
const MAX_ROWS = 400;


export function Flow({ tl, visits, range, hoverVisit, onHoverVisit, focusVisit }: {
  tl: Timeline; visits: Visit[]; range: [number, number];
  hoverVisit: number | null; onHoverVisit: (i: number | null) => void; focusVisit: number | null;
}) {
  const [a, b] = range;
  const listRef = useRef<HTMLDivElement>(null);
  const inRange = useMemo(() => visits.filter((v) => v.e > a && v.s < b), [visits, a, b]);

  const items = useMemo(() => {
    const out: Item[] = [];
    let burst: Visit[] = [];
    const flush = () => {
      if (burst.length === 1) out.push({ k: "visit", t: burst[0].s, v: burst[0] });
      else if (burst.length > 1) out.push({ k: "burst", t: burst[0].s, vs: burst });
      burst = [];
    };
    for (const v of inRange) {
      if ((v.e - v.s) * 60 < BURST_SEC) burst.push(v);
      else { flush(); out.push({ k: "visit", t: v.s, v }); }
    }
    flush();
    for (const e of tl.evidence) {
      const m = t2m(e.at);
      if (m < a || m > b) continue;
      if (e.kind === "agent_user") out.push({ k: "prompt", t: m, text: e.label, project: e.project ?? "" });
      if (e.kind === "commit") out.push({ k: "commit", t: m, text: e.label, repo: e.repo ?? "" });
    }
    return out.sort((p, q) => p.t - q.t);
  }, [inRange, tl.evidence, a, b]);

  const stats = useMemo(() => {
    let active = 0, longest: Visit | null = null, switches = 0;
    inRange.forEach((v, i) => {
      const d = Math.min(v.e, b) - Math.max(v.s, a);
      active += d;
      if (!longest || v.e - v.s > longest.e - longest.s) longest = v;
      if (i > 0 && inRange[i - 1].appLabel !== v.appLabel) switches++;
    });
    const span = b - a;
    return { active, longest: longest as Visit | null, switches, perHour: span ? (switches / span) * 60 : 0,
      avg: inRange.length ? active / inRange.length : 0 };
  }, [inRange, a, b]);

  useEffect(() => {
    if (focusVisit == null) return;
    listRef.current?.querySelector(`[data-v="${focusVisit}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focusVisit]);

  const maxD = Math.max(1, ...inRange.map((v) => v.e - v.s));
  const bar = (d: number) => `${Math.max(4, (Math.log1p(d * 60) / Math.log1p(maxD * 60)) * 100)}%`;

  return (
    <div>
      <div className="border-b border-neutral-100 px-4 pb-3 pt-3.5">
        <div className="flex items-baseline justify-between">
          <h3 className="text-[15px] font-semibold text-neutral-900">{tr("活动流", "Activity flow")}</h3>
          <span className="text-[12px] tabular-nums text-neutral-400">{hm(a)}–{hm(b)}</span>
        </div>
        <div className="mt-2.5 grid grid-cols-3 gap-2">
          <Mini v={durS(stats.active)} l={tr("活跃", "Active")} />
          <Mini v={`${stats.switches}`} l={tr(`应用切换 · ${stats.perHour.toFixed(0)}/时`, `App switches · ${stats.perHour.toFixed(0)}/h`)} />
          <Mini v={durS(stats.avg)} l={tr("平均停留", "Avg. stay")} />
        </div>
        {stats.longest && (
          <div className="mt-2 truncate text-[12px] text-neutral-500">
            {tr("最长停留", "Longest stay")} <b className="font-medium text-neutral-700">{durS(stats.longest.e - stats.longest.s)}</b> · {stats.longest.appLabel} · {cleanTitle(stats.longest.title)}
          </div>
        )}
      </div>
      <div ref={listRef} className="px-2 py-1.5">
        {items.slice(0, MAX_ROWS).map((it, k) => {
          if (it.k === "visit") {
            const v = it.v, cat = isCategory(v.project);
            return (
              <div key={`v${v.i}`} data-v={v.i}
                className={`flow-row ${hoverVisit === v.i ? "hot" : ""} ${focusVisit === v.i ? "focus" : ""}`}
                onMouseEnter={() => onHoverVisit(v.i)} onMouseLeave={() => onHoverVisit(null)}>
                <span className="flow-t">{hms(v.s)}</span>
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="flow-app" style={{ color: cat ? "#71717a" : projectColor(v.project), borderColor: cat ? "#e4e4e7" : projectColor(v.project) }}>{v.appLabel}</span>
                    <span className="min-w-0 truncate text-[12.5px] text-neutral-800" title={`${v.title}\n${tr("归类", "Filed under")}: ${label(v.project)} · ${reason(v.reason ?? "")}`}>{placeOf(v)}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <div className="flow-bar"><i style={{ width: bar(v.e - v.s), background: cat ? "#c6cad2" : projectColor(v.project) }} /></div>
                    <span className="shrink-0 text-[11px] tabular-nums text-neutral-400">{durS(v.e - v.s)}{cat ? "" : ` · ${label(v.project)}`}</span>
                  </div>
                </div>
              </div>
            );
          }
          if (it.k === "burst") {
            const apps = [...new Set(it.vs.map((v) => v.appLabel))];
            return (
              <div key={`b${k}`} className="flow-row burst">
                <span className="flow-t">{hms(it.t)}</span>
                <div className="text-[12px] text-neutral-400">
                  {tr(`快速切换 ${it.vs.length} 次`, `${it.vs.length} quick switches`)} · {apps.join(" / ")} · {tr("共", "total")} {durS(it.vs.reduce((s, v) => s + v.e - v.s, 0))}
                </div>
              </div>
            );
          }
          if (it.k === "prompt") {
            return (
              <div key={`p${k}`} className="flow-row prompt">
                <span className="flow-t">{hms(it.t)}</span>
                <div className="flow-q" style={{ borderColor: projectColor(it.project) }}>
                  <span className="text-neutral-400">❯ {label(it.project)}</span> {it.text}
                </div>
              </div>
            );
          }
          return (
            <div key={`c${k}`} className="flow-row commit">
              <span className="flow-t">{hms(it.t)}</span>
              <div className="text-[12.5px] text-neutral-800"><span className="mr-1">◆</span>{it.text}
                <span className="ml-1 text-[11px] text-neutral-400">{it.repo}</span></div>
            </div>
          );
        })}
        {items.length > MAX_ROWS && <div className="px-2 py-2 text-[12px] text-neutral-400">{tr(`还有 ${items.length - MAX_ROWS} 条`, `${items.length - MAX_ROWS} more`)}</div>}
        {!items.length && <div className="px-2 py-6 text-[12.5px] text-neutral-400">{tr("无活动", "No activity")}</div>}
      </div>
    </div>
  );
}

function Mini({ v, l }: { v: string; l: string }) {
  return (
    <div className="rounded-lg bg-neutral-50 px-2.5 py-1.5">
      <div className="text-[15px] font-semibold tabular-nums text-neutral-900">{v}</div>
      <div className="truncate text-[11px] text-neutral-400">{l}</div>
    </div>
  );
}
