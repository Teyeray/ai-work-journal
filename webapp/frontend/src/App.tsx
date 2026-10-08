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
    new Date().toISOString().slice(0, 10));
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

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-2.5">
          <span className="font-semibold">AI 工作看板</span>
          <nav className="flex gap-1">
            {([["today", "今日"], ["analytics", "分析"], ["sessions", "会话"], ["projects", "项目"]] as [Tab, string][])
              .map(([t, label]) => (
                <button key={t}
                  className={`rounded px-3 py-1 text-sm ${tab === t ? "bg-neutral-900 text-white" : "text-neutral-600 hover:bg-neutral-100"}`}
                  onClick={() => go(t)}>{label}</button>
              ))}
          </nav>
          <div className="ml-auto flex items-center gap-2 text-xs text-neutral-400">
            <span className={connected ? "text-green-600" : ""}>
              {connected ? "● live" : "○ offline"}
            </span>
            <button className="btn" disabled={syncing} onClick={sync}>
              {syncing ? "同步中…" : "同步"}
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-4">
        {tab === "today" && <Today day={day} days={days} onDayChange={setDay} />}
        {tab === "analytics" && <Analytics days={days.length ? days : [day]} />}
        {tab === "sessions" && <Sessions day={day} />}
        {tab === "projects" && <Projects />}
      </main>
    </div>
  );
}
