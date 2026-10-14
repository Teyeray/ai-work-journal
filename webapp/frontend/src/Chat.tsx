import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { tr } from "./i18n";

/**
 * 问 AI: a side panel available on every tab. The backend runs a tool loop over
 * all journal data (progressive disclosure: overview → rows → full text); this
 * component streams its NDJSON events and renders the steps and the answer.
 */

export interface ChatCtx { tab?: string; day?: string; session_id?: number | null; view?: string }

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

interface Step { id: string; name: string; args: Record<string, unknown>; summary?: string | null; error?: string | null; ms?: number; done?: boolean }
interface Turn { turn: number; question: string; steps: Step[]; answer: string; error?: string }
interface Thread { id: number; title: string; updated_at: string }

const toolLabel = (name: string): string => ({
  search: tr("全局搜索", "Global search"), list_projects: tr("项目列表", "Project list"), range_summary: tr("区间统计", "Range summary"),
  get_day: tr("查看某天", "View a day"), get_windows: tr("窗口记录", "Window log"), find_sessions: tr("查找会话", "Find sessions"),
  read_session: tr("读会话", "Read session"), list_commits: tr("提交记录", "Commits"), screen_text: tr("屏幕 OCR", "Screen OCR"),
  feishu: tr("飞书记录", "Feishu records"), list_reports: tr("日报列表", "Report list"), read_report: tr("读日报", "Read report"),
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

export function ChatDock({ tab, day, onNavigate }: { tab: string; day: string; onNavigate: (href: string) => void }) {
  const [open, setOpen] = useState<boolean>(() => load("chat.open", false));
  const [wide, setWide] = useState<boolean>(() => load("chat.wide", false));
  const [status, setStatus] = useState<{ available: boolean; model: string } | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [showHist, setShowHist] = useState(false);
  const [tid, setTid] = useState<number | null>(() => load("chat.thread", null));
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
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

  const loadThreads = () => fetch("/api/v1/chat/threads").then((r) => r.json()).then((r) => setThreads(r.threads)).catch(() => {});
  useEffect(() => {
    if (!open) return;
    if (!status) fetch("/api/v1/chat/status").then((r) => r.json()).then(setStatus).catch(() => setStatus({ available: false, model: "" }));
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
        body: JSON.stringify({ text, thread_id: tid, context: { ...pageCtx } }),
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
      loadThreads();
    }
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
          <TurnView key={t.turn} t={t} live={busy && i === turns.length - 1} onRetry={() => send(t.question)} />
        ))}
      </div>

      <form className="chat-input" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <textarea ref={box} value={input} rows={Math.min(6, Math.max(1, input.split("\n").length))}
          placeholder={status && !status.available ? tr("LLM 未配置", "LLM not configured") : tr("问关于你工作的任何事…  Enter 发送,Shift+Enter 换行", "Ask anything about your work…  Enter to send, Shift+Enter for newline")}
          disabled={status ? !status.available : false}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(input); }
            if (e.key === "Escape") setOpen(false);
          }} />
        {busy
          ? <button type="button" className="chat-send stop" onClick={() => abort.current?.abort()} title={tr("停止", "Stop")}>■</button>
          : <button type="submit" className="chat-send" disabled={!input.trim()} title={tr("发送", "Send")}>↑</button>}
      </form>
      <div className="chat-foot">
        {pageCtx.tab && <span>{tr("上下文:", "Context: ")}{pageCtx.tab} · {pageCtx.day}{pageCtx.session_id ? ` · ${tr("会话", "session")} #${pageCtx.session_id}` : ""}</span>}
        {status?.model && <span className="ml-auto">{status.model}</span>}
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
      <div className="text-[15px] font-semibold text-neutral-800">{tr("问问你自己的工作", "Ask about your own work")}</div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-neutral-500">
        {tr("我能查到你的窗口记录、agent 会话原文、git 提交、项目设定、屏幕 OCR、飞书摘录和日报。先看概要,需要时再读原文。",
          "I can look up your window log, agent session transcripts, git commits, project settings, screen OCR, Feishu excerpts and reports. I start with summaries and read the full text when needed.")}
      </p>
      <div className="mt-4 flex flex-col gap-1.5">
        {suggest(tab).map((q) => (
          <button key={q} className="chat-suggest" onClick={() => onPick(q)}>{q}</button>
        ))}
      </div>
    </div>
  );
}

function TurnView({ t, live, onRetry }: { t: Turn; live: boolean; onRetry: () => void }) {
  const html = useMemo(() => md(t.answer), [t.answer]);
  const [copied, setCopied] = useState(false);
  const thinking = live && !t.answer && t.steps.every((s) => s.done);
  return (
    <div className="chat-turn">
      <div className="chat-q">{t.question}</div>
      {t.steps.length > 0 && <Steps steps={t.steps} live={live} />}
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

function Steps({ steps, live }: { steps: Step[]; live: boolean }) {
  const [open, setOpen] = useState(false);
  const show = open || live;
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
          <span className={`ml-auto shrink-0 ${s.error ? "text-red-500" : "text-neutral-500"}`}>{s.error ? tr("出错", "Error") : s.summary ?? ""}</span>
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
