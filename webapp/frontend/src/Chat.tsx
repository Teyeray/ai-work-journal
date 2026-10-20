import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { tr, label, locale } from "./i18n";
import { fmtDur } from "./api";
import { type DayInfo, RAMP, STEPS, addDays, fmtDay, grid, iso, loadMonth, monthKey, shiftMonth } from "./DayPicker";

/**
 * 问 AI: a side panel available on every tab. The backend runs a tool loop over
 * all journal data (progressive disclosure: overview → rows → full text); this
 * component streams its NDJSON events and renders the steps and the answer.
 */

export interface ChatCtx { tab?: string; day?: string; session_id?: number | null; view?: string; days?: string[] }

// pages publish what the user is looking at; sent with each question and shown in the footer
const pageCtx: ChatCtx = {};
const ctxSubs = new Set<() => void>();
export function setChatContext(c: Partial<ChatCtx>) {
  if (Object.entries(c).every(([k, v]) => pageCtx[k as keyof ChatCtx] === v)) return;
  Object.assign(pageCtx, c);
  ctxSubs.forEach((f) => f());
}
function usePageCtx() {
  const [, bump] = useState(0);
  useEffect(() => { const f = () => bump((n) => n + 1); ctxSubs.add(f); return () => { ctxSubs.delete(f); }; }, []);
  return pageCtx;
}

interface Approval { token: string; command: string; cwd: string; reason: string; decided?: boolean }
interface Step { id: string; name: string; args: Record<string, unknown>; summary?: string | null; error?: string | null; ms?: number; done?: boolean; approval?: Approval }
interface Turn { turn: number; question: string; steps: Step[]; answer: string; error?: string }
interface Thread { id: number; title: string; updated_at: string }

const toolLabel = (name: string): string => ({
  search: tr("全局搜索", "Global search"), list_projects: tr("项目列表", "Project list"), range_summary: tr("区间统计", "Range summary"),
  get_day: tr("查看某天", "View a day"), get_windows: tr("窗口记录", "Window log"), find_sessions: tr("查找会话", "Find sessions"),
  read_session: tr("读会话", "Read session"), list_commits: tr("提交记录", "Commits"), screen_text: tr("屏幕 OCR", "Screen OCR"),
  feishu: tr("飞书记录", "Feishu records"), list_reports: tr("日报列表", "Report list"), read_report: tr("读日报", "Read report"),
  read_file: tr("读文件", "Read file"), list_dir: tr("列目录", "List dir"), grep: tr("搜代码", "Grep"),
  run_bash: tr("执行命令", "Run command"), web_search: tr("联网搜索", "Web search"), web_fetch: tr("读网页", "Fetch page"),
} as Record<string, string>)[name] ?? name;

const suggest = (tab: string): string[] => ({
  today: [tr("今天我主要在干什么?时间都去哪了", "What did I mainly work on today? Where did the time go?"),
    tr("今天哪段时间切换最频繁,在切什么", "When did I switch context most today, and between what?"),
    tr("今天 agent 替我做了哪些事", "What did agents do for me today?")],
  analytics: [tr("上周和这周相比,各项目投入有什么变化", "How did time per project change from last week to this week?"),
    tr("我一般几点效率最高", "What time of day am I usually most productive?"),
    tr("最近哪个项目被冷落了", "Which project has been neglected lately?")],
  sessions: [tr("这个会话做了什么,结果如何", "What did this session do, and how did it turn out?"),
    tr("最近一周我给 agent 的指令有什么共同问题", "What problems do my prompts to agents share this past week?"),
    tr("找一下我让 agent 部署上线的会话", "Find the sessions where I had an agent deploy something")],
  projects: [tr("每个项目最近的进展和卡点", "Recent progress and blockers for each project"),
    tr("paperfacts 最近两周做了哪些功能", "What features did paperfacts get in the last two weeks?"),
    tr("哪些项目我花了时间但没有提交", "Which projects did I spend time on without committing?")],
  reports: [tr("把上周的周报压缩成三句话", "Condense last week's weekly report into three sentences"),
    tr("日报里反复出现的待办有哪些", "Which to-dos keep recurring in the daily reports?"),
    tr("对比这周和上周的日报", "Compare this week's daily reports with last week's")],
} as Record<string, string[]>)[tab] ?? suggest("today");

