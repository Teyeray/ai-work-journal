import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts";
import { fmtDur, OTHER_PROJECT_COLOR, projectColor } from "./api";
import { useSse } from "./sse";
import { label, locale, tr } from "./i18n";

/**
 * Analytics: how a range of days went, each block answering one question.
 *   KPI tiles        averages vs the previous equal-length period
 *   时间都去哪了       your time per day, stacked by project (unobserved days marked, not zero)
 *   洞察              computed claims with evidence + confidence
 *   你 vs Agent       per project: your time and agent execution time
 *   专注              how much project time sits in long continuous stretches
 *   一天的节奏          day × hour heatmap of your active minutes
 *   Agent            agent execution (de-duplicated) and prompts per day
 */

interface DayStat {
  day: string; has_observation: boolean; human_sec: number; agent_sec: number; agent_peak: number;
  unknown_sec: number; prompts: number; commits: number; sessions: number;
  allocation: Record<string, number>; agent_allocation: Record<string, number>;
  hourly_human: number[]; hourly_agent: number[]; hourly_switches: number[];
  switches: number; focus_min: number[]; longest: { project: string; min: number } | null;
}
interface Summary {
  days: number; observed_days: number; human_per_day: number | null; agent_per_day: number | null;
  project_share: number | null; switches_per_hour: number | null; focus_share: number | null;
  commits: number; prompts: number;
}
interface Overview {
  days: DayStat[]; summary: Summary; previous: Summary | null;
  projects: { project: string; human_sec: number; agent_sec: number; priority: number | null }[];
  focus_bins: string[];
  insights: { kind: string; title: string; detail: string; confidence: string }[];
}

