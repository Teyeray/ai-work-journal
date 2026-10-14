import { useEffect, useState } from "react";
import { api, setProjectColors } from "./api";
import { Today } from "./Today";
import { Analytics } from "./Analytics";
import { Sessions } from "./Sessions";
import { Projects } from "./Projects";
import { Reports } from "./Reports";
import { useSse } from "./sse";
import { ScreenAlert } from "./ScreenAlert";
import { ChatDock } from "./Chat";
import { setLang, tr, useLang } from "./i18n";

type Tab = "today" | "analytics" | "sessions" | "projects" | "reports";

export default function App() {
  const lang = useLang();
  const [tab, setTab] = useState<Tab>(
    (new URLSearchParams(location.search).get("t") as Tab) || "today",
  );
  const [days, setDays] = useState<string[]>([]);
  const [day, setDay] = useState(() =>
    new URLSearchParams(location.search).get("day") ||
    new Date().toLocaleDateString("sv")); // local date, not UTC
  const [syncing, setSyncing] = useState(false);
  // deep links from 问 AI answers: #session/12 · #day/2026-10-07 · #report/daily/2026-10-07
  const [focusSession, setFocusSession] = useState<{ id: number } | null>(null);
  const [focusReport, setFocusReport] = useState<{ kind: "daily" | "weekly"; name: string } | null>(null);
  const [, setColorsReady] = useState(false);
  useEffect(() => {
    api.projects().then((r) => {
      // only top-level, active projects take a palette slot (ranked by recent time)
      setProjectColors(r.projects.filter((p) => p.active && !p.rules?.parent && !p.rules?.ignore));
      setColorsReady(true);
    }).catch(() => {});
  }, []);

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

  const navigate = (href: string) => {
    const [kind, a, b] = href.split("/");
    if (kind === "session" && /^\d+$/.test(a)) { setFocusSession({ id: Number(a) }); goTab("sessions"); }
    else if (kind === "day" && /^\d{4}-\d{2}-\d{2}$/.test(a)) goTab("today", a);
    else if (kind === "report" && (a === "daily" || a === "weekly") && b) {
      setFocusReport({ kind: a, name: b.endsWith(".md") ? b : `${b}.md` }); goTab("reports");
    }
  };

  return (
    <div className="app-root min-h-screen">
      <header className="sticky top-0 z-20 border-b border-neutral-200/80 bg-white/85 backdrop-blur-md">
        <div className={`mx-auto flex h-12 ${tab === "today" ? "max-w-[2200px]" : "max-w-[1600px]"} items-center gap-5 px-6`}>
          <span className="flex items-center gap-2 text-[13.5px] font-semibold tracking-tight text-neutral-900">
            <span className="flex h-5 w-5 items-center justify-center rounded-md bg-neutral-800 text-[10px] font-bold text-white">AI</span>
            {tr("工作看板", "Work Journal")}
          </span>
          <nav className="flex items-center gap-0.5">
            {([["today", tr("今日", "Today")], ["analytics", tr("分析", "Analytics")], ["sessions", tr("会话", "Sessions")],
              ["projects", tr("项目", "Projects")], ["reports", tr("日报", "Reports")]] as [Tab, string][])
              .map(([t, label]) => (
                <button key={t}
                  className={`tab ${tab === t ? "tab-active" : ""}`}
                  onClick={() => go(t)}>{label}</button>
              ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <ScreenAlert key={lang} />
            <span className={`flex items-center gap-1.5 text-[11px] font-medium ${connected ? "text-green-600" : "text-neutral-400"}`}>
              <span className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-green-500 running-dot" : "bg-neutral-300"}`} />
              {connected ? tr("实时", "Live") : tr("离线", "Offline")}
            </span>
            <button className="btn" disabled={syncing} onClick={sync}>
              {syncing ? tr("同步中…", "Syncing…") : tr("同步数据", "Sync")}
            </button>
            <button className="btn" title={tr("Switch to English", "切换到中文")}
              onClick={() => setLang(lang === "zh" ? "en" : "zh")}>
              {lang === "zh" ? "EN" : "中"}
            </button>
          </div>
        </div>
      </header>

      <div key={lang}>
      {tab === "today" && <Today day={day} days={days} onDayChange={setDay} goTab={goTab} />}
      {tab === "analytics" && <div className="mx-auto max-w-[1600px] px-6 py-5"><Analytics days={days.length ? days : [day]} goDay={(d) => goTab("today", d)} /></div>}
      {tab === "sessions" && <div className="mx-auto max-w-[1600px] px-6 py-5"><Sessions day={day} days={days} focusId={focusSession} /></div>}
      {tab === "projects" && <div className="mx-auto max-w-[1600px] px-6 py-5"><Projects /></div>}
      <ChatDock tab={tab} day={day} onNavigate={navigate} />
      {tab === "reports" && <div className="mx-auto max-w-[1600px] px-6 py-5"><Reports focus={focusReport} /></div>}
      </div>
    </div>
  );
}