const tabName = (tab: string): string => ({
  today: tr("今日", "Today"), analytics: tr("分析", "Analytics"), sessions: tr("会话", "Sessions"),
  projects: tr("项目", "Projects"), reports: tr("日报", "Reports"),
} as Record<string, string>)[tab] ?? tab;

DOMPurify.addHook("afterSanitizeAttributes", (n) => {
  if (n.tagName === "A" && /^https?:/.test(n.getAttribute("href") || "")) {
    n.setAttribute("target", "_blank");
    n.setAttribute("rel", "noopener noreferrer");
  }
});

function md(text: string) {
  return DOMPurify.sanitize(marked.parse(text, { async: false, breaks: true }) as string);
}

function load<T>(k: string, d: T): T {
  try { const v = localStorage.getItem(k); return v == null ? d : (JSON.parse(v) as T); } catch { return d; }
}
function save(k: string, v: unknown) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } }

export function ChatDock({ tab, day, days, onNavigate }: { tab: string; day: string; days: string[]; onNavigate: (href: string) => void }) {
  const [open, setOpen] = useState<boolean>(() => load("chat.open", false));
  const [wide, setWide] = useState<boolean>(() => load("chat.wide", false));
  const [status, setStatus] = useState<{ available: boolean; model: string; models?: string[] } | null>(null);
  const [pickedModel, setPickedModel] = useState<string>(() => load("chat.model", ""));
  const [threads, setThreads] = useState<Thread[]>([]);
  const [showHist, setShowHist] = useState(false);
  const [tid, setTid] = useState<number | null>(() => load("chat.thread", null));
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // days picked in the chat's own calendar; empty = follow the page's day
  const [ctxDays, setCtxDays] = useState<string[]>(() => load("chat.days", []));
  const abort = useRef<AbortController | null>(null);
  const pending = useRef(new Set<string>());
  const scroller = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const stick = useRef(true);

  usePageCtx();
  useEffect(() => {
    setChatContext({ tab: tabName(tab), day, ...(tab === "sessions" ? {} : { session_id: null }) });
  }, [tab, day]);

  useEffect(() => save("chat.open", open), [open]);
  useEffect(() => {   // wide screens: the page makes room instead of being covered
    document.documentElement.style.setProperty("--chat-w", open ? (wide ? "720px" : "440px") : "0px");
  }, [open, wide]);
  useEffect(() => save("chat.wide", wide), [wide]);
  useEffect(() => save("chat.thread", tid), [tid]);
  useEffect(() => save("chat.days", ctxDays), [ctxDays]);
  useEffect(() => save("chat.model", pickedModel), [pickedModel]);
  // a model removed from the config falls back to the default
  const modelId = status?.models?.includes(pickedModel) ? pickedModel : status?.model ?? "";
  useEffect(() => {   // closing the tab denies whatever is waiting, so the server doesn't hold it
    const h = () => pending.current.forEach((token) =>
      navigator.sendBeacon("/api/v1/chat/approve", new Blob([JSON.stringify({ token, allow: false })], { type: "application/json" })));
    window.addEventListener("pagehide", h);
    return () => window.removeEventListener("pagehide", h);
  }, []);

  const loadThreads = () => fetch("/api/v1/chat/threads").then((r) => r.json()).then((r) => setThreads(r.threads)).catch(() => {});
  useEffect(() => {
    if (!open) return;
    if (!status) fetch("/api/v1/chat/status").then((r) => r.json()).then(setStatus).catch(() => setStatus({ available: false, model: "", models: [] }));
    loadThreads();
  }, [open]); // eslint-disable-line

  // restore the open thread (once per thread id, never mid-stream)
  useEffect(() => {
    if (!open || tid == null || busy) return;
    fetch(`/api/v1/chat/threads/${tid}`).then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((v) => setTurns(v.turns)).catch(() => { setTid(null); setTurns([]); });
  }, [open, tid]); // eslint-disable-line

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") { e.preventDefault(); setOpen((o) => !o); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);
  useEffect(() => { if (open) setTimeout(() => box.current?.focus(), 50); }, [open]);

  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [turns]);

  const patchLast = (f: (t: Turn) => Turn) => setTurns((ts) => (ts.length ? [...ts.slice(0, -1), f(ts[ts.length - 1])] : ts));

  const send = async (text: string) => {
    text = text.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    stick.current = true;
    setTurns((ts) => [...ts, { turn: (ts[ts.length - 1]?.turn ?? 0) + 1, question: text, steps: [], answer: "" }]);
    const ac = new AbortController();
    abort.current = ac;
    try {
      const r = await fetch("/api/v1/chat", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: ac.signal,
        body: JSON.stringify({ text, thread_id: tid, model: modelId || null, context: { ...pageCtx, ...(ctxDays.length ? { days: ctxDays } : {}) } }),
      });
      if (!r.ok || !r.body) throw new Error(r.status === 503 ? tr("LLM 未配置(需要 ~/.config/ai-work-journal/.env 里的百炼 key)", "LLM not configured (needs a Bailian key in ~/.config/ai-work-journal/.env)") : `HTTP ${r.status}`);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (!line) continue;
          let ev: any;
          try { ev = JSON.parse(line); } catch { continue; }
          if (ev.type === "thread") setTid(ev.id);
          else if (ev.type === "delta") patchLast((t) => ({ ...t, answer: t.answer + ev.text }));
          else if (ev.type === "step") patchLast((t) => ({ ...t, steps: [...t.steps, { id: ev.id, name: ev.name, args: ev.args }] }));
          else if (ev.type === "approval") {
            pending.current.add(ev.token);
            patchLast((t) => ({ ...t, steps: t.steps.map((s) => (s.id === ev.id ? { ...s, approval: { token: ev.token, command: ev.command, cwd: ev.cwd, reason: ev.reason } } : s)) }));
          }
          else if (ev.type === "step_done") patchLast((t) => ({ ...t, steps: t.steps.map((s) => (s.id === ev.id ? { ...s, ...ev, done: true } : s)) }));
          else if (ev.type === "error") patchLast((t) => ({ ...t, error: ev.message }));
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") patchLast((t) => ({ ...t, error: String((e as Error).message || e) }));
      else patchLast((t) => ({ ...t, error: tr("已停止", "Stopped") }));
    } finally {
      setBusy(false);
      abort.current = null;
      pending.current.clear();
      loadThreads();
    }
  };

  const decide = (token: string, allow: boolean) => {
    pending.current.delete(token);
    patchLast((t) => ({ ...t, steps: t.steps.map((s) => (s.approval?.token === token ? { ...s, approval: { ...s.approval, decided: true } } : s)) }));
    fetch("/api/v1/chat/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token, allow }) }).catch(() => {});
  };
  const stop = () => {   // a command waiting for approval is denied, so the server side ends too
    pending.current.forEach((tk) => decide(tk, false));
    abort.current?.abort();
  };

  const newChat = () => { if (busy) return; setTid(null); setTurns([]); setShowHist(false); box.current?.focus(); };
  const pick = (id: number) => { if (busy) return; setShowHist(false); setTurns([]); setTid(id); };
  const remove = async (id: number) => {
    await fetch(`/api/v1/chat/threads/${id}`, { method: "DELETE" }).catch(() => {});
    if (id === tid) newChat();
    loadThreads();
  };

  const click = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href") || "";
    if (href.startsWith("#")) { e.preventDefault(); onNavigate(href.slice(1)); }
    const img = (e.target as HTMLElement).closest("img");
    if (img && !a) window.open(img.getAttribute("src") || "", "_blank");
  };

  const title = threads.find((t) => t.id === tid)?.title;

  if (!open) {
    return (
      <button className="chat-fab" onClick={() => setOpen(true)} title={tr("问 AI (Ctrl/⌘ + J)", "Ask AI (Ctrl/⌘ + J)")}>
        <Spark /> {tr("问 AI", "Ask AI")} <kbd>⌘J</kbd>
      </button>
    );
  }

  return (
    <section className={`chat-panel ${wide ? "wide" : ""}`} aria-label={tr("问 AI", "Ask AI")}>
      <header className="flex h-11 shrink-0 items-center gap-1.5 border-b border-neutral-200 px-3">
        <Spark />
        <div className="min-w-0 flex-1 truncate text-[13px] font-semibold text-neutral-800">{title || tr("问 AI", "Ask AI")}</div>
        <button className="chat-icon" title={tr("历史对话", "History")} onClick={() => setShowHist((v) => !v)}>{tr("历史", "History")}</button>
        <button className="chat-icon" title={tr("新对话", "New chat")} onClick={newChat} disabled={busy}>{tr("新对话", "New chat")}</button>
        <button className="chat-icon" title={wide ? tr("收窄", "Narrow") : tr("加宽", "Widen")} onClick={() => setWide((w) => !w)}>{wide ? "⇥" : "⇤"}</button>
        <button className="chat-icon" title={tr("关闭 (⌘J)", "Close (⌘J)")} onClick={() => setOpen(false)}>✕</button>
      </header>

      {showHist && (
        <div className="chat-hist">
          {threads.length === 0 && <div className="px-3 py-4 text-[12px] text-neutral-400">{tr("还没有对话", "No conversations yet")}</div>}
          {threads.map((t) => (
            <div key={t.id} className={`chat-hist-row ${t.id === tid ? "on" : ""}`}>
              <button className="min-w-0 flex-1 truncate text-left" onClick={() => pick(t.id)}>{t.title || tr("(无标题)", "(untitled)")}</button>
              <span className="shrink-0 text-[11px] text-neutral-400">{new Date(t.updated_at).toLocaleDateString("sv").slice(5)}</span>
              <button className="chat-del" title={tr("删除", "Delete")} onClick={() => remove(t.id)}>✕</button>
            </div>
          ))}
        </div>
      )}

      <div ref={scroller} className="chat-body" onClick={click}
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; }}>
        {turns.length === 0 && (
          <Empty status={status} tab={tab} onPick={send} />
        )}
        {turns.map((t, i) => (
          <TurnView key={t.turn} t={t} live={busy && i === turns.length - 1} onRetry={() => send(t.question)} onDecide={decide} />
        ))}
      </div>

      <form className="chat-input" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <textarea ref={box} value={input} rows={Math.min(6, Math.max(1, input.split("\n").length))}
          placeholder={status && !status.available ? tr("LLM 未配置", "LLM not configured") : tr("问点什么…", "Ask anything…")}
          disabled={status ? !status.available : false}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(input); }
            if (e.key === "Escape") setOpen(false);
          }} />
        {busy
          ? <button type="button" className="chat-send stop" onClick={stop} title={tr("停止", "Stop")}>■</button>
          : <button type="submit" className="chat-send" disabled={!input.trim()} title={tr("发送", "Send")}>↑</button>}
      </form>
      <div className="chat-foot">
        <ContextDays sel={ctxDays} onChange={setCtxDays} known={days} pageDay={day}
          label={ctxDays.length ? undefined : `${pageCtx.tab ?? ""} · ${pageCtx.day ?? day}${pageCtx.session_id ? ` · ${tr("会话", "session")} #${pageCtx.session_id}` : ""}`} />
        {status?.model && ((status.models?.length ?? 0) > 1
          ? <ModelMenu models={status.models!} current={modelId} def={status.model} disabled={busy}
              onPick={(m) => setPickedModel(m === status.model ? "" : m)}
              onDefault={(m) => fetch("/api/v1/chat/default", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: m }) })
                .then((r) => (r.ok ? r.json() : Promise.reject())).then((r) => { setStatus((st) => (st ? { ...st, model: r.model } : st)); setPickedModel(""); })
                .catch(() => {})} />
          : <span className="ml-auto shrink-0">{status.model}</span>)}
      </div>
    </section>
  );
}