const OTHER = "其他活动";
const INK2 = "#52525b", MUTED = "#a1a1aa", GRID = "#eef0f3";
const YOU = "#3f3f46", AGENT = "#a1a1aa";
const ORDINAL = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"];   // blue 250→650
const SEQ = ["#f3f7fd", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"];
const WD = "日一二三四五六";
const dayLabel = (d: string) => {
  const dt = new Date(d + "T12:00:00");
  const md = `${+d.slice(5, 7)}/${+d.slice(8, 10)}`;
  return tr(`${md} 周${WD[dt.getDay()]}`, `${md} ${dt.toLocaleDateString(locale(), { weekday: "short" })}`);
};
const h1 = (sec: number) => +(sec / 3600).toFixed(2);

export function Analytics({ days, goDay }: { days: string[]; goDay: (d: string) => void }) {
  const last = days[days.length - 1] ?? new Date().toLocaleDateString("sv");
  const [span, setSpan] = useState<number | null>(14);
  const shift = (d: string, n: number) => {
    const x = new Date(d + "T12:00:00"); x.setDate(x.getDate() + n); return x.toLocaleDateString("sv");
  };
  const [start, setStart] = useState(() => shift(last, -13));
  const [end, setEnd] = useState(last);
  useEffect(() => { if (span) { setEnd(last); setStart(shift(last, -(span - 1))); } }, [last, span]); // eslint-disable-line
  const [data, setData] = useState<Overview | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = () => {
    if (!start || !end || start > end) return;
    fetch(`/api/v1/analytics/overview?start=${start}&end=${end}`)
      .then((r) => (r.ok ? r.json() : r.json().then((b) => Promise.reject(b.detail ?? r.status))))
      .then((d) => { setData(d); setErr(null); }).catch((e) => setErr(String(e)));
  };
  useEffect(load, [start, end]); // eslint-disable-line
  useSse(load);

  const s = data?.summary, p = data?.previous;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[26px] font-semibold tracking-tight text-neutral-900">{tr("分析", "Analytics")}</h1>
          <div className="text-[12.5px] text-neutral-400">
            {data ? tr(`${s!.days} 天 · ${s!.observed_days} 天有记录`, `${s!.days} days · ${s!.observed_days} recorded`) : tr("加载中…", "Loading…")}
            {err && <span className="ml-2 text-red-600">{err}</span>}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="seg">
            {[7, 14, 30].map((n) => (
              <button key={n} className={span === n ? "on" : ""} onClick={() => setSpan(n)}>{tr(`近 ${n} 天`, `Last ${n} days`)}</button>
            ))}
          </div>
          <input type="date" value={start} max={end} onChange={(e) => { setSpan(null); setStart(e.target.value); }} className="input h-8 text-[12.5px]" />
          <span className="text-neutral-400">→</span>
          <input type="date" value={end} min={start} onChange={(e) => { setSpan(null); setEnd(e.target.value); }} className="input h-8 text-[12.5px]" />
        </div>
      </div>

      {data && s && (<>
        <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-6">
          <Tile label={tr("日均活跃", "Active / day")} v={s.human_per_day != null ? fmtDur(s.human_per_day) : "—"}
            cmp={delta(s.human_per_day, p?.human_per_day, (x) => fmtDur(Math.abs(x)))} hint={tr("仅计有记录的日子", "Recorded days only")} />
          <Tile label={tr("日均 Agent", "Agent / day")} v={s.agent_per_day != null ? fmtDur(s.agent_per_day) : "—"}
            cmp={delta(s.agent_per_day, p?.agent_per_day, (x) => fmtDur(Math.abs(x)))} hint={tr("按时间去重", "De-duplicated")} />
          <Tile label={tr("归入项目", "Assigned to projects")} v={pct(s.project_share)} cmp={delta(s.project_share, p?.project_share, ppt)}
            hint={tr("落在项目内的时间占比", "Share of time in a project")} />
          <Tile label={tr("长时间专注", "Long focus")} v={pct(s.focus_share)} cmp={delta(s.focus_share, p?.focus_share, ppt)}
            hint={tr("≥30 分钟连续段占比", "Share in stretches ≥30 min")} />
          <Tile label={tr("每小时切换", "Switches per hour")} v={s.switches_per_hour != null ? s.switches_per_hour.toFixed(1) : "—"}
            cmp={delta(s.switches_per_hour, p?.switches_per_hour, (x) => Math.abs(x).toFixed(1), true)} hint={tr("项目间切换,<1 分钟不计", "Between projects; <1 min ignored")} />
          <Tile label={tr("提交 / Prompt", "Commits / Prompts")} v={`${s.commits} / ${s.prompts}`}
            cmp={p ? { text: tr(`前期 ${p.commits} / ${p.prompts}`, `Prev ${p.commits} / ${p.prompts}`), tone: "flat" } : null} hint={tr("周期合计", "Period total")} />
        </div>

        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <Card title={tr("一天的节奏", "Daily rhythm")} sub={tr("分钟 / 小时", "min per hour")}>
            <Rhythm data={data} />
          </Card>
          <Card title={tr("洞察", "Insights")} sub="">
            <ul className="space-y-3">
              {data.insights.map((i, k) => (
                <li key={k}>
                  <div className="flex items-start gap-2">
                    <span className="min-w-0 flex-1 text-[13.5px] font-semibold leading-snug text-neutral-900">{i.title}</span>
                    <span className={`shrink-0 rounded px-1.5 text-[11px] leading-[18px] ${i.confidence === "高" ? "bg-neutral-100 text-neutral-600" : i.confidence === "中" ? "bg-neutral-100 text-neutral-500" : "bg-amber-50 text-amber-700"}`}>
                      {tr("可信度", "Confidence")} {label(i.confidence)}
                    </span>
                  </div>
                  <div className="mt-0.5 text-[12.5px] leading-relaxed text-neutral-500">{i.detail}</div>
                </li>
              ))}
              {!data.insights.length && <li className="text-[12.5px] text-neutral-400">{tr("数据不足", "Not enough data")}</li>}
            </ul>
          </Card>
        </div>

        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <Card title={tr("时间都去哪了", "Where the time went")} sub={tr("按项目", "By project")}>
            <DailyStack data={data} onDay={goDay} />
          </Card>
          <Card title={tr("Agent 使用", "Agent usage")} sub={tr("运行时长 · Prompt", "Runtime · prompts")}>
            <AgentDaily data={data} />
          </Card>
        </div>

        <div className="grid items-start gap-4 lg:grid-cols-2">
          <Card title={tr("各项目投入", "Per project")} sub={tr("你 vs Agent · 小时", "You vs agent · hours")}>
            <YouVsAgent data={data} />
          </Card>
          <Card title={tr("专注分布", "Focus distribution")} sub={tr("按连续段长度", "By stretch length")}>
            <Focus data={data} />
          </Card>
        </div>
      </>)}
    </div>
  );
}

