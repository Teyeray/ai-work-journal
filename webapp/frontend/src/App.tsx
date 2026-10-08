import { useEffect, useState } from "react";
import { api } from "./api";
import { Today } from "./Today";
import { Analytics } from "./Analytics";
import { Sessions } from "./Sessions";
import { Projects } from "./Projects";
import { useSse } from "./sse";

type Tab = "today" | "analytics" | "sessions" | "projects";

export default function App() {
  const [tab, setTab] = useState<Tab>(
    (new URLSearchParams(location.search).get("t") as Tab) || "today",
  );
  const [days, setDays] = useState<string[]>([]);
  const [day, setDay] = useState(() =>
    new URLSearchParams(location.search).get("day") ||
    new Date().toLocaleDateString("sv")); // local date, not UTC
  const [syncing, setSyncing] = useState(false);

  const loadDays = () => api.days().then((r) => {
    const ds = r.days;
    setDays(ds);
    if (!ds.includes(day)) setDay(ds[ds.length - 1] ?? day);
  }).catch(() => {});
  useEffect(() => { loadDays(); }, []);
  useSse(loadDays);

  const connected = useSse(() => {});

  const sync = async () => {
    setSyncing(true);
    try { await api.sync(); } catch { /* ignore */ }
    setSyncing(false);
    loadDays();
  };

  const go = (t: Tab) => {
    setTab(t);
    history.replaceState(null, "", `?t=${t}${t === "today" ? `&day=${day}` : ""}`);
  };
  const goTab = (t: string, d?: string) => {
    if (d) setDay(d);
    setTab(t as Tab);
    history.replaceState(null, "", `?t=${t}${t === "today" ? `&day=${d ?? day}` : ""}`);
  };

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-neutral-200/80 bg-white/85 backdrop-blur-md">
        <div className="mx-auto flex h-12 max-w-[1560px] items-center gap-5 px-6">
          <span className="flex items-center gap-2 text-[13.5px] font-semibold tracking-tight text-neutral-900">
            <span className="flex h-5 w-5 items-center justify-center rounded-md bg-neutral-800 text-[10px] font-bold text-white">AI</span>
            工作看板
          </span>
          <nav className="flex items-center gap-0.5">
            {([["today", "今日"], ["analytics", "分析"], ["sessions", "会话"], ["projects", "项目"]] as [Tab, string][])
              .map(([t, label]) => (
                <button key={t}
                  className={`tab ${tab === t ? "tab-active" : ""}`}
                  onClick={() => go(t)}>{label}</button>
              ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <span className={`flex items-center gap-1.5 text-[11px] font-medium ${connected ? "text-green-600" : "text-neutral-400"}`}>
              <span className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-green-500 running-dot" : "bg-neutral-300"}`} />
              {connected ? "实时" : "离线"}
            </span>
            <button className="btn" disabled={syncing} onClick={sync}>
              {syncing ? "同步中…" : "同步数据"}
            </button>
          </div>
        </div>
      </header>

      {tab === "today" && <Today day={day} days={days} onDayChange={setDay} goTab={goTab} />}
      {tab === "analytics" && <div className="mx-auto max-w-[1100px] px-6 py-5"><Analytics days={days.length ? days : [day]} /></div>}
      {tab === "sessions" && <div className="mx-auto max-w-[1100px] px-6 py-5"><Sessions day={day} /></div>}
      {tab === "projects" && <div className="mx-auto max-w-[1100px] px-6 py-5"><Projects /></div>}
    </div>
  );
}