function Empty({ status, tab, onPick }: { status: { available: boolean } | null; tab: string; onPick: (q: string) => void }) {
  if (status && !status.available) {
    return <div className="px-2 py-10 text-center text-[12.5px] leading-relaxed text-neutral-500">
      {tr("LLM 未配置。在 ", "LLM not configured. Add BAILIAN_API_KEY / BAILIAN_BASE_URL to ")}<code>~/.config/ai-work-journal/.env</code>{tr(" 写入 BAILIAN_API_KEY / BAILIAN_BASE_URL 后重启看板。", " and restart the dashboard.")}</div>;
  }
  return (
    <div className="px-1 pt-6">
      <div className="text-[15px] font-semibold text-neutral-800">{tr("问 AI", "Ask AI")}</div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-neutral-500">
        {tr("工作记录 · 本地代码 · 命令 · 联网", "Work history · local code · shell · web")}
      </p>
      <div className="mt-4 flex flex-col gap-1.5">
        {suggest(tab).map((q) => (
          <button key={q} className="chat-suggest" onClick={() => onPick(q)}>{q}</button>
        ))}
      </div>
    </div>
  );
}

function TurnView({ t, live, onRetry, onDecide }: { t: Turn; live: boolean; onRetry: () => void; onDecide: (token: string, allow: boolean) => void }) {
  const html = useMemo(() => md(t.answer), [t.answer]);
  const [copied, setCopied] = useState(false);
  const thinking = live && !t.answer && t.steps.every((s) => s.done);
  return (
    <div className="chat-turn">
      <div className="chat-q">{t.question}</div>
      {t.steps.length > 0 && <Steps steps={t.steps} live={live} onDecide={onDecide} />}
      {thinking && <div className="chat-thinking"><span /><span /><span /></div>}
      {t.answer && <div className={`md chat-md ${live ? "live" : ""}`} dangerouslySetInnerHTML={{ __html: html }} />}
      {t.error && (
        <div className="mt-2 flex items-center gap-2 text-[12px] text-red-600">
          <span>{t.error === tr("已停止", "Stopped") ? tr("已停止", "Stopped") : `${tr("出错:", "Error: ")}${t.error}`}</span>
          {!live && <button className="link" onClick={onRetry}>{tr("重试", "Retry")}</button>}
        </div>
      )}
      {!live && t.answer && (
        <button className="chat-copy" onClick={() => { navigator.clipboard?.writeText(t.answer).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200); }); }}>
          {copied ? tr("已复制", "Copied") : tr("复制", "Copy")}
        </button>
      )}
    </div>
  );
}