// ---------------- tiles & cards ----------------

type Cmp = { text: string; tone: "up" | "down" | "flat" } | null;
const pct = (x: number | null) => (x == null ? "—" : `${Math.round(x * 100)}%`);
const ppt = (x: number) => tr(`${Math.abs(Math.round(x * 100))}pp`, `${Math.abs(Math.round(x * 100))}pp`);
function delta(cur: number | null | undefined, prev: number | null | undefined, fmt: (x: number) => string, lowerIsBetter = false): Cmp {
  if (cur == null || prev == null) return null;
  const d = cur - prev;
  if (Math.abs(d) < 1e-9) return { text: tr("持平", "Flat"), tone: "flat" };
  const up = d > 0;
  return { text: `${up ? "↑" : "↓"} ${fmt(d)} vs ${tr("前期", "prev")}`, tone: up !== lowerIsBetter ? "up" : "down" };
}

function Tile({ label, v, cmp, hint }: { label: string; v: string; cmp: Cmp; hint: string }) {
  return (
    <div className="card px-4 py-3" title={hint}>
      <div className="text-[12px] text-neutral-500">{label}</div>
      <div className="mt-0.5 text-[24px] font-semibold leading-tight tracking-tight tabular-nums text-neutral-900">{v}</div>
      <div className="mt-0.5 h-4 text-[11.5px] tabular-nums text-neutral-400">
        {cmp ? <span className={cmp.tone === "flat" ? "" : "text-neutral-600"}>{cmp.text}</span> : tr("无对比", "No comparison")}
      </div>
    </div>
  );
}

function Card({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
  return (
    <div className="card px-5 pb-4 pt-3.5">
      <div className="mb-2">
        <h2 className="text-[15px] font-semibold text-neutral-900">{title}</h2>
        {sub && <div className="text-[12px] text-neutral-400">{sub}</div>}
      </div>
      {children}
    </div>
  );
}

// ---------------- chart plumbing ----------------

function useEChart(option: echarts.EChartsOption | null, height: number, onClick?: (p: { dataIndex: number }) => void) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    if (!el.current) return;
    chart.current = echarts.init(el.current, undefined, { renderer: "svg" });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); chart.current?.dispose(); chart.current = null; };
  }, []);
  useEffect(() => { if (chart.current && option) chart.current.setOption(option, true); }, [option]);
  useEffect(() => {
    const c = chart.current;
    if (!c || !onClick) return;
    c.on("click", onClick as never);
    return () => { c.off("click"); };
  }, [onClick]);
  return <div ref={el} style={{ height }} className="w-full" />;
}

const base = {
  textStyle: { fontFamily: "inherit", color: INK2, fontSize: 11.5 },
  animationDuration: 300,
  tooltip: {
    backgroundColor: "#18181b", borderWidth: 0, padding: [8, 10],
    textStyle: { color: "#fff", fontSize: 12 }, extraCssText: "border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.18);",
  },
};
const axisCat = { axisLine: { lineStyle: { color: "#d4d4d8" } }, axisTick: { show: false }, axisLabel: { color: MUTED, fontSize: 11 } };
const axisVal = { splitLine: { lineStyle: { color: GRID } }, axisLabel: { color: MUTED, fontSize: 11 } };
const row = (color: string, name: string, val: string) =>
  `<div style="display:flex;gap:8px;align-items:center"><i style="width:8px;height:8px;border-radius:2px;background:${color};display:inline-block"></i><span style="flex:1">${name}</span><b style="font-weight:600">${val}</b></div>`;

// ---------------- 时间都去哪了 ----------------

