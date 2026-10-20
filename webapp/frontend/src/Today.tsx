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
import { tr, label, locale } from "./i18n";

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
                ? tr(`${fmtHM(tl.observed_span.start)}–${fmtHM(tl.observed_span.end)} · ${tl.episodes.length} 个工作段`,
                  `${fmtHM(tl.observed_span.start)}–${fmtHM(tl.observed_span.end)} · ${tl.episodes.length} ${tl.episodes.length === 1 ? "work block" : "work blocks"}`)
                : tr("无窗口记录", "No window data")}
              {err && <span className="ml-2 text-red-600">{err}</span>}
            </div>
          </div>
          <div className="mb-0.5">
            <DayPicker value={day} days={days} onChange={onDayChange} hotkeys />
          </div>
        </div>
        <div className="flex flex-wrap gap-7">
          <Kpi v={fmtDur(m?.human_active_sec ?? 0)} l={tr("活跃时间", "Active time")} />
          <Kpi v={fmtDur(m?.agent_exec_union_sec ?? 0)} l={tr(`Agent 运行 · 峰值 ${m?.agent_peak_parallel ?? 0}`, `Agent runtime · peak ${m?.agent_peak_parallel ?? 0}`)}
            title={tr(`去重后;累加 ${fmtDur(m?.agent_exec_sec ?? 0)}`, `De-duplicated; summed ${fmtDur(m?.agent_exec_sec ?? 0)}`)} />
          <Kpi v={`${tl ? tl.agent.filter((a) => !a.subagent).length : 0}`} l={tr("会话", "Sessions") + (tl && tl.agent.some((a) => a.subagent) ? tr(` + ${tl.agent.filter((a) => a.subagent).length} 子代理`, ` + ${tl.agent.filter((a) => a.subagent).length} subagents`) : "")} />
          <Kpi v={`${nPrompts}`} l={tr("Prompt", "Prompts")} />
          <Kpi v={`${today?.outcomes.commits.length ?? 0}`} l={tr("提交", "Commits")} />
          {activeOv.length > 0 && <Kpi v={`${activeOv.length}`} l={tr("人工修正", "Manual fixes")} />}
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
              <div className="py-24 text-center text-[13px] text-neutral-400">{tr("加载中…", "Loading…")}</div>
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
              {([["story", tr("今天做了什么", "What got done")], ["flow", tr("活动流", "Activity flow")], ["detail", tr("详情", "Details")]] as [RailTab, string][]).map(([k, l]) => (
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
  const back = <button className="link mb-3 text-[12.5px]" onClick={() => setSel(null)}>{tr("← 取消选择", "← Deselect")}</button>;
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
      <div className="px-4 pb-1 pt-3 text-[12px] text-neutral-400">{tr(`${tl.episodes.length} 个工作段`, `${tl.episodes.length} ${tl.episodes.length === 1 ? "work block" : "work blocks"}`)}</div>
      <div className="px-2 py-1.5">
        {tl.episodes.map((e) => <StoryItem key={episodeKey(e)} ep={e} onClick={() => setSel({ kind: "episode", key: episodeKey(e) })} />)}
        {!tl.episodes.length && (
          <div className="px-2 py-6 text-[12.5px] leading-relaxed text-neutral-400">
            {tl.has_observation ? tr("无工作段", "No work blocks") : tr("无窗口记录", "No window data")}
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
          {ep.switches > 10 && <Chip>{tr("切换", "Switches")} {ep.switches}</Chip>}
          {ag.length > 0 && <Chip>{ag.map(([p, n]) => `${label(p)}×${n}`).join(" ")}</Chip>}
          {ep.commits.length > 0 && <Chip dark>{tr(`${ep.commits.length} 提交`, `${ep.commits.length} ${ep.commits.length === 1 ? "commit" : "commits"}`)}</Chip>}
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
      setMsg(tr(`已改为 ${v} · ${targets.length} 块`, `Set to ${v} · ${targets.length} ${targets.length === 1 ? "block" : "blocks"}`));
      onChanged();
    } catch (e) { setMsg(tr(`失败:${e}`, `Failed: ${e}`)); }
    setBusy(false);
  };

  return (
    <div>
      <h3 className="flex items-center gap-2 text-[18px] font-semibold text-neutral-900">
        <Dot c={projectColor(ep.label)} size={10} />{episodeTitle(ep)}
      </h3>
      <div className="mt-0.5 text-[13px] tabular-nums text-neutral-500">
        {fmtHM(ep.start)}–{fmtHM(ep.end)} · {tr("跨度", "Span")} {fmtDur((endM(ep.end, day) - startM(ep.start, day)) * 60)}
      </div>
      <div className="mt-3.5 grid grid-cols-3 gap-2">
        <Stat v={fmtDur(ep.human_sec)} l={tr("活跃", "Active")} />
        <Stat v={`${Math.round(ep.share * 100)}%`} l={tr("主项目占比", "Main project share")} />
        <Stat v={`${ep.switches}`} l={tr("窗口切换", "Window switches")} />
      </div>

      <Section title={tr("时间构成", "Time breakdown")}>
        <div className="mb-1.5 flex h-2 overflow-hidden rounded-full">
          {ep.mix.map((m) => <div key={m.label} style={{ width: `${(m.sec / total) * 100}%`, background: projectColor(m.label) }} />)}
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[12px] tabular-nums text-neutral-500">
          {ep.mix.map((m) => <span key={m.label} className="flex items-center gap-1"><Dot c={projectColor(m.label)} />{label(m.label)} {fmtDur(m.sec)}</span>)}
        </div>
      </Section>

      {ep.commits.length > 0 && (
        <Section title={tr(`成果 · 提交 ${ep.commits.length}`, `Outcomes · commits ${ep.commits.length}`)}>
          {ep.commits.map((c, i) => (
            <div key={i} className="flex gap-2 py-1 text-[12.5px]">
              <span className="w-10 shrink-0 tabular-nums text-neutral-400">{fmtHM(c.at)}</span>
              <span className="text-neutral-700">{c.subject}</span>
            </div>
          ))}
        </Section>
      )}

      {ep.sessions.length > 0 && (
        <Section title={tr(`Agent 会话 · ${ep.n_sessions}`, `Agent sessions · ${ep.n_sessions}`)}>
          {ep.sessions.map((s) => (
            <button key={s.id} className="flex w-full items-center gap-2 border-b border-neutral-50 py-1.5 text-left text-[12.5px] last:border-0 hover:bg-neutral-50"
              onClick={() => goTab("sessions", day)}>
              <Dot c={projectColor(s.project)} />
              <span className="text-neutral-700">{s.agent === "claude-code" ? "claude" : s.agent} · {label(s.project)}</span>
              <span className="text-neutral-400">#{s.id}</span>
              <span className="ml-auto tabular-nums text-neutral-400">{tr("本段执行", "Ran")} {fmtDur(s.exec_sec)}</span>
            </button>
          ))}
        </Section>
      )}

      {ep.prompts.length > 0 && (
        <Section title={tr(`Prompt · ${ep.n_prompts}`, `Prompts · ${ep.n_prompts}`)}>
          {ep.prompts.slice(0, 5).map((p, i) => (
            <div key={i} className="mb-1.5 line-clamp-3 rounded-r-md border-l-[3px] border-neutral-300 bg-neutral-50 px-2.5 py-1.5 text-[12.5px] leading-snug text-neutral-600">
              <b className="mr-1 font-semibold tabular-nums text-neutral-800">{fmtHM(p.at)}</b>{p.text}
            </div>
          ))}
        </Section>
      )}

      {ep.titles.length > 0 && (
        <Section title={tr("主要窗口", "Main windows")}>
          {ep.titles.map((t, i) => <div key={i} className="truncate border-b border-neutral-50 py-1 text-[12.5px] text-neutral-600 last:border-0" title={t}>{t}</div>)}
        </Section>
      )}

      {!ep.is_category && ep.share < 0.4 && (
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] leading-relaxed text-amber-800">
          {tr(`碎片化 · 主项目 ${Math.round(ep.share * 100)}%`, `Fragmented · main project ${Math.round(ep.share * 100)}%`)}
        </div>
      )}

      <Section title={tr("修正归属", "Fix attribution")}>
        <div className="flex gap-2">
          <input value={project} onChange={(e) => setProject(e.target.value)} className="input flex-1" placeholder={tr("项目名", "Project name")} />
          <button className="btn-primary" disabled={busy || !project.trim() || !blocks.some((b) => b.project !== project.trim())}
            onClick={relabel}>{tr("整段改为", "Set whole block to")}</button>
        </div>
        {msg && <div className="mt-1.5 text-[11.5px] text-green-700">{msg}</div>}
        {myOv.map((o) => (
          <div key={o.id} className="mt-1.5 flex items-center gap-2 text-[11.5px] text-neutral-500">
            <span>{fmtHM(o.anchor_start)} → {o.value}{tr(`(原 ${o.original_value})`, ` (was ${o.original_value})`)}</span>
            <button className="link" onClick={async () => { await api.revokeOverride(o.id); onChanged(); }}>{tr("撤销", "Undo")}</button>
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
      <h3 className="text-[18px] font-semibold text-amber-600">{tr("未知区间", "Unknown interval")}</h3>
      <div className="mt-1 text-[12.5px] leading-relaxed text-neutral-500">
        {fmtHM(interval.start)}–{fmtHM(interval.end)} · {fmtDur(dur)}{tr(" · 无窗口数据", " · no window data")}
      </div>
      <div className="mt-3 flex gap-2">
        <input value={project} onChange={(e) => setProject(e.target.value)}
          className="input flex-1" placeholder={tr("标注为某个项目…", "Label as a project…")} />
        <button className="btn-primary" disabled={busy || !project.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              await api.addOverride({
                day_key: day, anchor_start: interval.start, field: "project",
                value: project.trim(), note: "unknown interval annotation",
              });
              setMsg(tr("已标注", "Labeled"));
              onChanged();
            } catch (e) { setMsg(tr(`失败:${e}`, `Failed: ${e}`)); }
            setBusy(false);
          }}>{tr("标注", "Label")}</button>
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
        {session.agent === "claude-code" ? "claude" : session.agent} · {label(session.project)}
        <span className="text-[13px] font-normal text-neutral-400">#{session.id}</span>
      </h3>
      <div className="mt-0.5 text-[13px] tabular-nums text-neutral-500">
        {fmtHM(session.start)}–{fmtHM(session.end)} · {session.status === "running" ? tr("运行中", "Running") : tr("已结束", "Ended")}
        {session.subagent && tr(" · 子代理", " · subagent")}
      </div>
      <div className="mt-3.5 grid grid-cols-3 gap-2">
        <Stat v={fmtDur(session.exec_sec)} l={tr("执行", "Run time")} />
        <Stat v={fmtDur(Math.max(0, lifecycle - session.exec_sec))} l={tr("等待/挂起", "Idle/suspended")} />
        <Stat v={`${session.n_user}`} l={tr("用户输入", "User inputs")} />
      </div>
      <Section title={tr(`执行段 · ${session.segments.length}`, `Run segments · ${session.segments.length}`)}>
        {session.segments.map((s, i) => (
          <div key={i} className="flex gap-3 border-b border-neutral-50 py-1 text-[12.5px] tabular-nums last:border-0">
            <span className="text-neutral-600">{fmtHM(s.start)}–{fmtHM(s.end)}</span>
            <span className="text-neutral-400">{fmtDur((endM(s.end, day) - startM(s.start, day)) * 60)}</span>
          </div>
        ))}
      </Section>
      <button className="btn mt-4" onClick={() => goTab("sessions", day)}>{tr("打开会话 →", "Open session →")}</button>
    </div>
  );
}

// ================= cards below the timeline =================

function AllocationCard({ today }: { today: TodayData | null }) {
  if (!today) return null;
  return (
    <div className="card px-4 py-3.5">
      <h3 className="mb-2.5 text-[14px] font-semibold text-neutral-900">{tr("时间分配", "Time allocation")}</h3>
      <StackedBar allocation={today.allocation} />
      <div className="label mb-1.5 mt-4">{tr("你 · 按项目", "You · by project")}</div>
      <AllocList allocation={today.allocation} />
      <div className="label mb-1.5 mt-4">{tr("Agent · 按项目", "Agent · by project")}</div>
      <AllocList allocation={today.agent_allocation} />
    </div>
  );
}

function StackedBar({ allocation }: { allocation: Record<string, number> }) {
  const items = Object.entries(allocation).sort((a, b) => b[1] - a[1]);
  const total = items.reduce((s, [, v]) => s + v, 0);
  if (!total) return <div className="text-[12px] text-neutral-400">{tr("无数据", "No data")}</div>;
  return (
    <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-neutral-100">
      {items.map(([name, sec]) => (
        <div key={name} title={`${label(name)} ${fmtDur(sec)}`}
          style={{ width: `${(sec / total) * 100}%`, background: projectColor(name) }} />
      ))}
    </div>
  );
}

function AllocList({ allocation }: { allocation: Record<string, number> }) {
  const items = Object.entries(allocation).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const total = items.reduce((s, [, v]) => s + v, 0);
  if (!items.length) return <div className="text-[12px] text-neutral-400">{tr("无", "None")}</div>;
  return (
    <ul className="space-y-1">
      {items.map(([name, sec]) => (
        <li key={name} className="flex items-center gap-2 text-[12.5px]">
          <Dot c={projectColor(name)} />
          <span className={`min-w-0 flex-1 truncate ${isCategory(name) ? "text-neutral-400" : "text-neutral-700"}`}>{label(name)}</span>
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
      <h3 className="mb-2.5 text-[14px] font-semibold text-neutral-900">{tr("今日成果", "Today's outcomes")}</h3>
      {commits.length ? (
        <ul className="thin-scroll max-h-56 space-y-1.5 overflow-y-auto">
          {commits.map((c, i) => <CommitRow key={i} c={c} />)}
        </ul>
      ) : (
        <div className="text-[12.5px] text-neutral-400">{tr("无提交", "No commits")}</div>
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
      <h3 className="mb-2 text-[14px] font-semibold text-neutral-900">{tr("数据质量", "Data quality")}</h3>
      <ul className="text-[12px]">
        <QRow k={tr("观察窗口", "Observed window")} v={observedSpan
          ? `${fmtHM(observedSpan.start)}–${fmtHM(observedSpan.end)}`
          : tr("无", "None")} />
        <QRow k={tr("活跃 / 观察", "Active / observed")} v={cov != null ? `${Math.round(cov * 100)}%` : "—"} />
        <QRow k={tr("未知区间", "Unknown intervals")} v={fmtDur(q.unknown_sec)} />
        <QRow k={tr("人工修正", "Manual fixes")} v={activeOv ? tr(`${activeOv} 条`, `${activeOv}`) : "0"} />
        {screen && screen.state !== "off" && (
          <QRow k={tr("屏幕截图", "Screenshots")} v={screen.state === "down" ? tr("screenpipe 未运行", "screenpipe not running")
            : tr(`${screen.day_frames ?? 0} 帧`, `${screen.day_frames ?? 0} frames`) + (screen.state === "stalled" ? tr(" · 截屏已停止", " · capture stopped") : "")}
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
  const dt = new Date(d + "T12:00:00");
  const [, m, dd] = d.split("-");
  const wd = dt.toLocaleDateString(locale(), { weekday: "short" });
  return tr(`${+m}月${+dd}日 ${wd}`, dt.toLocaleDateString(locale(), { month: "short", day: "numeric" }) + ` ${wd}`);
}
