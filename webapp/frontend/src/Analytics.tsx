import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts";
import { fmtDur, OTHER_PROJECT_COLOR, projectColor } from "./api";
import { useSse } from "./sse";

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
const dayLabel = (d: string) => `${+d.slice(5, 7)}/${+d.slice(8, 10)} 周${WD[new Date(d + "T12:00:00").getDay()]}`;
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
          <h1 className="text-[26px] font-semibold tracking-tight text-neutral-900">分析</h1>
          <div className="text-[12.5px] text-neutral-400">
            {data ? `${s!.days} 天 · ${s!.observed_days} 天有窗口观察 · 对比前 ${s!.days} 天` : "加载中…"}
            {err && <span className="ml-2 text-red-600">{err}</span>}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="seg">
            {[7, 14, 30].map((n) => (
              <button key={n} className={span === n ? "on" : ""} onClick={() => setSpan(n)}>近 {n} 天</button>
            ))}
          </div>
          <input type="date" value={start} max={end} onChange={(e) => { setSpan(null); setStart(e.target.value); }} className="input h-8 text-[12.5px]" />
          <span className="text-neutral-400">→</span>
          <input type="date" value={end} min={start} onChange={(e) => { setSpan(null); setEnd(e.target.value); }} className="input h-8 text-[12.5px]" />
        </div>
      </div>

      {data && s && (<>
        <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-6">
          <Tile label="日均电脑前活跃" v={s.human_per_day != null ? fmtDur(s.human_per_day) : "—"}
            cmp={delta(s.human_per_day, p?.human_per_day, (x) => fmtDur(Math.abs(x)))} hint="只算有窗口观察的日子" />
          <Tile label="日均 Agent 在跑" v={s.agent_per_day != null ? fmtDur(s.agent_per_day) : "—"}
            cmp={delta(s.agent_per_day, p?.agent_per_day, (x) => fmtDur(Math.abs(x)))} hint="所有会话执行段按时间去重" />
          <Tile label="归入项目" v={pct(s.project_share)} cmp={delta(s.project_share, p?.project_share, ppt)}
            hint="你的时间中落在某个项目泳道的比例" />
          <Tile label="长时间专注" v={pct(s.focus_share)} cmp={delta(s.focus_share, p?.focus_share, ppt)}
            hint="项目时间中处于 ≥30 分钟连续段的比例" />
          <Tile label="每小时切换" v={s.switches_per_hour != null ? s.switches_per_hour.toFixed(1) : "—"}
            cmp={delta(s.switches_per_hour, p?.switches_per_hour, (x) => Math.abs(x).toFixed(1), true)} hint="项目泳道之间的切换,短于 1 分钟的瞥一眼不算" />
          <Tile label="提交 / Prompt" v={`${s.commits} / ${s.prompts}`}
            cmp={p ? { text: `前期 ${p.commits} / ${p.prompts}`, tone: "flat" } : null} hint="整个周期的合计" />
        </div>

        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <Card title="一天的节奏" sub="每小时你在电脑前活跃的分钟数 · 悬停看 Agent 与切换">
            <Rhythm data={data} />
          </Card>
          <Card title="洞察" sub="由数据计算,附依据和可信度">
            <ul className="space-y-3">
              {data.insights.map((i, k) => (
                <li key={k}>
                  <div className="flex items-start gap-2">
                    <span className="min-w-0 flex-1 text-[13.5px] font-semibold leading-snug text-neutral-900">{i.title}</span>
                    <span className={`shrink-0 rounded px-1.5 text-[11px] leading-[18px] ${i.confidence === "高" ? "bg-neutral-100 text-neutral-600" : i.confidence === "中" ? "bg-neutral-100 text-neutral-500" : "bg-amber-50 text-amber-700"}`}>
                      可信度 {i.confidence}
                    </span>
                  </div>
                  <div className="mt-0.5 text-[12.5px] leading-relaxed text-neutral-500">{i.detail}</div>
                </li>
              ))}
              {!data.insights.length && <li className="text-[12.5px] text-neutral-400">数据不足,暂时没有可靠的结论。</li>}
            </ul>
          </Card>
        </div>

        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <Card title="时间都去哪了" sub="你在电脑前的时间,按项目堆叠 · 点柱子打开那一天">
            <DailyStack data={data} onDay={goDay} />
          </Card>
          <Card title="Agent 使用" sub="每天 Agent 在跑的时长与你发的 prompt">
            <AgentDaily data={data} />
          </Card>
        </div>

        <div className="grid items-start gap-4 lg:grid-cols-2">
          <Card title="你和 Agent 在各项目上的投入" sub="你的时间 vs Agent 执行时间(去重) · 小时">
            <YouVsAgent data={data} />
          </Card>
          <Card title="专注分布" sub="项目时间按所在连续段的长度分组 · 越往下越专注">
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
const ppt = (x: number) => `${Math.abs(Math.round(x * 100))} 个百分点`;
function delta(cur: number | null | undefined, prev: number | null | undefined, fmt: (x: number) => string, lowerIsBetter = false): Cmp {
  if (cur == null || prev == null) return null;
  const d = cur - prev;
  if (Math.abs(d) < 1e-9) return { text: "与前期持平", tone: "flat" };
  const up = d > 0;
  return { text: `${up ? "↑" : "↓"} ${fmt(d)} vs 前期`, tone: up !== lowerIsBetter ? "up" : "down" };
}

function Tile({ label, v, cmp, hint }: { label: string; v: string; cmp: Cmp; hint: string }) {
  return (
    <div className="card px-4 py-3" title={hint}>
      <div className="text-[12px] text-neutral-500">{label}</div>
      <div className="mt-0.5 text-[24px] font-semibold leading-tight tracking-tight tabular-nums text-neutral-900">{v}</div>
      <div className="mt-0.5 h-4 text-[11.5px] tabular-nums text-neutral-400">
        {cmp ? <span className={cmp.tone === "flat" ? "" : "text-neutral-600"}>{cmp.text}</span> : "前期无数据"}
      </div>
    </div>
  );
}

function Card({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
  return (
    <div className="card px-5 pb-4 pt-3.5">
      <div className="mb-2">
        <h2 className="text-[15px] font-semibold text-neutral-900">{title}</h2>
        <div className="text-[12px] text-neutral-400">{sub}</div>
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
      ...named.map((k) => [k, [k], projectColor(k)] as [string, string[], string]),
      ...(rest.length ? [["其他项目", rest, OTHER_PROJECT_COLOR] as [string, string[], string]] : []),
      ...(tot[OTHER] ? [[OTHER, [OTHER], "#d4d7dd"] as [string, string[], string]] : []),
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
          if (!d.has_observation) return `<b>${dayLabel(d.day)}</b><br/>没有窗口数据<br/>Agent 在跑 ${fmtDur(d.agent_sec)}`;
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
            label: { show: true, position: "insideTop" as const, color: MUTED, fontSize: 10.5, formatter: "无窗口数据" },
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
        return `<b>${r.project}</b>${r.priority != null ? ` · P${r.priority}` : ""}${ratio}` +
          row(YOU, "你", fmtDur(r.human_sec)) + row(AGENT, "Agent", fmtDur(r.agent_sec));
      },
    },
    yAxis: {
      type: "category", data: rows.map((r) => r.project), ...axisCat,
      axisLabel: { color: INK2, fontSize: 12, width: 110, overflow: "truncate" },
    },
    xAxis: { type: "value", ...axisVal, axisLabel: { ...axisVal.axisLabel, formatter: (v: number) => `${v}h` } },
    series: [
      { name: "你", type: "bar", color: YOU, barMaxWidth: 10, barGap: "30%", itemStyle: { borderRadius: [0, 4, 4, 0] },
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
        return `<b>连续段 ${data.focus_bins[x.dataIndex]}</b><br/>${fmtDur(mins[x.dataIndex] * 60)} · 占项目时间 ${Math.round(mins[x.dataIndex] / total * 100)}%`;
      },
    },
    yAxis: { type: "category", inverse: true, data: data.focus_bins, ...axisCat, axisLabel: { color: INK2, fontSize: 12 } },
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
      {longest && <div className="mt-1 text-[12px] text-neutral-500">最长一段:{longest.project} 连续 {fmtDur(longest.min * 60)}</div>}
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
            (v < 0 ? "没有窗口数据" : `你 ${v} 分钟 · 切换 ${d.hourly_switches[x]} 次`) +
            `<br/>Agent 在跑 ${Math.round(d.hourly_agent[x])} 分钟`;
        },
      },
      xAxis: { type: "category", data: [...Array(24).keys()].map((h) => `${h}`), ...axisCat, splitArea: { show: false } },
      yAxis: { type: "category", inverse: true, data: days.map((d) => dayLabel(d.day)), ...axisCat, axisLabel: { color: INK2, fontSize: 11.5 } },
      visualMap: {
        type: "piecewise", orient: "horizontal", left: 76, bottom: 0, itemWidth: 14, itemHeight: 10, textStyle: { color: MUTED, fontSize: 11 },
        pieces: [
          { value: -1, label: "无观察", color: "#f1f2f4" },
          { min: 0, max: 0, label: "0", color: "#fafafa" },
          { min: 1, max: 10, label: "1–10", color: SEQ[1] },
          { min: 11, max: 25, label: "11–25", color: SEQ[3] },
          { min: 26, max: 40, label: "26–40", color: SEQ[5] },
          { min: 41, max: 60, label: "41–60 分钟", color: SEQ[7] },
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
        return `<b>${dayLabel(d.day)}</b><br/>${title} ${fmt(vals[a.dataIndex])}<br/>会话 ${d.sessions} · 峰值并行 ${d.agent_peak}`; } },
    xAxis: { type: "category", data: data.days.map((d) => dayLabel(d.day).split(" ")[0]), ...axisCat },
    yAxis: { type: "value", ...axisVal, axisLabel: { ...axisVal.axisLabel, formatter: (v: number) => `${v}${unit}` } },
    series: [{ type: "bar", barMaxWidth: 16, color: "#2a78d6", itemStyle: { borderRadius: [3, 3, 0, 0] }, data: vals }],
  });
  const exec = useMemo(() => mk("Agent 在跑(去重)", data.days.map((d) => h1(d.agent_sec)), (v) => fmtDur(v * 3600), "h"), [data]); // eslint-disable-line
  const prm = useMemo(() => mk("你发的 prompt", data.days.map((d) => d.prompts), (v) => `${v} 条`, ""), [data]); // eslint-disable-line
  return <div className="space-y-1">{useEChart(exec, 150)}{useEChart(prm, 150)}</div>;
}