function DailyStack({ data, onDay }: { data: Overview; onDay: (d: string) => void }) {
  const option = useMemo<echarts.EChartsOption>(() => {
    const tot: Record<string, number> = {};
    for (const d of data.days) for (const [k, v] of Object.entries(d.allocation)) tot[k] = (tot[k] ?? 0) + v;
    const named = Object.keys(tot).filter((k) => k !== OTHER && projectColor(k) !== OTHER_PROJECT_COLOR)
      .sort((a, b) => tot[b] - tot[a]).slice(0, 8);
    const rest = Object.keys(tot).filter((k) => k !== OTHER && !named.includes(k));
    const groups: [string, string[], string][] = [
      ...named.map((k) => [label(k), [k], projectColor(k)] as [string, string[], string]),
      ...(rest.length ? [[tr("其他项目", "Other projects"), rest, OTHER_PROJECT_COLOR] as [string, string[], string]] : []),
      ...(tot[OTHER] ? [[label(OTHER), [OTHER], "#d4d7dd"] as [string, string[], string]] : []),
    ];
    // consecutive days without window data → one band, one label
    const unobs: [string, string][] = [];
    data.days.forEach((d, i) => {
      if (d.has_observation) return;
      const prev = i > 0 && !data.days[i - 1].has_observation;
      if (prev) unobs[unobs.length - 1][1] = dayLabel(d.day);
      else unobs.push([dayLabel(d.day), dayLabel(d.day)]);
    });
    return {
      ...base,
      grid: { left: 36, right: 8, top: 34, bottom: 26 },
      legend: { top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, textStyle: { color: INK2, fontSize: 11.5 } },
      tooltip: {
        ...base.tooltip, trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: "rgba(0,0,0,.04)" } },
        formatter: (ps: unknown) => {
          const arr = ps as { dataIndex: number; seriesName: string; color: string; value: number }[];
          const d = data.days[arr[0].dataIndex];
          if (!d.has_observation) return `<b>${dayLabel(d.day)}</b><br/>${tr("没有窗口数据", "No window data")}<br/>${tr("Agent 运行", "Agent runtime")} ${fmtDur(d.agent_sec)}`;
          return `<b>${dayLabel(d.day)}</b> · ${fmtDur(d.human_sec)}<div style="margin-top:4px">` +
            arr.filter((x) => x.value > 0).sort((a, b) => b.value - a.value)
              .map((x) => row(x.color, x.seriesName, fmtDur(x.value * 3600))).join("") + "</div>";
        },
      },
      xAxis: { type: "category", data: data.days.map((d) => dayLabel(d.day)), ...axisCat },
      yAxis: { type: "value", ...axisVal, axisLabel: { ...axisVal.axisLabel, formatter: (v: number) => `${v}h` } },
      series: groups.map(([name, keys, color], gi) => ({
        name, type: "bar" as const, stack: "t", barMaxWidth: 30, color,
        itemStyle: { borderColor: "#fff", borderWidth: 1, borderRadius: gi === groups.length - 1 ? [4, 4, 0, 0] : 0 },
        emphasis: { focus: "series" as const },
        data: data.days.map((d) => h1(keys.reduce((s, k) => s + (d.allocation[k] ?? 0), 0))),
        ...(gi === 0 && unobs.length ? {
          markArea: {
            silent: true, itemStyle: { color: "rgba(160,165,175,.10)" },
            label: { show: true, position: "insideTop" as const, color: MUTED, fontSize: 10.5, formatter: tr("无窗口数据", "No window data") },
            data: unobs.map(([a, b]) => [{ xAxis: a }, { xAxis: b }]),
          },
        } : {}),
      })),
    };
  }, [data]);
  return useEChart(option, 320, (p) => onDay(data.days[p.dataIndex].day));
}

// ---------------- 你 vs Agent ----------------

