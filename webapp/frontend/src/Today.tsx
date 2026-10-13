import { useEffect, useMemo, useState } from "react";
import { DayPicker } from "./DayPicker";
import { api, fmtDur, fmtHM, isCategory, projectColor } from "./api";
import type { ScreenStatus,
  AgentLane, Episode, CommitItem, Interval, Override, Timeline, TodayData,
} from "./api";
import { ActivityTimeline, endM, episodeKey, episodeTitle, startM, type Selection } from "./Timeline";
import { useSse } from "./sse";
import { dayVisits } from "./visits";
import { Flow } from "./Flow";

type RailTab = "story" | "flow" | "detail";

/** Everything that happened that day: observed windows ∪ agent execution, padded to half hours. */
function dayExtent(tl: Timeline, withAgents = true): [number, number] {
  const d = tl.day_key;
  let lo = Infinity, hi = -Infinity;
  if (tl.observed_span) { lo = startM(tl.observed_span.start, d); hi = endM(tl.observed_span.end, d); }
  if (withAgents || lo === Infinity) for (const a of tl.agent) for (const g of a.segments) {
    lo = Math.min(lo, startM(g.start, d)); hi = Math.max(hi, endM(g.end, d));
  }
  if (lo === Infinity) return [480, 1200];
  return [Math.max(0, Math.floor((lo - 10) / 30) * 30), Math.min(1440, Math.ceil((hi + 10) / 30) * 30)];
}

function episodeView(e: Episode, d: string): [number, number] {
  const s = startM(e.start, d), en = endM(e.end, d), pad = Math.max(1, (en - s) * 0.05);
  return [Math.max(0, s - pad), Math.min(1440, Math.max(en + pad, s + 10))];
}

const KIND_ICON: Record<string, string> = {
  focus: "◎", overlap: "⇄", night: "☾", unknown: "?",
  projects: "▤", outcomes: "⎇", corrections: "✎",
};

