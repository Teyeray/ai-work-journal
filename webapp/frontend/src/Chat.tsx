import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";

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

const TOOL_LABEL: Record<string, string> = {
  search: "全局搜索", list_projects: "项目列表", range_summary: "区间统计", get_day: "查看某天",
  get_windows: "窗口记录", find_sessions: "查找会话", read_session: "读会话", list_commits: "提交记录",
  screen_text: "屏幕 OCR", feishu: "飞书记录", list_reports: "日报列表", read_report: "读日报",
};

const SUGGEST: Record<string, string[]> = {
  today: ["今天我主要在干什么?时间都去哪了", "今天哪段时间切换最频繁,在切什么", "今天 agent 替我做了哪些事"],
  analytics: ["上周和这周相比,各项目投入有什么变化", "我一般几点效率最高", "最近哪个项目被冷落了"],
  sessions: ["这个会话做了什么,结果如何", "最近一周我给 agent 的指令有什么共同问题", "找一下我让 agent 部署上线的会话"],
  projects: ["每个项目最近的进展和卡点", "paperfacts 最近两周做了哪些功能", "哪些项目我花了时间但没有提交"],
  reports: ["把上周的周报压缩成三句话", "日报里反复出现的待办有哪些", "对比这周和上周的日报"],
};

const TAB_NAME: Record<string, string> = { today: "今日", analytics: "分析", sessions: "会话", projects: "项目", reports: "日报" };

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
    setChatContext({ tab: TAB_NAME[tab] ?? tab, day, ...(tab === "sessions" ? {} : { session_id: null }) });
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
      if (!r.ok || !r.body) throw new Error(r.status === 503 ? "LLM 未配置(需要 ~/.config/ai-work-journal/.env 里的百炼 key)" : `HTTP ${r.status}`);
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
      else patchLast((t) => ({ ...t, error: "已停止" }));
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
      <button className="chat-fab" onClick={() => setOpen(true)} title="问 AI (Ctrl/⌘ + J)">
        <Spark /> 问 AI <kbd>⌘J</kbd>
      </button>
    );
  }

  return (
    <section className={`chat-panel ${wide ? "wide" : ""}`} aria-label="问 AI">
      <header className="flex h-11 shrink-0 items-center gap-1.5 border-b border-neutral-200 px-3">
        <Spark />
        <div className="min-w-0 flex-1 truncate text-[13px] font-semibold text-neutral-800">{title || "问 AI"}</div>
        <button className="chat-icon" title="历史对话" onClick={() => setShowHist((v) => !v)}>历史</button>
        <button className="chat-icon" title="新对话" onClick={newChat} disabled={busy}>新对话</button>
        <button className="chat-icon" title={wide ? "收窄" : "加宽"} onClick={() => setWide((w) => !w)}>{wide ? "⇥" : "⇤"}</button>
        <button className="chat-icon" title="关闭 (⌘J)" onClick={() => setOpen(false)}>✕</button>
      </header>

      {showHist && (
        <div className="chat-hist">
          {threads.length === 0 && <div className="px-3 py-4 text-[12px] text-neutral-400">还没有对话</div>}
          {threads.map((t) => (
            <div key={t.id} className={`chat-hist-row ${t.id === tid ? "on" : ""}`}>
              <button className="min-w-0 flex-1 truncate text-left" onClick={() => pick(t.id)}>{t.title || "(无标题)"}</button>
              <span className="shrink-0 text-[11px] text-neutral-400">{new Date(t.updated_at).toLocaleDateString("sv").slice(5)}</span>
              <button className="chat-del" title="删除" onClick={() => remove(t.id)}>✕</button>
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
          placeholder={status && !status.available ? "LLM 未配置" : "问关于你工作的任何事…  Enter 发送,Shift+Enter 换行"}
          disabled={status ? !status.available : false}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(input); }
            if (e.key === "Escape") setOpen(false);
          }} />
        {busy
          ? <button type="button" className="chat-send stop" onClick={() => abort.current?.abort()} title="停止">■</button>
          : <button type="submit" className="chat-send" disabled={!input.trim()} title="发送">↑</button>}
      </form>
      <div className="chat-foot">
        {pageCtx.tab && <span>上下文:{pageCtx.tab} · {pageCtx.day}{pageCtx.session_id ? ` · 会话 #${pageCtx.session_id}` : ""}</span>}
        {status?.model && <span className="ml-auto">{status.model}</span>}
      </div>
    </section>
  );
}

function Empty({ status, tab, onPick }: { status: { available: boolean } | null; tab: string; onPick: (q: string) => void }) {
  if (status && !status.available) {
    return <div className="px-2 py-10 text-center text-[12.5px] leading-relaxed text-neutral-500">
      LLM 未配置。在 <code>~/.config/ai-work-journal/.env</code> 写入 BAILIAN_API_KEY / BAILIAN_BASE_URL 后重启看板。</div>;
  }
  return (
    <div className="px-1 pt-6">
      <div className="text-[15px] font-semibold text-neutral-800">问问你自己的工作</div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-neutral-500">
        我能查到你的窗口记录、agent 会话原文、git 提交、项目设定、屏幕 OCR、飞书摘录和日报。先看概要,需要时再读原文。
      </p>
      <div className="mt-4 flex flex-col gap-1.5">
        {(SUGGEST[tab] ?? SUGGEST.today).map((q) => (
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
          <span>{t.error === "已停止" ? "已停止" : `出错:${t.error}`}</span>
          {!live && <button className="link" onClick={onRetry}>重试</button>}
        </div>
      )}
      {!live && t.answer && (
        <button className="chat-copy" onClick={() => { navigator.clipboard?.writeText(t.answer).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200); }); }}>
          {copied ? "已复制" : "复制"}
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
        {live && steps.some((s) => !s.done) ? "正在查询…" : `查了 ${n} 处数据`}
        {!show && <span className="truncate text-neutral-400"> · {[...new Set(steps.map((s) => TOOL_LABEL[s.name] ?? s.name))].join("、")}</span>}
      </button>
      {show && steps.map((s) => (
        <div key={s.id} className="chat-step">
          <span className={`chat-dot ${s.done ? (s.error ? "err" : "ok") : "run"}`} />
          <span className="font-medium text-neutral-700">{TOOL_LABEL[s.name] ?? s.name}</span>
          <span className="truncate text-neutral-400">{argText(s.args)}</span>
          <span className={`ml-auto shrink-0 ${s.error ? "text-red-500" : "text-neutral-500"}`}>{s.error ? "出错" : s.summary ?? ""}</span>
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