function YouVsAgent({ data }: { data: Overview }) {
  const rows = data.projects.filter((r) => r.project !== OTHER && (r.human_sec >= 300 || r.agent_sec >= 300)).slice(0, 9).reverse();
  const option = useMemo<echarts.EChartsOption>(() => ({
    ...base,
    grid: { left: 8, right: 56, top: 30, bottom: 20, containLabel: true },
    legend: { top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, textStyle: { color: INK2 } },
    tooltip: {
      ...base.tooltip, trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: "rgba(0,0,0,.04)" } },
      formatter: (ps: unknown) => {
        const arr = ps as { dataIndex: number }[];
        const r = rows[arr[0].dataIndex];
        const ratio = r.human_sec ? ` · 1:${(r.agent_sec / r.human_sec).toFixed(1)}` : "";
        return `<b>${label(r.project)}</b>${r.priority != null ? ` · P${r.priority}` : ""}${ratio}` +
          row(YOU, tr("你", "You"), fmtDur(r.human_sec)) + row(AGENT, "Agent", fmtDur(r.agent_sec));
      },
    },
    yAxis: {
      type: "category", data: rows.map((r) => label(r.project)), ...axisCat,
      axisLabel: { color: INK2, fontSize: 12, width: 110, overflow: "truncate" },
    },
    xAxis: { type: "value", ...axisVal, axisLabel: { ...axisVal.axisLabel, formatter: (v: number) => `${v}h` } },
    series: [
      { name: tr("你", "You"), type: "bar", color: YOU, barMaxWidth: 10, barGap: "30%", itemStyle: { borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: "right", color: INK2, fontSize: 11, formatter: (p: { value: unknown }) => ((p.value as number) > 0 ? `${p.value}h` : "") },
        data: rows.map((r) => h1(r.human_sec)) },
      { name: "Agent", type: "bar", color: AGENT, barMaxWidth: 10, itemStyle: { borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: "right", color: MUTED, fontSize: 11, formatter: (p: { value: unknown }) => ((p.value as number) > 0 ? `${p.value}h` : "") },
        data: rows.map((r) => h1(r.agent_sec)) },
    ],
  }), [data]); // eslint-disable-line
  return useEChart(option, Math.max(180, rows.length * 34 + 50));
}

// ---------------- 专注 ----------------

function Focus({ data }: { data: Overview }) {
  const obs = data.days.filter((d) => d.has_observation);
  const mins = data.focus_bins.map((_, i) => obs.reduce((s, d) => s + d.focus_min[i], 0));
  const total = mins.reduce((a, b) => a + b, 0) || 1;
  const option = useMemo<echarts.EChartsOption>(() => ({
    ...base,
    grid: { left: 84, right: 96, top: 6, bottom: 20 },
    tooltip: {
      ...base.tooltip, trigger: "item",
      formatter: (p: unknown) => {
        const x = p as { dataIndex: number };
        return `<b>${tr("连续段", "Stretch")} ${label(data.focus_bins[x.dataIndex])}</b><br/>${fmtDur(mins[x.dataIndex] * 60)} · ${tr("占项目时间", "of project time")} ${Math.round(mins[x.dataIndex] / total * 100)}%`;
      },
    },
    yAxis: { type: "category", inverse: true, data: data.focus_bins.map(label), ...axisCat, axisLabel: { color: INK2, fontSize: 12 } },
    xAxis: { type: "value", ...axisVal, axisLabel: { ...axisVal.axisLabel, formatter: (v: number) => `${Math.round(v / 60)}h` } },
    series: [{
      type: "bar", barMaxWidth: 18,
      data: mins.map((v, i) => ({ value: Math.round(v), itemStyle: { color: ORDINAL[i], borderRadius: [0, 4, 4, 0] } })),
      label: { show: true, position: "right", color: INK2, fontSize: 11.5,
        formatter: (p: { dataIndex: number }) => `${fmtDur(mins[p.dataIndex] * 60)} · ${Math.round(mins[p.dataIndex] / total * 100)}%` },
    }],
  }), [data]); // eslint-disable-line
  const longest = obs.map((d) => d.longest).filter(Boolean).sort((a, b) => b!.min - a!.min)[0];
  return (
    <div>
      {useEChart(option, 200)}
      {longest && <div className="mt-1 text-[12px] text-neutral-500">{tr(`最长 · ${longest.project} · ${fmtDur(longest.min * 60)}`, `Longest · ${longest.project} · ${fmtDur(longest.min * 60)}`)}</div>}
    </div>
  );
}

// ---------------- 一天的节奏 ----------------