export function Today({ day, days, onDayChange, goTab }: {
  day: string;
  days: string[];
  onDayChange: (d: string) => void;
  goTab: (t: string, day?: string) => void;
}) {
  const [tl, setTl] = useState<Timeline | null>(null);
  const [today, setToday] = useState<TodayData | null>(null);
  const [sel, setSel] = useState<Selection | null>(null);
  const [ovs, setOvs] = useState<Override[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [view, setView] = useState<[number, number]>([480, 1200]);
  const [viewDay, setViewDay] = useState<string | null>(null);
  const [hoverVisit, setHoverVisit] = useState<number | null>(null);
  const [focusVisit, setFocusVisit] = useState<number | null>(null);
  const [tab, setTab] = useState<RailTab>("story");
  const visits = useMemo(() => (tl ? dayVisits(tl) : []), [tl]);
  const extent = useMemo<[number, number]>(() => (tl ? dayExtent(tl) : [480, 1200]), [tl]);
  // default view: when you were at the computer (overnight agents stay reachable via 全天)
  const workday = useMemo<[number, number]>(() => (tl ? dayExtent(tl, false) : [480, 1200]), [tl]);

  // new day → workday view (live refreshes keep the user's zoom)
  if (tl && tl.day_key === day && viewDay !== day) {
    setViewDay(day);
    setView(dayExtent(tl, false));
    setFocusVisit(null);
  }

  const select = (s: Selection | null) => {
    setSel(s);
    if (s?.kind === "episode" && tl) {
      const ep = tl.episodes.find((e) => episodeKey(e) === s.key);
      if (ep) setView(episodeView(ep, day));
    }
    setTab(s ? "detail" : "story");
  };

  const load = (keepSel = false) => {
    api.timeline(day).then(setTl).catch((e) => setErr(String(e)));
    api.today(day).then(setToday).catch(() => {});
    api.overrides(day).then((r) => setOvs(r.overrides)).catch(() => {});
    if (!keepSel) { setSel(null); setTab("story"); }
  };
  useEffect(() => load(), [day]);
  useSse((msg) => { if (msg.affected_days.includes(day)) load(true); });

  const activeOv = ovs.filter((o) => !o.superseded_by && o.field !== "__revoke__");
  const isToday = day === new Date().toLocaleDateString("sv");
  const nPrompts = tl?.evidence.filter((e) => e.kind === "agent_user").length ?? 0;
  const m = tl?.metrics;

  return (
    <div className="mx-auto max-w-[2200px] px-6 pb-10 pt-5">
      {/* header */}
      <div className="mb-3 flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <h1 className="text-[26px] font-semibold leading-tight tracking-tight text-neutral-900">{fmtDate(day)}</h1>
            <div className="mt-0.5 text-[12.5px] text-neutral-400">
              {tl?.observed_span
                ? `观测 ${fmtHM(tl.observed_span.start)}–${fmtHM(tl.observed_span.end)} · ${tl.episodes.length} 个工作段`
                : "当日无窗口观察"}
              {err && <span className="ml-2 text-red-600">{err}</span>}
            </div>
          </div>
          <div className="mb-0.5">
            <DayPicker value={day} days={days} onChange={onDayChange} hotkeys />
          </div>
        </div>
        <div className="flex flex-wrap gap-7">
          <Kpi v={fmtDur(m?.human_active_sec ?? 0)} l="你在电脑前活跃" />
          <Kpi v={fmtDur(m?.agent_exec_union_sec ?? 0)} l={`Agent 在跑 · 峰值 ${m?.agent_peak_parallel ?? 0} 个并行`}
            title={`各会话执行时长相加为 ${fmtDur(m?.agent_exec_sec ?? 0)};这里按时间去重`} />
          <Kpi v={`${tl ? tl.agent.filter((a) => !a.subagent).length : 0}`} l={`会话${tl && tl.agent.some((a) => a.subagent) ? ` + ${tl.agent.filter((a) => a.subagent).length} 子代理` : ""}`} />
          <Kpi v={`${nPrompts}`} l="你发的 Prompt" />
          <Kpi v={`${today?.outcomes.commits.length ?? 0}`} l="提交" />
          {activeOv.length > 0 && <Kpi v={`${activeOv.length}`} l="人工修正" />}
        </div>
      </div>

      {/* insights first: the day's conclusions above the fold */}
      {today && today.insights.length > 0 && (
        <div className="mb-4 grid gap-2.5 md:grid-cols-2 xl:grid-cols-4">
          {today.insights.slice(0, 4).map((i, k) => (
            <div key={k} className="card flex gap-2.5 px-3.5 py-2.5">
              <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-neutral-100 text-[11px] text-neutral-500">
                {KIND_ICON[i.kind] ?? "•"}
              </span>
              <div className="min-w-0">
                <div className="truncate text-[13px] font-semibold text-neutral-900">{i.title}</div>
                <div className="mt-0.5 line-clamp-2 text-[12px] leading-snug text-neutral-500">{i.detail}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_380px] 2xl:grid-cols-[minmax(0,1fr)_420px]">
        <div className="min-w-0 space-y-4">
          <div className="card px-5 pb-4 pt-4">
            {tl ? (
              <ActivityTimeline tl={tl} visits={visits} view={view} onView={setView} extent={extent} workday={workday}
                selection={sel} onSelect={select} isToday={isToday}
                hoverVisit={hoverVisit} onHoverVisit={setHoverVisit}
                onPickVisit={(i) => { setFocusVisit(i); setTab("flow"); }} />
            ) : (
              <div className="py-24 text-center text-[13px] text-neutral-400">加载中…</div>
            )}
          </div>
          <div className="grid items-start gap-4 lg:grid-cols-3">
            <AllocationCard today={today} />
            <OutcomesCard today={today} />
            <QualityCard day={day} today={today} activeOv={activeOv.length} observedSpan={tl?.observed_span ?? null} />
          </div>
        </div>

        <aside className="card thin-scroll xl:sticky xl:top-16 xl:max-h-[calc(100vh-80px)] xl:overflow-y-auto">
          {tl && <>
            <div className="rail-tabs">
              {([["story", "今天做了什么"], ["flow", "活动流"], ["detail", "详情"]] as [RailTab, string][]).map(([k, l]) => (
                <button key={k} className={tab === k ? "on" : ""} disabled={k === "detail" && !sel}
                  onClick={() => setTab(k)}>{l}</button>
              ))}
            </div>
            {tab === "flow" ? (
              <Flow tl={tl} visits={visits} range={view} hoverVisit={hoverVisit}
                onHoverVisit={setHoverVisit} focusVisit={focusVisit} />
            ) : (
              <Rail tl={tl} sel={tab === "detail" ? sel : null} setSel={select} day={day} overrides={ovs}
                onChanged={() => load(true)} goTab={goTab} />
            )}
          </>}
        </aside>
      </div>
    </div>
  );
}

function Kpi({ v, l, title }: { v: string; l: string; title?: string }) {
  return (
    <div title={title}>
      <div className="text-[22px] font-semibold leading-tight tracking-tight tabular-nums text-neutral-900">{v}</div>
      <div className="text-[12px] text-neutral-400">{l}</div>
    </div>
  );
}

// ================= right rail =================

function Rail({ tl, sel, setSel, day, overrides, onChanged, goTab }: {
  tl: Timeline; sel: Selection | null; setSel: (s: Selection | null) => void; day: string;
  overrides: Override[]; onChanged: () => void; goTab: (t: string, day?: string) => void;
}) {
  const back = <button className="link mb-3 text-[12.5px]" onClick={() => setSel(null)}>← 取消选择</button>;
  const selEp = sel?.kind === "episode" ? tl.episodes.find((e) => episodeKey(e) === sel.key) : undefined;
  if (selEp) {
    return <div className="px-4 py-4">{back}
      <EpisodeDetail ep={selEp} tl={tl} day={day} overrides={overrides}
        onChanged={onChanged} goTab={goTab} /></div>;
  }
  if (sel?.kind === "unknown") {
    return <div className="px-4 py-4">{back}<UnknownDetail interval={sel.interval} day={day} onChanged={onChanged} /></div>;
  }
  if (sel?.kind === "session") {
    return <div className="px-4 py-4">{back}<SessionDetail session={sel.session} goTab={goTab} day={day} /></div>;
  }
  return (
    <div>
      <div className="px-4 pb-1 pt-3 text-[12px] text-neutral-400">{tl.episodes.length} 个工作段 · 点击在时间轴上放大</div>
      <div className="px-2 py-1.5">
        {tl.episodes.map((e) => <StoryItem key={episodeKey(e)} ep={e} onClick={() => setSel({ kind: "episode", key: episodeKey(e) })} />)}
        {!tl.episodes.length && (
          <div className="px-2 py-6 text-[12.5px] leading-relaxed text-neutral-400">
            {tl.has_observation ? "当日没有足够的前台活动形成工作段。" : "当日没有前台窗口观察,无法划分工作段。可在时间轴上框选时段查看 Agent 执行与证据。"}
          </div>
        )}
      </div>
    </div>
  );
}

function StoryItem({ ep, onClick }: { ep: Episode; onClick: () => void }) {
  const ag = Object.entries(ep.agents_by_project);
  return (
    <button className="grid w-full grid-cols-[48px_1fr] gap-2.5 rounded-lg px-2 py-2 text-left hover:bg-neutral-50" onClick={onClick}>
      <div className="pt-0.5 text-[11.5px] leading-snug tabular-nums text-neutral-400">{fmtHM(ep.start)}<br />{fmtHM(ep.end)}</div>
      <div className="min-w-0">
        <div className="flex items-center gap-1.5 text-[13.5px] font-semibold text-neutral-900">
          <Dot c={projectColor(ep.label)} />{episodeTitle(ep)}
        </div>
        {ep.summary && <div className="mt-0.5 line-clamp-2 text-[12.5px] leading-snug text-neutral-500">{ep.summary}</div>}
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          <Chip>{fmtDur(ep.human_sec)}</Chip>
          {ep.switches > 10 && <Chip>切换 {ep.switches}</Chip>}
          {ag.length > 0 && <Chip>{ag.map(([p, n]) => `${p}×${n}`).join(" ")}</Chip>}
          {ep.commits.length > 0 && <Chip dark>{ep.commits.length} 提交</Chip>}
        </div>
      </div>
    </button>
  );
}

function EpisodeDetail({ ep, tl, day, overrides, onChanged, goTab }: {
  ep: Episode; tl: Timeline; day: string; overrides: Override[];
  onChanged: () => void; goTab: (t: string, day?: string) => void;
}) {
  const [project, setProject] = useState(ep.label);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => { setProject(ep.label); setMsg(null); }, [ep.start, ep.label]);

  const total = ep.mix.reduce((s, m) => s + m.sec, 0) || 1;
  // every block overlapping the episode (one may start before the first slot)
  const blocks = tl.blocks.filter((b) => b.end > ep.start && b.start < ep.end);
  const myOv = overrides.filter((o) => !o.superseded_by && o.field === "project" &&
    o.anchor_start >= ep.start && o.anchor_start < ep.end);

  const relabel = async () => {
    const v = project.trim();
    setBusy(true);
    try {
      const targets = blocks.filter((b) => b.project !== v);
      for (const b of targets) {
        await api.addOverride({ day_key: day, anchor_start: b.start, field: "project", value: v,
          note: `episode ${fmtHM(ep.start)}–${fmtHM(ep.end)}` });
      }
      setMsg(`已把 ${targets.length} 个窗口块改为 ${v}(追加式记录,可撤销)`);
      onChanged();
    } catch (e) { setMsg(`失败:${e}`); }
    setBusy(false);
  };

  return (
    <div>
      <h3 className="flex items-center gap-2 text-[18px] font-semibold text-neutral-900">
        <Dot c={projectColor(ep.label)} size={10} />{episodeTitle(ep)}
      </h3>
      <div className="mt-0.5 text-[13px] tabular-nums text-neutral-500">
        {fmtHM(ep.start)}–{fmtHM(ep.end)} · 跨度 {fmtDur((endM(ep.end, day) - startM(ep.start, day)) * 60)}
      </div>
      <div className="mt-3.5 grid grid-cols-3 gap-2">
        <Stat v={fmtDur(ep.human_sec)} l="人类活跃" />
        <Stat v={`${Math.round(ep.share * 100)}%`} l="主项目占比" />
        <Stat v={`${ep.switches}`} l="窗口切换" />
      </div>

      <Section title="时间构成">
        <div className="mb-1.5 flex h-2 overflow-hidden rounded-full">
          {ep.mix.map((m) => <div key={m.label} style={{ width: `${(m.sec / total) * 100}%`, background: projectColor(m.label) }} />)}
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[12px] tabular-nums text-neutral-500">
          {ep.mix.map((m) => <span key={m.label} className="flex items-center gap-1"><Dot c={projectColor(m.label)} />{m.label} {fmtDur(m.sec)}</span>)}
        </div>
      </Section>

      {ep.commits.length > 0 && (
        <Section title={`成果 · 提交 ${ep.commits.length}`}>
          {ep.commits.map((c, i) => (
            <div key={i} className="flex gap-2 py-1 text-[12.5px]">
              <span className="w-10 shrink-0 tabular-nums text-neutral-400">{fmtHM(c.at)}</span>
              <span className="text-neutral-700">{c.subject}</span>
            </div>
          ))}
        </Section>
      )}

      {ep.sessions.length > 0 && (
        <Section title={`同时段 Agent 会话 · ${ep.n_sessions}`}>
          {ep.sessions.map((s) => (
            <button key={s.id} className="flex w-full items-center gap-2 border-b border-neutral-50 py-1.5 text-left text-[12.5px] last:border-0 hover:bg-neutral-50"
              onClick={() => goTab("sessions", day)}>
              <Dot c={projectColor(s.project)} />
              <span className="text-neutral-700">{s.agent === "claude-code" ? "claude" : s.agent} · {s.project}</span>
              <span className="text-neutral-400">#{s.id}</span>
              <span className="ml-auto tabular-nums text-neutral-400">本段执行 {fmtDur(s.exec_sec)}</span>
            </button>
          ))}
        </Section>
      )}

      {ep.prompts.length > 0 && (
        <Section title={`你给 Agent 的指令 · ${ep.n_prompts}`}>
          {ep.prompts.slice(0, 5).map((p, i) => (
            <div key={i} className="mb-1.5 line-clamp-3 rounded-r-md border-l-[3px] border-neutral-300 bg-neutral-50 px-2.5 py-1.5 text-[12.5px] leading-snug text-neutral-600">
              <b className="mr-1 font-semibold tabular-nums text-neutral-800">{fmtHM(p.at)}</b>{p.text}
            </div>
          ))}
        </Section>
      )}

      {ep.titles.length > 0 && (
        <Section title="主要窗口">
          {ep.titles.map((t, i) => <div key={i} className="truncate border-b border-neutral-50 py-1 text-[12.5px] text-neutral-600 last:border-0" title={t}>{t}</div>)}
        </Section>
      )}

      {!ep.is_category && ep.share < 0.4 && (
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] leading-relaxed text-amber-800">
          主项目只占 {Math.round(ep.share * 100)}%,这一段比较碎,归属可能不准。
        </div>
      )}

      <Section title="修正归属(追加式,可撤销)">
        <div className="flex gap-2">
          <input value={project} onChange={(e) => setProject(e.target.value)} className="input flex-1" placeholder="项目名" />
          <button className="btn-primary" disabled={busy || !project.trim() || !blocks.some((b) => b.project !== project.trim())}
            onClick={relabel}>整段改为</button>
        </div>
        {msg && <div className="mt-1.5 text-[11.5px] text-green-700">{msg}</div>}
        {myOv.map((o) => (
          <div key={o.id} className="mt-1.5 flex items-center gap-2 text-[11.5px] text-neutral-500">
            <span>{fmtHM(o.anchor_start)} → {o.value}(原 {o.original_value})</span>
            <button className="link" onClick={async () => { await api.revokeOverride(o.id); onChanged(); }}>撤销</button>
          </div>
        ))}
      </Section>
    </div>
  );
}

function UnknownDetail({ interval, day, onChanged }: {
  interval: Interval; day: string; onChanged: () => void;
}) {
  const [project, setProject] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const dur = (endM(interval.end, day) - startM(interval.start, day)) * 60;
  return (
    <div>
      <h3 className="text-[18px] font-semibold text-amber-600">未知区间</h3>
      <div className="mt-1 text-[12.5px] leading-relaxed text-neutral-500">
        {fmtHM(interval.start)}–{fmtHM(interval.end)} · {fmtDur(dur)}。观察期内没有窗口数据(虚拟桌面、全屏或采集缺口)。
      </div>
      <div className="mt-3 flex gap-2">
        <input value={project} onChange={(e) => setProject(e.target.value)}
          className="input flex-1" placeholder="标注为某个项目…" />
        <button className="btn-primary" disabled={busy || !project.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              await api.addOverride({
                day_key: day, anchor_start: interval.start, field: "project",
                value: project.trim(), note: "unknown interval annotation",
              });
              setMsg("已标注");
              onChanged();
            } catch (e) { setMsg(`失败:${e}`); }
            setBusy(false);
          }}>标注</button>
      </div>
      {msg && <div className="mt-1.5 text-[11.5px] text-green-700">{msg}</div>}
    </div>
  );
}

function SessionDetail({ session, goTab, day }: {
  session: AgentLane; goTab: (t: string, day?: string) => void; day: string;
}) {
  const lifecycle = (endM(session.end, day) - startM(session.start, day)) * 60;
  return (
    <div>
      <h3 className="flex items-center gap-2 text-[18px] font-semibold text-neutral-900">
        <Dot c={projectColor(session.project)} size={10} />
        {session.agent === "claude-code" ? "claude" : session.agent} · {session.project}
        <span className="text-[13px] font-normal text-neutral-400">#{session.id}</span>
      </h3>
      <div className="mt-0.5 text-[13px] tabular-nums text-neutral-500">
        {fmtHM(session.start)}–{fmtHM(session.end)} · {session.status === "running" ? "运行中" : "已结束"}
        {session.subagent && " · 子代理"}
      </div>
      <div className="mt-3.5 grid grid-cols-3 gap-2">
        <Stat v={fmtDur(session.exec_sec)} l="执行" />
        <Stat v={fmtDur(Math.max(0, lifecycle - session.exec_sec))} l="等待/挂起" />
        <Stat v={`${session.n_user}`} l="用户输入" />
      </div>
      <Section title={`执行段 · ${session.segments.length}(10 分钟内事件聚为一段)`}>
        {session.segments.map((s, i) => (
          <div key={i} className="flex gap-3 border-b border-neutral-50 py-1 text-[12.5px] tabular-nums last:border-0">
            <span className="text-neutral-600">{fmtHM(s.start)}–{fmtHM(s.end)}</span>
            <span className="text-neutral-400">{fmtDur((endM(s.end, day) - startM(s.start, day)) * 60)}</span>
          </div>
        ))}
      </Section>
      <button className="btn mt-4" onClick={() => goTab("sessions", day)}>在会话页查看完整对话与评审 →</button>
    </div>
  );
}

// ================= cards below the timeline =================

function AllocationCard({ today }: { today: TodayData | null }) {
  if (!today) return null;
  return (
    <div className="card px-4 py-3.5">
      <h3 className="mb-2.5 text-[14px] font-semibold text-neutral-900">时间分配</h3>
      <StackedBar allocation={today.allocation} />
      <div className="label mb-1.5 mt-4">人类 · 按项目</div>
      <AllocList allocation={today.allocation} />
      <div className="label mb-1.5 mt-4">Agent 执行 · 按项目(不计入人类时间)</div>
      <AllocList allocation={today.agent_allocation} />
    </div>
  );
}

function StackedBar({ allocation }: { allocation: Record<string, number> }) {
  const items = Object.entries(allocation).sort((a, b) => b[1] - a[1]);
  const total = items.reduce((s, [, v]) => s + v, 0);
  if (!total) return <div className="text-[12px] text-neutral-400">无观察数据</div>;
  return (
    <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-neutral-100">
      {items.map(([name, sec]) => (
        <div key={name} title={`${name} ${fmtDur(sec)}`}
          style={{ width: `${(sec / total) * 100}%`, background: projectColor(name) }} />
      ))}
    </div>
  );
}

function AllocList({ allocation }: { allocation: Record<string, number> }) {
  const items = Object.entries(allocation).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const total = items.reduce((s, [, v]) => s + v, 0);
  if (!items.length) return <div className="text-[12px] text-neutral-400">无</div>;
  return (
    <ul className="space-y-1">
      {items.map(([name, sec]) => (
        <li key={name} className="flex items-center gap-2 text-[12.5px]">
          <Dot c={projectColor(name)} />
          <span className={`min-w-0 flex-1 truncate ${isCategory(name) ? "text-neutral-400" : "text-neutral-700"}`}>{name}</span>
          <span className="tabular-nums text-neutral-400">{Math.round((sec / total) * 100)}%</span>
          <span className="w-14 text-right tabular-nums text-neutral-600">{fmtDur(sec)}</span>
        </li>
      ))}
    </ul>
  );
}

function OutcomesCard({ today }: { today: TodayData | null }) {
  if (!today) return null;
  const { commits } = today.outcomes;
  return (
    <div className="card px-4 py-3.5">
      <h3 className="mb-2.5 text-[14px] font-semibold text-neutral-900">今日成果</h3>
      {commits.length ? (
        <ul className="thin-scroll max-h-56 space-y-1.5 overflow-y-auto">
          {commits.map((c, i) => <CommitRow key={i} c={c} />)}
        </ul>
      ) : (
        <div className="text-[12.5px] text-neutral-400">无 Git 提交(产出可能未提交或未走 Git)。</div>
      )}
    </div>
  );
}

function CommitRow({ c }: { c: CommitItem }) {
  return (
    <li className="flex gap-2 text-[12px] leading-relaxed">
      <span className="w-10 shrink-0 tabular-nums text-neutral-400">{fmtHM(c.at)}</span>
      <Dot c={projectColor(c.project && c.project !== "Unassigned" ? c.project : c.repo)} />
      <span className="min-w-0 flex-1 truncate text-neutral-700" title={`${c.repo} · ${c.subject}`}>{c.subject}</span>
    </li>
  );
}

function QualityCard({ day, today, activeOv, observedSpan }: {
  day: string; today: TodayData | null; activeOv: number; observedSpan: Interval | null;
}) {
  const [screen, setScreen] = useState<ScreenStatus | null>(null);
  useEffect(() => { api.screenStatus(day).then(setScreen).catch(() => setScreen(null)); }, [day]);
  if (!today) return null;
  const q = today.quality;
  const cov = today.metrics.observed_coverage;
  return (
    <div className="card px-4 py-3.5">
      <h3 className="mb-2 text-[14px] font-semibold text-neutral-900">数据质量</h3>
      <ul className="text-[12px]">
        <QRow k="观察窗口" v={observedSpan
          ? `${fmtHM(observedSpan.start)}–${fmtHM(observedSpan.end)}`
          : "无"} />
        <QRow k="活跃 / 观察" v={cov != null ? `${Math.round(cov * 100)}%` : "—"} />
        <QRow k="未知区间" v={fmtDur(q.unknown_sec)} />
        <QRow k="生效人工修正" v={activeOv ? `${activeOv} 条` : "0"} />
        {screen && screen.state !== "off" && (
          <QRow k="屏幕截图" v={screen.state === "down" ? "screenpipe 未运行"
            : `${screen.day_frames ?? 0} 帧${screen.state === "stalled" ? " · 截屏已停止" : ""}`}
            warn={screen.state !== "ok"} title={screen.reason} />
        )}
      </ul>
    </div>
  );
}

function QRow({ k, v, warn, title }: { k: string; v: string; warn?: boolean; title?: string }) {
  return (
    <li className="flex justify-between gap-2 border-b border-neutral-50 py-1 last:border-0" title={title}>
      <span className="text-neutral-400">{k}</span>
      <span className={`tabular-nums ${warn ? "text-amber-700" : "text-neutral-700"}`}>{v}</span>
    </li>
  );
}

// ================= primitives =================

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-5">
      <div className="label mb-2">{title}</div>
      {children}
    </div>
  );
}

function Stat({ v, l }: { v: string; l: string }) {
  return (
    <div className="rounded-lg bg-neutral-50 px-2.5 py-2">
      <div className="text-[16px] font-semibold tabular-nums text-neutral-900">{v}</div>
      <div className="text-[11.5px] text-neutral-400">{l}</div>
    </div>
  );
}

function Chip({ children, dark }: { children: React.ReactNode; dark?: boolean }) {
  return <span className={`rounded-[5px] px-1.5 py-px text-[11.5px] tabular-nums ${dark ? "bg-neutral-900 text-white" : "bg-neutral-100 text-neutral-500"}`}>{children}</span>;
}

function Dot({ c, size = 8 }: { c: string; size?: number }) {
  return <span className="inline-block shrink-0 rounded-[2px]" style={{ width: size, height: size, background: c }} />;
}

function fmtDate(d: string) {
  const wd = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][
    new Date(d + "T12:00:00").getDay()];
  const [, m, dd] = d.split("-");
  return `${+m}月${+dd}日 ${wd}`;
}