function Steps({ steps, live, onDecide }: { steps: Step[]; live: boolean; onDecide: (token: string, allow: boolean) => void }) {
  const [open, setOpen] = useState(false);
  const show = open || live || steps.some((s) => s.approval && !s.approval.decided && !s.done);
  const n = steps.length;
  return (
    <div className="chat-steps">
      <button className="chat-steps-head" onClick={() => setOpen((o) => !o)}>
        <span className={`chat-caret ${show ? "open" : ""}`}>▸</span>
        {live && steps.some((s) => !s.done) ? tr("正在查询…", "Querying…") : tr(`查了 ${n} 处数据`, `Checked ${n} data source${n === 1 ? "" : "s"}`)}
        {!show && <span className="truncate text-neutral-400"> · {[...new Set(steps.map((s) => toolLabel(s.name)))].join(tr("、", ", "))}</span>}
      </button>
      {show && steps.map((s) => (
        <div key={s.id} className="chat-step">
          <span className={`chat-dot ${s.done ? (s.error ? "err" : "ok") : "run"}`} />
          <span className="font-medium text-neutral-700">{toolLabel(s.name)}</span>
          <span className="truncate text-neutral-400">{argText(s.args)}</span>
          <span className={`ml-auto shrink-0 ${s.error ? "text-red-500" : "text-neutral-500"}`}>{s.error ? (s.approval ? tr("未执行", "Not run") : tr("出错", "Error")) : s.summary ?? ""}</span>
        </div>
      ))}
      {steps.filter((s) => s.approval && !s.approval.decided && !s.done).map((s) => (
        <div key={s.id + "-ok"} className="chat-approve">
          <div className="text-[11.5px] text-amber-800">{tr("需要你批准:", "Needs your approval: ")}<span className="text-amber-700/80">{s.approval!.reason}</span></div>
          <pre>{s.approval!.command}</pre>
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate text-[11px] text-neutral-500">{s.approval!.cwd && tr("目录 ", "in ") + s.approval!.cwd}</span>
            <button className="chat-approve-no" onClick={() => onDecide(s.approval!.token, false)}>{tr("拒绝", "Deny")}</button>
            <button className="chat-approve-yes" onClick={() => onDecide(s.approval!.token, true)}>{tr("允许执行", "Allow")}</button>
          </div>
        </div>
      ))}
    </div>
  );
}