function Rhythm({ data }: { data: Overview }) {
  const days = data.days.filter((d) => d.has_observation || d.agent_sec > 0);
  const option = useMemo<echarts.EChartsOption>(() => {
    const cells: [number, number, number][] = [];
    days.forEach((d, y) => d.hourly_human.forEach((m, x) => cells.push([x, y, d.has_observation ? Math.round(m) : -1])));
    return {
      ...base,
      grid: { left: 76, right: 12, top: 8, bottom: 52 },
      tooltip: {
        ...base.tooltip, trigger: "item",
        formatter: (p: unknown) => {
          const [x, y, v] = (p as { value: [number, number, number] }).value;
          const d = days[y];
          return `<b>${dayLabel(d.day)} ${String(x).padStart(2, "0")}:00</b><br/>` +
            (v < 0 ? tr("没有窗口数据", "No window data") : tr(`你 ${v} 分钟 · 切换 ${d.hourly_switches[x]} 次`, `You ${v} min · ${d.hourly_switches[x]} ${d.hourly_switches[x] === 1 ? "switch" : "switches"}`)) +
            `<br/>${tr(`Agent ${Math.round(d.hourly_agent[x])} 分钟`, `Agent ${Math.round(d.hourly_agent[x])} min`)}`;
        },
      },
      xAxis: { type: "category", data: [...Array(24).keys()].map((h) => `${h}`), ...axisCat, splitArea: { show: false } },
      yAxis: { type: "category", inverse: true, data: days.map((d) => dayLabel(d.day)), ...axisCat, axisLabel: { color: INK2, fontSize: 11.5 } },
      visualMap: {
        type: "piecewise", orient: "horizontal", left: 76, bottom: 0, itemWidth: 14, itemHeight: 10, textStyle: { color: MUTED, fontSize: 11 },
        pieces: [
          { value: -1, label: tr("无观察", "No data"), color: "#f1f2f4" },
          { min: 0, max: 0, label: "0", color: "#fafafa" },
          { min: 1, max: 10, label: "1–10", color: SEQ[1] },
          { min: 11, max: 25, label: "11–25", color: SEQ[3] },
          { min: 26, max: 40, label: "26–40", color: SEQ[5] },
          { min: 41, max: 60, label: tr("41–60 分钟", "41–60 min"), color: SEQ[7] },
        ],
      },
      series: [{ type: "heatmap", data: cells, itemStyle: { borderColor: "#fff", borderWidth: 2, borderRadius: 3 } }],
    };
  }, [data]); // eslint-disable-line
  return useEChart(option, Math.max(160, days.length * 26 + 70));
}

// ---------------- Agent ----------------

function AgentDaily({ data }: { data: Overview }) {
  const mk = (title: string, vals: number[], fmt: (v: number) => string, unit: string): echarts.EChartsOption => ({
    ...base,
    title: { text: title, left: 0, top: 0, textStyle: { fontSize: 12, color: INK2, fontWeight: 500 } },
    grid: { left: 34, right: 6, top: 26, bottom: 22 },
    tooltip: { ...base.tooltip, trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: "rgba(0,0,0,.04)" } },
      formatter: (ps: unknown) => { const a = (ps as { dataIndex: number }[])[0]; const d = data.days[a.dataIndex];
        return `<b>${dayLabel(d.day)}</b><br/>${title} ${fmt(vals[a.dataIndex])}<br/>${tr(`会话 ${d.sessions} · 峰值并行 ${d.agent_peak}`, `Sessions ${d.sessions} · peak parallel ${d.agent_peak}`)}`; } },
    xAxis: { type: "category", data: data.days.map((d) => dayLabel(d.day).split(" ")[0]), ...axisCat },
    yAxis: { type: "value", ...axisVal, axisLabel: { ...axisVal.axisLabel, formatter: (v: number) => `${v}${unit}` } },
    series: [{ type: "bar", barMaxWidth: 16, color: "#2a78d6", itemStyle: { borderRadius: [3, 3, 0, 0] }, data: vals }],
  });
  const exec = useMemo(() => mk(tr("Agent 运行", "Agent runtime"), data.days.map((d) => h1(d.agent_sec)), (v) => fmtDur(v * 3600), "h"), [data]); // eslint-disable-line
  const prm = useMemo(() => mk(tr("Prompt", "Prompts"), data.days.map((d) => d.prompts), (v) => tr(`${v} 条`, `${v}`), ""), [data]); // eslint-disable-line
  return <div className="space-y-1">{useEChart(exec, 150)}{useEChart(prm, 150)}</div>;
}
