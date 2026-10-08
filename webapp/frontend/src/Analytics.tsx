import { useEffect, useRef, useState } from "react";
import * as echarts from "echarts";
import { api, fmtDur } from "./api";
import { useSse } from "./sse";

export function Analytics({ days }: { days: string[] }) {
  const last = days[days.length - 1] ?? "";
  const [end, setEnd] = useState(last);
  const [start, setStart] = useState(() => {
    const d = new Date((last || "") + "T12:00:00");
    d.setDate(d.getDate() - 13);
    return days.includes(d.toISOString().slice(0, 10)) || d.toISOString().slice(0, 10) <= last
      ? d.toISOString().slice(0, 10) : days[0] ?? "";
  });
  const [data, setData] = useState<Awaited<ReturnType<typeof api.analyticsTime>> | null>(null);

  const load = () => {
    if (start && end && start <= end) {
      api.analyticsTime(start, end).then(setData).catch(() => {});
    }
  };
  useEffect(() => { load(); }, [start, end]);
  useSse(load);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h2 className="text-lg font-semibold">长程分析</h2>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} className="input" />
          <span className="text-neutral-400">→</span>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className="input" />
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card p-2"><BarChart data={data} field="human_sec" title="人类活跃时长" /></div>
        <div className="card p-2"><BarChart data={data} field="agent_sec" title="Agent 运行时长(会话跨度)" /></div>
        <div className="card p-2 lg:col-span-2"><ProjectStack data={data} /></div>
      </div>
    </div>
  );
}

function useChart(dep: unknown) {
  const [ref, setRef] = useState<HTMLDivElement | null>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    if (!ref) return;
    chart.current?.dispose();
    chart.current = echarts.init(ref);
    return () => { chart.current?.dispose(); chart.current = null; };
  }, [ref]);
  useEffect(() => { dep; chart.current?.resize(); }, [dep, window.innerWidth]);
  return { ref: setRef, chart };
}

function BarChart({ data, field, title }: {
  data: Awaited<ReturnType<typeof api.analyticsTime>> | null;
  field: "human_sec" | "agent_sec";
  title: string;
}) {
  const { ref, chart } = useChart(data);
  useEffect(() => {
    if (!chart.current || !data) return;
    chart.current.setOption({
      title: { text: title, left: 8, textStyle: { fontSize: 13 } },
      grid: { left: 48, right: 16, top: 36, bottom: 24 },
      tooltip: { trigger: "axis", valueFormatter: (v: number) => fmtDur(v) },
      xAxis: { type: "category", data: data.days.map((d) => d.day.slice(5)) },
      yAxis: { type: "value", axisLabel: { formatter: (v: number) => `${Math.round(v / 3600)}h` } },
      series: [{
        type: "bar", barMaxWidth: 28,
        data: data.days.map((d) => ({
          value: Math.round(d[field] / 60),
          itemStyle: { color: d.has_observation ? "#2563eb" : "#d4d4d4" },
        })),
      }],
    });
  }, [chart, data, field]);
  return <div ref={ref} className="h-64 w-full" />;
}

function ProjectStack({ data }: {
  data: Awaited<ReturnType<typeof api.analyticsTime>> | null;
}) {
  const { ref, chart } = useChart(data);
  useEffect(() => {
    if (!chart.current || !data) return;
    const names = new Set<string>();
    for (const d of data.days) {
      for (const [k, v] of Object.entries(d.allocation)) {
        if (v > 300) names.add(k);
      }
    }
    const series = [...names].map((name) => ({
      name, type: "bar" as const, stack: "total", barMaxWidth: 28,
      color: palette(name),
      data: data.days.map((d) => Math.round((d.allocation[name] ?? 0) / 60)),
    }));
    chart.current.setOption({
      title: { text: "项目时间分布(堆叠,分钟)", left: 8, textStyle: { fontSize: 13 } },
      grid: { left: 48, right: 16, top: 36, bottom: 24 },
      legend: { top: 8, right: 16, type: "scroll", textStyle: { fontSize: 11 } },
      tooltip: {
        trigger: "axis",
        valueFormatter: (v: number) => fmtDur(v * 60),
      },
      xAxis: { type: "category", data: data.days.map((d) => d.day.slice(5)) },
      yAxis: { type: "value", axisLabel: { formatter: (v: number) => `${Math.round(v / 60)}h` } },
      series,
    });
  }, [chart, data]);
  return <div ref={ref} className="h-80 w-full" />;
}

const CATS = ["终端·其他", "网页浏览", "文献阅读", "文本编辑", "文件管理", "个人事务", "系统", "PyCharm"];
const PALETTE = [
  "#2563eb", "#dc2626", "#16a34a", "#9333ea", "#0891b2",
  "#d97706", "#db2777", "#4f46e5", "#059669", "#65a30d", "#ea580c",
];
function palette(name: string) {
  if (CATS.includes(name)) return "#9ca3af";
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