function argText(a: Record<string, unknown>) {
  return Object.entries(a || {}).filter(([, v]) => v !== "" && v != null)
    .map(([k, v]) => (k === "id" ? `#${v}` : String(v))).join(" · ");
}

function Spark() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden className="shrink-0 text-[var(--accent)]">
      <path fill="currentColor" d="M8 0l1.7 5.1L15 7l-5.3 1.9L8 14l-1.7-5.1L1 7l5.3-1.9z" />
    </svg>
  );
}

/** Footer context: the page's day by default, or several days picked from a
 *  month calendar (click toggles a day, shift-click selects a range). */
function ContextDays({ sel, onChange, known, pageDay, label: pageLabel }: {
  sel: string[]; onChange: (d: string[]) => void; known: string[]; pageDay: string; label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [month, setMonth] = useState(monthKey(sel[sel.length - 1] ?? pageDay));
  const [info, setInfo] = useState<Record<string, DayInfo>>({});
  const [anchor, setAnchor] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const has = useMemo(() => new Set(known), [known]);
  const picked = useMemo(() => new Set(sel), [sel]);
  const today = iso(new Date());

  useEffect(() => {
    if (!open) return;
    let live = true;
    loadMonth(month, month === monthKey(today)).then((r) => { if (live) setInfo((o) => ({ ...o, ...r })); });
    return () => { live = false; };
  }, [open, month]); // eslint-disable-line
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);

  const span = (a: string, b: string) => {
    const [lo, hi] = a < b ? [a, b] : [b, a];
    const out: string[] = [];
    for (let d = lo; d <= hi; d = addDays(d, 1)) if (has.has(d)) out.push(d);
    return out;
  };
  const click = (d: string, shift: boolean) => {
    if (shift && anchor) onChange([...new Set([...sel, ...span(anchor, d)])].sort());
    else onChange(picked.has(d) ? sel.filter((x) => x !== d) : [...sel, d].sort());
    setAnchor(d);
  };
  const mon = addDays(today, -((new Date().getDay() + 6) % 7));
  const presets: [string, string[]][] = [
    [tr("本周", "This week"), span(mon, today)],
    [tr("上周", "Last week"), span(addDays(mon, -7), addDays(mon, -1))],
    [tr("近 7 天", "Last 7 days"), span(addDays(today, -6), today)],
    [tr("近 30 天", "Last 30 days"), span(addDays(today, -29), today)],
  ];
  const shade = (d?: DayInfo) => {
    if (!d || !d.has_observation) return null;
    let i = 0;
    while (i < STEPS.length && d.human_sec >= STEPS[i]) i++;
    return i;
  };
  const tot = sel.reduce((n, d) => n + (info[d]?.human_sec ?? 0), 0);
  const summary = !sel.length ? "" : sel.length === 1 ? fmtDay(sel[0])
    : tr(`${sel.length} 天 · ${sel[0].slice(5)}~${sel[sel.length - 1].slice(5)}`, `${sel.length} days · ${sel[0].slice(5)}–${sel[sel.length - 1].slice(5)}`);

  return (
    <div ref={root} className="relative min-w-0">
      <button className="chat-ctx" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((o) => !o)}
        title={tr("选择对话的上下文日期(可多选)", "Pick the days this chat is about (multi-select)")}>
        {tr("上下文:", "Context: ")}<span className={sel.length ? "font-medium text-[var(--accent)]" : ""}>{sel.length ? summary : pageLabel}</span> ▾
      </button>
      {open && (
        <div className="dp-pop chat-ctx-pop" role="dialog" aria-label={tr("上下文日期", "Context days")}>
          <div className="flex items-center gap-1">
            <button className="icon-btn h-6 w-6" onClick={() => setMonth(shiftMonth(month, -1))} title={tr("上个月", "Previous month")}>‹</button>
            <div className="min-w-[84px] text-center text-[13px] font-medium text-neutral-800">
              {tr(`${+month.slice(0, 4)}年${+month.slice(5)}月`, new Date(month + "-15T12:00:00").toLocaleDateString(locale(), { year: "numeric", month: "short" }))}
            </div>
            <button className="icon-btn h-6 w-6" disabled={month >= monthKey(today)} onClick={() => setMonth(shiftMonth(month, 1))} title={tr("下个月", "Next month")}>›</button>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {presets.map(([name, ds]) => (
              <button key={name} className="dp-chip border border-neutral-200" disabled={!ds.length} onClick={() => { onChange(ds); setAnchor(null); }}>{name}</button>
            ))}
          </div>
          <div className="mt-2 grid grid-cols-7 gap-[3px]">
            {(tr("一,二,三,四,五,六,日", "M,T,W,T,F,S,S")).split(",").map((w, wi) => (
              <div key={wi} className="pb-0.5 text-center text-[10.5px] text-neutral-400">{w}</div>
            ))}
            {grid(month).map((d) => {
              const di = info[d], s = shade(di), ok = has.has(d);
              const cls = ["dp-cell", monthKey(d) !== month && "out", !ok && "off", picked.has(d) && "sel",
                d === today && "today", ok && di && !di.has_observation && di.agent_sec > 0 && "agent"].filter(Boolean).join(" ");
              return (
                <button key={d} className={cls} disabled={!ok} aria-pressed={picked.has(d)} aria-label={d}
                  title={di ? tr(`你 ${fmtDur(di.human_sec)} · agent ${fmtDur(di.agent_sec)}`, `You ${fmtDur(di.human_sec)} · agent ${fmtDur(di.agent_sec)}`) + (di.top.length ? " · " + di.top.map(label).join(", ") : "") : d}
                  style={s != null ? { background: RAMP[s], color: s >= 3 ? "#fff" : undefined } : undefined}
                  onClick={(e) => click(d, e.shiftKey)}>
                  {+d.slice(8)}
                </button>
              );
            })}
          </div>
          <div className="mt-2 flex items-center gap-2 border-t border-neutral-100 pt-1.5 text-[11.5px] text-neutral-500">
            <span className="min-w-0 flex-1 truncate">
              {sel.length ? tr(`已选 ${sel.length} 天`, `${sel.length} day${sel.length > 1 ? "s" : ""} selected`) + (tot ? ` · ${tr("你", "you")} ${fmtDur(tot)}` : "")
                : tr("Shift 选范围", "Shift-click for range")}
            </span>
            {sel.length > 0 && <button className="dp-chip" onClick={() => { onChange([]); setAnchor(null); }}>{tr("清空", "Clear")}</button>}
          </div>
        </div>
      )}
    </div>
  );
}

