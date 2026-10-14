import { useEffect, useMemo, useRef, useState } from "react";
import { DayPicker } from "./DayPicker";
import { setChatContext } from "./Chat";
import { api, fmtDur, fmtHM, projectColor } from "./api";
import type { SessionRow, SessionDetail, SessionEvent, Review } from "./api";
import { useSse } from "./sse";
import { label, locale, reason, tr } from "./i18n";

/**
 * 会话: what each agent session was about and how it went.
 *   left   sessions titled by your first instruction, with project, run time,
 *          prompts, tool calls; subagents fold under their parent
 *   right  the conversation (your instructions stand out, tool calls collapse
 *          into chips) and the quality review
 */

const spans = (): [number, string][] => [[1, tr("当天", "Day")], [3, tr("近 3 天", "Last 3 days")], [7, tr("近 7 天", "Last 7 days")]];
const WD = "日一二三四五六";
const dayHead = (d: string) => {
  const dt = new Date(d + "T12:00:00");
  return tr(`${+d.slice(5, 7)}月${+d.slice(8, 10)}日 周${WD[dt.getDay()]}`,
    dt.toLocaleDateString(locale(), { month: "short", day: "numeric", weekday: "short" }));
};
const projName = (p: string) => (p === "Unassigned" ? tr("未归属", "Unassigned") : label(p));
const agentName = (a: string) => (a === "claude-code" ? "claude" : a);
const tok = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`);

export function Sessions({ day, days, focusId }: { day: string; days: string[]; focusId?: { id: number } | null }) {
  const [anchor, setAnchor] = useState(day);
  const [span, setSpan] = useState(1);
  const [project, setProject] = useState("");
  const [agent, setAgent] = useState("");
  const [query, setQuery] = useState("");
  const [subs, setSubs] = useState(false);
  const [rows, setRows] = useState<SessionRow[]>([]);
  const [sel, setSel] = useState<number | null>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => setAnchor(day), [day]);
  // a link from 问 AI: jump to that session's day and open it, whatever the filters were
  const focusRef = useRef<number | null>(null);
  useEffect(() => {
    if (focusId == null) return;
    const id = focusId.id;
    focusRef.current = id;
    api.session(id).then((d) => {
      setProject(""); setAgent(""); setQuery(""); setSpan(1);
      setAnchor(d.start_at.slice(0, 10));
      setSel(id);
    }).catch(() => {});
  }, [focusId]);
  useEffect(() => { setChatContext({ session_id: sel }); }, [sel]);
  const load = () => {
    const p: Record<string, string | number> = { day: anchor, days: span, limit: 300 };
    if (agent) p.agent = agent;
    if (query.trim()) p.query = query.trim();
    if (subs) p.subagents = "true";
    setLoading(true);
    api.sessions(p).then((r) => {
      setRows(r.sessions);
      // keep the open session if it is still listed, else open the first one
      // default: the newest session you actually typed into (loop/self-wakeup runs are rarely what you want)
      const first = (r.sessions.find((x) => x.prompts > 0) ?? r.sessions[0])?.id ?? null;
      setSel((cur) => (cur != null && (cur === focusRef.current || r.sessions.some((x) => x.id === cur)) ? cur : first));
    }).catch(() => {}).finally(() => setLoading(false));
  };
  useEffect(() => { const t = setTimeout(load, query ? 250 : 0); return () => clearTimeout(t); }, [anchor, span, agent, query, subs]); // eslint-disable-line
  useSse(load);

  useEffect(() => {
    if (sel == null) { setDetail(null); return; }
    api.session(sel).then(setDetail).catch(() => setDetail(null));
  }, [sel]);

  const projects = useMemo(() => [...new Set(rows.map((r) => r.project))].sort(), [rows]);
  const shown = rows.filter((r) => !project || r.project === project);
  const pickProject = (v: string) => {
    setProject(v);
    const first = rows.find((r) => !v || r.project === v);
    if (first && !(rows.find((r) => r.id === sel && (!v || r.project === v)))) setSel(first.id);
  };
  const groups = useMemo(() => {
    const g: [string, SessionRow[]][] = [];
    for (const r of shown) {
      const d = (r.range?.last_at ?? r.start_at).slice(0, 10);
      if (g.length && g[g.length - 1][0] === d) g[g.length - 1][1].push(r); else g.push([d, [r]]);
    }
    return g;
  }, [shown]);
  const totals = shown.reduce((t, r) => ({ exec: t.exec + (r.range?.exec_sec ?? r.exec_sec), prompts: t.prompts + (r.range?.prompts ?? r.prompts) }), { exec: 0, prompts: 0 });
  const selRow = rows.find((r) => r.id === sel);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[26px] font-semibold tracking-tight text-neutral-900">{tr("会话", "Sessions")}</h1>
          <div className="text-[12.5px] text-neutral-400">
            {loading ? tr("加载中…", "Loading…") : tr(`${shown.length} 个会话 · 执行 ${fmtDur(totals.exec)}(累加) · 你发了 ${totals.prompts} 条指令`,
              `${shown.length} ${shown.length === 1 ? "session" : "sessions"} · ${fmtDur(totals.exec)} running (summed) · ${totals.prompts} ${totals.prompts === 1 ? "prompt" : "prompts"} from you`)}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <DayPicker value={anchor} days={days} onChange={setAnchor} />
          <div className="seg">
            {spans().map(([n, l]) => <button key={n} className={span === n ? "on" : ""} onClick={() => setSpan(n)}>{l}</button>)}
          </div>
          <select value={project} onChange={(e) => pickProject(e.target.value)} className="input h-8 w-[140px] text-[12.5px]">
            <option value="">{tr("全部项目", "All projects")}</option>
            {projects.map((p) => <option key={p} value={p}>{projName(p)}</option>)}
          </select>
          <select value={agent} onChange={(e) => setAgent(e.target.value)} className="input h-8 w-[100px] text-[12.5px]">
            <option value="">{tr("全部 agent", "All agents")}</option><option value="claude">claude</option><option value="pi">pi</option>
          </select>
          <label className="tl-check text-[12.5px] text-neutral-600"><input type="checkbox" checked={subs} onChange={(e) => setSubs(e.target.checked)} />{tr("子代理", "Subagents")}</label>
          <input value={query} onChange={(e) => setQuery(e.target.value)} className="input h-8 w-[200px] text-[12.5px]" placeholder={tr("搜索对话内容…", "Search conversations…")} />
        </div>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[460px_minmax(0,1fr)]">
        <div className="card thin-scroll overflow-y-auto xl:sticky xl:top-16 xl:max-h-[calc(100vh-150px)]">
          {groups.map(([d, rs]) => (
            <div key={d}>
              {span > 1 && <div className="sticky top-0 z-10 border-b border-neutral-100 bg-white/95 px-4 py-1.5 text-[11.5px] font-semibold text-neutral-500 backdrop-blur">{dayHead(d)}</div>}
              {rs.map((r) => <Row key={r.id} r={r} on={sel === r.id} onClick={() => setSel(r.id)} />)}
            </div>
          ))}
          {!shown.length && !loading && <div className="px-4 py-10 text-center text-[12.5px] text-neutral-400">{tr("这段时间没有会话", "No sessions in this period")}</div>}
        </div>
        <div className="min-w-0">
          {detail ? <Pane d={detail} onPick={setSel} range={selRow?.carried && selRow.range?.first_at ? selRow.range : null} /> : <div className="card py-24 text-center text-[13px] text-neutral-400">{tr("选择左侧的一个会话", "Select a session on the left")}</div>}
        </div>
      </div>
    </div>
  );
}

function Row({ r, on, onClick }: { r: SessionRow; on: boolean; onClick: () => void }) {
  // a session opened days ago and still going: describe it by what it did in this range
  const g = r.carried && r.range ? r.range : null;
  const title = g?.last_prompt ?? r.title;
  const s = g?.first_at ?? r.start_at, e = g?.last_at ?? r.end_at ?? r.start_at;
  const prompts = g ? g.prompts : r.prompts;
  return (
    <button onClick={onClick} className={`block w-full border-b border-neutral-50 px-4 py-2.5 text-left hover:bg-neutral-50 ${on ? "bg-[var(--accent-soft)]" : ""}`}>
      <div className="flex items-start gap-2">
        <i className="mt-1.5 inline-block h-2 w-2 shrink-0 rounded-[2px]" style={{ background: projectColor(r.project) }} />
        <div className="min-w-0 flex-1">
          <div className={`line-clamp-2 text-[13px] leading-snug ${title ? "text-neutral-900" : "italic text-neutral-400"}`}>
            {g?.last_prompt && <span className="mr-1 text-neutral-400">{tr("最近:", "Latest:")}</span>}
            {title ?? (r.subagent ? tr("子代理任务", "Subagent task") : tr("没有输入(自动会话)", "No input (automated session)"))}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[11.5px] tabular-nums text-neutral-400">
            <span>{fmtHM(s)}–{fmtHM(e)}</span>
            {g && <span className="rounded bg-amber-50 px-1 text-amber-700" title={tr(`会话开始于 ${r.start_at.slice(0, 16).replace("T", " ")};统计只算所选日期内`, `Session started ${r.start_at.slice(0, 16).replace("T", " ")}; stats count only the selected dates`)}>{tr(`自 ${r.start_at.slice(5, 10)} 起`, `since ${r.start_at.slice(5, 10)}`)}</span>}
            <span>{agentName(r.agent)}{r.subagent ? tr(" · 子代理", " · subagent") : ""}</span>
            <span className="text-neutral-600">{projName(r.project)}</span>
            <span>{tr("执行", "Ran")} {fmtDur(g ? g.exec_sec : r.exec_sec)}</span>
            {prompts > 0 && <span>{tr(`${prompts} 条指令`, `${prompts} ${prompts === 1 ? "prompt" : "prompts"}`)}</span>}
            {!g && r.tools > 0 && <span>{tr(`${r.tools} 次工具`, `${r.tools} ${r.tools === 1 ? "tool call" : "tool calls"}`)}</span>}
            {r.children.length > 0 && <span className="rounded bg-neutral-100 px-1 text-neutral-500">{tr(`+${r.children.length} 子代理`, `+${r.children.length} ${r.children.length === 1 ? "subagent" : "subagents"}`)}</span>}
            {r.status === "running" && <span className="rounded bg-green-50 px-1 text-green-700">{tr("运行中", "Running")}</span>}
          </div>
        </div>
      </div>
    </button>
  );
}

// ---------------- detail ----------------

type Item =
  | { k: "you"; e: SessionEvent }
  | { k: "ctx"; e: SessionEvent }
  | { k: "say"; e: SessionEvent }
  | { k: "tools"; at: string; names: Record<string, number>; errors: number };

function transcript(evs: SessionEvent[], subagent: boolean): Item[] {
  const out: Item[] = [];
  for (const e of evs) {
    const text = (e.text ?? "").trim();
    if (e.role === "user") {
      if (e.tool_results?.length) {
        const errs = e.tool_results.filter((t) => t.is_error).length;
        const last = out[out.length - 1];
        if (errs && last?.k === "tools") last.errors += errs;
        continue;
      }
      if (!text) continue;
      out.push({ k: !subagent && !text.startsWith("<") && text.length >= 5 ? "you" : "ctx", e });
    } else if (e.role === "assistant") {
      if (text) out.push({ k: "say", e });
      if (e.tools?.length) {
        const last = out[out.length - 1];
        const t = last?.k === "tools" ? last : (out.push({ k: "tools", at: e.at, names: {}, errors: 0 }), out[out.length - 1] as Extract<Item, { k: "tools" }>);
        for (const n of e.tools) t.names[n ?? "?"] = (t.names[n ?? "?"] ?? 0) + 1;
      }
    }
  }
  return out;
}

function Pane({ d, onPick, range }: { d: SessionDetail; onPick: (id: number) => void; range: SessionRow["range"] | null }) {
  const from = range?.first_at ?? null;
  const [tab, setTab] = useState<"chat" | "review">("chat");
  const [mine, setMine] = useState(false);
  const items = useMemo(() => transcript(d.events, d.subagent), [d]);
  const shown = mine ? items.filter((i) => i.k === "you") : items;
  const errors = items.reduce((n, i) => n + (i.k === "tools" ? i.errors : 0), 0);
  // a long-running session opens where the selected day begins, not at its first message
  const startIdx = from ? shown.findIndex((i) => (i.k === "tools" ? i.at : i.e.at) >= from) : -1;
  const [earlier, setEarlier] = useState(false);
  useEffect(() => { setEarlier(false); }, [d.id, from]);
  const hidden = startIdx > 0 && !earlier ? startIdx : 0;
  return (
    <div className="card">
      <div className="border-b border-neutral-100 px-6 pb-3 pt-4">
        <div className="flex items-start gap-3">
          <h2 className="min-w-0 flex-1 text-[17px] font-semibold leading-snug text-neutral-900">{d.title ?? tr("没有输入的会话", "Session with no input")}</h2>
          {d.status === "running" && <span className="shrink-0 rounded bg-green-50 px-1.5 text-[12px] text-green-700">{tr("运行中", "Running")}</span>}
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5 text-[12px]">
          <Chip><i className="mr-1 inline-block h-2 w-2 rounded-[2px]" style={{ background: projectColor(d.project) }} />{projName(d.project)}</Chip>
          <Chip>{agentName(d.agent)}{d.subagent ? tr(" 子代理", " subagent") : ""}{d.models.length ? ` · ${d.models.join(", ")}` : ""}</Chip>
          <Chip>{d.start_at.slice(5, 10).replace("-", "/")} {fmtHM(d.start_at)}–{fmtHM(d.end_at ?? d.start_at)}</Chip>
          <Chip>{tr("执行", "Ran")} {fmtDur(d.exec_sec)}</Chip>
          <Chip>{tr(`${d.prompts} 条指令 · ${d.tools} 次工具${errors ? ` · ${errors} 次出错` : ""}`, `${d.prompts} ${d.prompts === 1 ? "prompt" : "prompts"} · ${d.tools} ${d.tools === 1 ? "tool call" : "tool calls"}${errors ? ` · ${errors} ${errors === 1 ? "error" : "errors"}` : ""}`)}</Chip>
          {d.tokens > 0 && <Chip>{tok(d.tokens)} tokens</Chip>}
          {range && <span className="inline-flex items-center rounded-md bg-amber-50 px-2 py-0.5 text-amber-700">{tr(`所选日期:${fmtHM(range.first_at!)}–${fmtHM(range.last_at!)} · ${range.prompts} 条指令 · 执行 ${fmtDur(range.exec_sec)}`, `Selected dates: ${fmtHM(range.first_at!)}–${fmtHM(range.last_at!)} · ${range.prompts} ${range.prompts === 1 ? "prompt" : "prompts"} · ran ${fmtDur(range.exec_sec)}`)}</span>}
        </div>
        <div className="mt-1.5 truncate font-mono text-[11.5px] text-neutral-400" title={`${d.cwd} · ${tr("归类", "Attribution")}: ${reason(d.project_reason)}`}>{d.cwd}</div>
        {(d.children.length > 0 || d.parent_id) && (
          <div className="mt-2 flex flex-wrap gap-1.5 text-[12px]">
            {d.parent_id && <button className="link" onClick={() => onPick(d.parent_id!)}>{tr("← 父会话", "← Parent session")} #{d.parent_id}</button>}
            {d.children.map((c) => <button key={c} className="chip-x hover:bg-neutral-200" onClick={() => onPick(c)}>{tr("子代理", "Subagent")} #{c}</button>)}
          </div>
        )}
      </div>
      <div className="rail-tabs !static">
        <button className={tab === "chat" ? "on" : ""} onClick={() => setTab("chat")}>{tr("对话", "Conversation")}</button>
        <button className={tab === "review" ? "on" : ""} onClick={() => setTab("review")}>{tr("质量评审", "Quality review")}</button>
        {tab === "chat" && (
          <label className="tl-check ml-auto pb-2 text-[12.5px] text-neutral-600">
            <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} />{tr("只看我的指令", "My prompts only")}
          </label>
        )}
      </div>
      {tab === "chat" ? (
        <div className="space-y-2 px-6 py-4">
          {hidden > 0 && (
            <button className="block w-full rounded-md border border-dashed border-neutral-200 py-1.5 text-[12px] text-neutral-500 hover:bg-neutral-50"
              onClick={() => setEarlier(true)}>
              {tr(`↑ 显示更早的 ${hidden} 条(会话开始于 ${d.start_at.slice(5, 10)} ${fmtHM(d.start_at)})`, `↑ Show ${hidden} earlier (session started ${d.start_at.slice(5, 10)} ${fmtHM(d.start_at)})`)}
            </button>
          )}
          {shown.map((i, k) => k < hidden ? null : (
            <div key={k}>
              {k === startIdx && startIdx > 0 && <div className="mb-2 flex items-center gap-2 text-[11.5px] text-neutral-400"><span className="h-px flex-1 bg-neutral-200" />{tr(`${fmtHM(from!)} 起 · 所选日期`, `From ${fmtHM(from!)} · selected dates`)}<span className="h-px flex-1 bg-neutral-200" /></div>}
              <Bubble i={i} />
            </div>
          ))}
          {!shown.length && <div className="py-10 text-center text-[12.5px] text-neutral-400">{mine ? tr("这个会话里没有你输入的指令", "No prompts from you in this session") : tr("没有对话内容", "No conversation content")}</div>}
        </div>
      ) : <ReviewPanel d={d} />}
    </div>
  );
}

function Bubble({ i }: { i: Item }) {
  const [open, setOpen] = useState(false);
  if (i.k === "tools") {
    const entries = Object.entries(i.names).sort((a, b) => b[1] - a[1]);
    return (
      <div className="flex flex-wrap items-center gap-1 pl-3 text-[11.5px] text-neutral-400">
        <span className="tabular-nums">{fmtHM(i.at)}</span>
        {entries.map(([n, c]) => <span key={n} className="rounded bg-neutral-100 px-1.5 font-mono text-neutral-500">{n}{c > 1 ? ` ×${c}` : ""}</span>)}
        {i.errors > 0 && <span className="rounded bg-red-50 px-1.5 text-red-600">{tr(`${i.errors} 次出错`, `${i.errors} ${i.errors === 1 ? "error" : "errors"}`)}</span>}
      </div>
    );
  }
  const text = i.e.text ?? "";
  const long = text.length > 420;
  if (i.k === "ctx") {
    return (
      <button onClick={() => setOpen(!open)} className="block w-full rounded-md px-3 py-1 text-left text-[11.5px] text-neutral-400 hover:bg-neutral-50">
        <span className="tabular-nums">{fmtHM(i.e.at)}</span> {tr("注入的上下文 / 系统消息", "Injected context / system message")} {open ? "▾" : "▸"}
        {open && <div className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] text-neutral-500">{text}</div>}
      </button>
    );
  }
  const you = i.k === "you";
  return (
    <div className={`rounded-lg px-3.5 py-2.5 ${you ? "border-l-[3px] border-neutral-900 bg-neutral-50" : ""}`}>
      <div className="mb-0.5 text-[11.5px] tabular-nums text-neutral-400">{fmtHM(i.e.at)} · {you ? tr("你", "You") : "agent"}</div>
      <div className={`whitespace-pre-wrap break-words text-[13px] leading-relaxed ${you ? "font-medium text-neutral-900" : "text-neutral-600"}`}>
        {long && !open ? text.slice(0, 420) + "…" : text}
      </div>
      {long && <button className="link mt-1 text-[12px]" onClick={() => setOpen(!open)}>{open ? tr("收起", "Collapse") : tr("展开全文", "Show full text")}</button>}
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return <span className="inline-flex items-center rounded-md bg-neutral-100 px-2 py-0.5 text-neutral-600">{children}</span>;
}

// ---------------- review ----------------

type Dim = { key: keyof Review; label: string; max: number };
const promptDims = (): Dim[] => [
  { key: "prompt_goal", label: tr("目标明确", "Clear goal"), max: 25 },
  { key: "prompt_context", label: tr("上下文充分", "Enough context"), max: 25 },
  { key: "prompt_constraints", label: tr("约束清晰", "Clear constraints"), max: 25 },
  { key: "prompt_acceptance", label: tr("验收标准", "Acceptance criteria"), max: 25 },
];
const execDims = (): Dim[] => [
  { key: "exec_planning", label: tr("规划", "Planning"), max: 15 },
  { key: "exec_tools", label: tr("工具使用", "Tool use"), max: 20 },
  { key: "exec_error_recovery", label: tr("错误恢复", "Error recovery"), max: 20 },
  { key: "exec_verification", label: tr("验证", "Verification"), max: 30 },
  { key: "exec_efficiency", label: tr("效率", "Efficiency"), max: 15 },
];

function ReviewPanel({ d }: { d: SessionDetail }) {
  const [review, setReview] = useState<Review | null>(
    d.review && d.review.status === "analyzed" ? (d.review as unknown as Review) : null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [rating, setRating] = useState("");
  const [comment, setComment] = useState("");
  const [sent, setSent] = useState(false);
  useEffect(() => {
    setReview(d.review && d.review.status === "analyzed" ? (d.review as unknown as Review) : null);
    setErr(null); setSent(false);
  }, [d.id]); // eslint-disable-line

  const go = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await api.review(d.id);
      if (r.review) setReview(r.review);
      else if ((r as unknown as Review).status === "analyzed") setReview(r as unknown as Review);
      else setErr(r.message ?? tr("评审失败", "Review failed"));
    } catch (e) { setErr(String(e)); }
    setBusy(false);
  };
  const v = (k: keyof Review) => (review as unknown as Record<string, number | undefined> | null)?.[k as string];
  const sum = (dims: Dim[]) => dims.reduce((s, x) => s + (v(x.key) ?? 0), 0);
  return (
    <div className="px-6 py-4">
      <div className="flex items-center gap-3">
        <div className="text-[12.5px] text-neutral-500">
          {tr("按固定 rubric 打分:你的指令质量 4×25 分 + agent 执行 5 项共 100 分;结果按内容指纹缓存。", "Scored with a fixed rubric: prompt quality 4×25 points + agent execution 5 items for 100 points; results are cached by content fingerprint.")}
        </div>
        <button className="btn-primary ml-auto h-8 shrink-0 text-[12.5px]" disabled={busy || !d.review_available} onClick={go}>
          {busy ? tr("评审中…", "Reviewing…") : review ? tr("重新评审", "Re-review") : tr("开始评审", "Start review")}</button>
      </div>
      {!d.review_available && <div className="mt-2 text-[12.5px] text-amber-700">{tr("没有可用的 LLM(配置百炼密钥后即可评审)。", "No LLM available (configure a Bailian key to enable review).")}</div>}
      {err && <div className="mt-2 text-[12.5px] text-red-600">{err}</div>}
      {review && (
        <div className="mt-4 space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            {([[tr("你的指令", "Your prompts"), promptDims()], [tr("Agent 执行", "Agent execution"), execDims()]] as const).map(([title, dims]) => (
              <div key={title} className="rounded-lg bg-neutral-50 px-4 py-3">
                <div className="flex items-baseline justify-between">
                  <span className="text-[13px] font-semibold text-neutral-800">{title}</span>
                  <span className="text-[18px] font-semibold tabular-nums text-neutral-900">{sum(dims as Dim[])}<span className="text-[12px] font-normal text-neutral-400">/100</span></span>
                </div>
                <div className="mt-2 space-y-1.5">
                  {dims.map((x) => {
                    const val = v(x.key);
                    return (
                      <div key={x.key as string} className="grid grid-cols-[72px_1fr_44px] items-center gap-2 text-[12px]">
                        <span className="text-neutral-500">{x.label}</span>
                        <div className="h-1.5 overflow-hidden rounded-full bg-neutral-200">
                          <div className="h-full rounded-full bg-[#2a78d6]" style={{ width: `${((val ?? 0) / x.max) * 100}%` }} />
                        </div>
                        <span className="text-right tabular-nums text-neutral-600">{val ?? "—"}/{x.max}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
          {review.summary && <div className="text-[13px] leading-relaxed text-neutral-700">{review.summary}</div>}
          {review.suggestions?.length ? (
            <div>
              <div className="label mb-2">{tr("改进建议", "Suggestions")}</div>
              <ol className="space-y-1.5">
                {review.suggestions.map((s, k) => (
                  <li key={k} className="group flex items-start gap-2 rounded-md px-2 py-1 text-[13px] leading-relaxed hover:bg-neutral-50">
                    <span className="tabular-nums text-neutral-400">{k + 1}.</span>
                    <span className="flex-1 text-neutral-700">{s}</span>
                    <button className="btn h-6 shrink-0 px-2 text-[11px] opacity-0 group-hover:opacity-100" onClick={() => navigator.clipboard?.writeText(s)}>{tr("复制", "Copy")}</button>
                  </li>
                ))}
              </ol>
            </div>
          ) : null}
          <div className="flex items-center gap-2 border-t border-neutral-100 pt-3">
            <select value={rating} onChange={(e) => setRating(e.target.value)} className="input h-8 w-32 text-[12.5px]">
              <option value="">{tr("这次评审…", "This review…")}</option>
              <option value="helpful">{tr("有帮助", "Helpful")}</option><option value="partially">{tr("部分有用", "Partly useful")}</option>
              <option value="not_helpful">{tr("没帮助", "Not helpful")}</option><option value="wrong">{tr("不准确", "Inaccurate")}</option>
            </select>
            <input value={comment} onChange={(e) => setComment(e.target.value)} className="input h-8 flex-1 text-[12.5px]" placeholder={tr("一句话说明(可选)", "One-line note (optional)")} />
            <button className="btn h-8 text-[12.5px]" disabled={!rating || sent}
              onClick={async () => { await api.feedback({ session_id: d.id, rating, comment }); setSent(true); }}>{sent ? tr("已提交", "Submitted") : tr("提交反馈", "Submit feedback")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