const VENDORS: [RegExp, string][] = [[/qwen/i, "Qwen"], [/deepseek/i, "DeepSeek"], [/kimi|moonshot/i, "Kimi"],
  [/glm/i, "GLM"], [/minimax/i, "MiniMax"], [/gpt|^o\d/i, "OpenAI"], [/claude/i, "Claude"], [/gemini/i, "Gemini"]];
const vendor = (m: string) => VENDORS.find(([re]) => re.test(m))?.[1] ?? "";
const shortName = (m: string) => m.replace(/^[^/]+\//, "");

/** Footer model picker: a list grouped by vendor; the pick is remembered in this
 *  browser, "set as default" is saved on the server for every device. */
function ModelMenu({ models, current, def, disabled, onPick, onDefault }: {
  models: string[]; current: string; def: string; disabled: boolean;
  onPick: (m: string) => void; onDefault: (m: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false); };
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", h);
    document.addEventListener("keydown", k);
    return () => { document.removeEventListener("mousedown", h); document.removeEventListener("keydown", k); };
  }, [open]);
  const groups = useMemo(() => {
    const g = new Map<string, string[]>();
    for (const m of models) { const v = vendor(m); g.set(v, [...(g.get(v) ?? []), m]); }
    return [...g.entries()];
  }, [models]);

  return (
    <div ref={root} className="relative ml-auto shrink-0">
      <button className="chat-model" disabled={disabled} aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen((o) => !o)} title={tr("切换模型", "Switch model")}>
        <span className="truncate">{shortName(current)}</span>
        <span className={`chat-model-caret ${open ? "open" : ""}`}>▾</span>
      </button>
      {open && (
        <div className="chat-model-pop" role="menu">
          {groups.map(([v, ms]) => (
            <div key={v || "other"} className="py-1 first:pt-0.5">
              {v && <div className="chat-model-group">{v}</div>}
              {ms.map((m) => (
                <div key={m} role="menuitemradio" aria-checked={m === current} tabIndex={0}
                  className={`chat-model-row ${m === current ? "on" : ""}`}
                  onClick={() => { onPick(m); setOpen(false); }}
                  onKeyDown={(e) => { if (e.key === "Enter") { onPick(m); setOpen(false); } }}>
                  <span className="chat-model-check">{m === current ? "✓" : ""}</span>
                  <span className="min-w-0 flex-1 truncate">{shortName(m)}</span>
                  {m === def
                    ? <span className="chat-model-def">{tr("默认", "Default")}</span>
                    : <button className="chat-model-setdef" onClick={(e) => { e.stopPropagation(); onDefault(m); }}>
                        {tr("设为默认", "Set default")}</button>}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
