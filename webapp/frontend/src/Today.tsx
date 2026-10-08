import { useEffect, useState } from "react";
import { api, fmtDur, fmtHM, projectColor, CATEGORY_COLOR } from "./api";
import type {
  AgentLane, Block, Insight, CommitItem, Interval, Override, Timeline, TodayData,
} from "./api";
import {
  ActivityTimeline, isCat, mergeSegments, type Selection, type Segment, t2m,
} from "./Timeline";
import { useSse } from "./sse";

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

  const load = () => {
    api.timeline(day).then(setTl).catch((e) => setErr(String(e)));
    api.today(day).then(setToday).catch(() => {});
    api.overrides(day).then((r) => setOvs(r.overrides)).catch(() => {});
    setSel(null);
  };
  useEffect(load, [day]);
  useSse((msg) => { if (msg.affected_days.includes(day)) load(); });

  const idx = days.indexOf(day);
  const activeOv = ovs.filter((o) => !o.superseded_by && o.field !== "__revoke__");
  const isToday = day === new Date().toISOString().slice(0, 10);

  return (
    <div className="mx-auto grid max-w-[1560px] gap-4 px-6 py-5 xl:grid-cols-[minmax(0,1fr)_336px]">
      {/* ================= main column ================= */}
      <div className="min-w-0 space-y-4">
        {/* page header */}
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
          <h1 className="text-[20px] font-semibold tracking-tight text-neutral-900">
            {fmtDate(day)}
          </h1>
          <div className="flex items-center gap-1.5">
            <button className="icon-btn" disabled={idx <= 0}
              onClick={() => onDayChange(days[idx - 1])} title="前一天">←</button>
            <select value={day} onChange={(e) => onDayChange(e.target.value)}
              className="input h-7 w-[128px] text-[12px]">
              {[...days].reverse().map((d) => <option key={d}>{d}</option>)}
            </select>
            <button className="icon-btn" disabled={idx < 0 || idx >= days.length - 1}
              onClick={() => onDayChange(days[idx + 1])} title="后一天">→</button>
          </div>
          {/* summary chips */}
          <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-neutral-500">
            <Stat label="人类活跃" value={fmtDur(today?.metrics.human_active_sec ?? 0)}
              color="#5E6AD2" />
            <Stat label="Agent 执行" value={fmtDur(today?.metrics.agent_exec_sec ?? 0)}
              color="#E8862E" note={today ? `${today.outcomes.n_sessions} 会话` : undefined} />
            <Stat label="提交" value={`${today?.outcomes.commits.length ?? 0}`}
              note={today ? `${today.outcomes.n_user_inputs} 次输入` : undefined} />
            {activeOv.length > 0 &&
              <Stat label="修正" value={`${activeOv.length}`} color="#D97706" />}
            {err && <span className="text-red-600">{err}</span>}
          </div>
        </div>

        {/* timeline card */}
        <div className="card px-5 pb-4 pt-4">
          <div className="mb-1 flex items-baseline justify-between">
            <h2 className="text-[15px] font-semibold text-neutral-800">活动时间轴</h2>
            <span className="text-[11px] text-neutral-400">
              滚轮缩放 · 拖拽平移 · 点击查看证据与修正
            </span>
          </div>
          {tl ? (
            <ActivityTimeline tl={tl} selection={sel} onSelect={setSel} isToday={isToday} />
          ) : (
            <div className="py-24 text-center text-[13px] text-neutral-400">加载中…</div>
          )}
        </div>

        {/* detail panel */}
        <div className="card px-5 py-4">
          {sel?.kind === "segment" && (
            <SegmentDetail segment={sel.segment} tl={tl} day={day}
              overrides={ovs} onChanged={load} goTab={goTab} />
          )}
          {sel?.kind === "unknown" && (
            <UnknownDetail interval={sel.interval} day={day} onChanged={load} />
          )}
          {sel?.kind === "session" && (
            <SessionDetail session={sel.session} goTab={goTab} day={day} />
          )}
          {!sel && (
            <div className="flex items-center gap-2 py-6 text-[13px] text-neutral-400">
              <span className="text-neutral-300">←</span>
              点击时间轴上的活动段 / 会话 / 未知区间,这里将展示其证据链与修正操作。
            </div>
          )}
        </div>
      </div>

      {/* ================= right rail ================= */}
      <aside className="space-y-4">
        <InsightsCard insights={today?.insights ?? []} day={day} onPick={onDayChange} />
        <AllocationCard today={today} />
        <OutcomesCard today={today} />
        <QualityCard today={today} activeOv={activeOv.length}
          observedSpan={tl?.observed_span ?? null} />
      </aside>
    </div>
  );
}

function Stat({ label, value, note, color }: {
  label: string; value: string; note?: string; color?: string;
}) {
  return (
    <span className="flex items-baseline gap-1.5">
      <span className="text-[11px] text-neutral-400">{label}</span>
      <span className="font-semibold tabular-nums" style={{ color: color ?? "#27272a" }}>{value}</span>
      {note && <span className="text-[11px] text-neutral-400">{note}</span>}
    </span>
  );
}

// ================= detail panels =================

function SegmentDetail({ segment, tl, day, overrides, onChanged, goTab }: {
  segment: Segment; tl: Timeline | null; day: string;
  overrides: Override[]; onChanged: () => void;
  goTab: (t: string, day?: string) => void;
}) {
  const blocks = segment.blocks;
  const anchor = blocks[0];
  const [project, setProject] = useState(anchor.project);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => { setProject(anchor.project); setMsg(null); }, [anchor.project, anchor.start]);

  const start = segment.start, end = segment.end;
  const dur = (t2m(end) - t2m(start)) * 60;
  const evIn = (tl?.evidence ?? []).filter((e) => e.at >= start && e.at < end);
  const titles = blocks.flatMap((b) => b.titles).slice(0, 10);
  const related = (tl?.agent ?? []).filter((a) =>
    a.segments.some((s) => s.end > start && s.start < end));
  const myOv = overrides.filter((o) => !o.superseded_by &&
    o.field !== "__revoke__" && o.anchor_start >= start && o.anchor_start < end);

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      {/* left: meta + titles */}
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="h-3 w-3 rounded-[4px]"
            style={{ background: isCat(segment.project) ? CATEGORY_COLOR : projectColor(segment.project) }} />
          <h3 className="text-[15px] font-semibold text-neutral-900">{segment.project}</h3>
          <span className="badge bg-neutral-100">{blocks[0].activity_type}</span>
          {anchor.is_manual_override && <span className="badge badge-dark">人工修正</span>}
          <span className="ml-auto text-[12px] tabular-nums text-neutral-500">
            {fmtHM(start)}–{fmtHM(end)} · {fmtDur(dur)}
          </span>
        </div>
        <div className="mt-1.5 flex gap-3 text-[11.5px] text-neutral-400">
          <span>置信 {Math.round(anchor.confidence * 100)}%</span>
          <span>{blocks.reduce((s, b) => s + b.n_events, 0)} 窗口事件</span>
          <span>{blocks.reduce((s, b) => s + b.evidence_count, 0)} 条证据</span>
          <span>{blocks.length > 1 ? `${blocks.length} 段合并(≤8min 间隙)` : "单段"}</span>
        </div>

        {titles.length > 0 && (
          <div className="mt-3">
            <div className="label mb-1.5">窗口标题(人类活动观察)</div>
            <div className="thin-scroll max-h-28 overflow-y-auto rounded-lg bg-neutral-50 px-3 py-2 text-[11.5px] leading-relaxed text-neutral-500">
              {titles.map((t, i) => (
                <div key={i} className="truncate border-b border-neutral-100 py-0.5 last:border-0" title={t}>
                  “{t}”
                </div>
              ))}
            </div>
          </div>
        )}

        {related.length > 0 && (
          <div className="mt-3">
            <div className="label mb-1.5">同时段的 Agent 会话({related.length})</div>
            <ul className="space-y-1">
              {related.slice(0, 4).map((a) => (
                <li key={a.id}>
                  <button className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-[12px] hover:bg-neutral-50"
                    onClick={() => goTab("sessions", day)}>
                    <span className="h-2 w-2 rounded-full"
                      style={{ background: a.agent === "pi" ? "#5E6AD2" : "#E8862E" }} />
                    <span className="text-neutral-600">{a.agent === "claude-code" ? "claude" : a.agent} · {a.project}</span>
                    <span className="ml-auto text-[11px] tabular-nums text-neutral-400">
                      执行 {fmtDur(a.exec_sec)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* right: evidence + correction */}
      <div className="min-w-0">
        <div className="label mb-1.5">证据链({evIn.length})</div>
        {evIn.length ? (
          <ul className="thin-scroll max-h-40 space-y-0.5 overflow-y-auto text-[11.5px]">
            {evIn.map((e) => (
              <li key={e.id} className="flex gap-2 border-b border-neutral-50 py-1 last:border-0">
                <span className="w-10 shrink-0 tabular-nums text-neutral-400">{fmtHM(e.at)}</span>
                <span className={
                  e.kind === "commit" ? "ev-commit font-medium" :
                  e.kind === "agent_user" ? "ev-user" : "ev-meta"}>
                  {e.kind === "commit" ? "⎇ 提交" : e.kind === "agent_user" ? "❯ 输入" : "· 会话启动"}
                </span>
                <span className="min-w-0 flex-1 truncate text-neutral-500" title={e.label}>{e.label || "—"}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-[11.5px] text-neutral-400">无事件级证据 — 该段的观察来源是窗口标题。</div>
        )}

        <div className="mt-4 border-t border-neutral-100 pt-3">
          <div className="label mb-1.5">修正项目归属(追加式,可撤销)</div>
          <div className="flex gap-2">
            <input value={project} onChange={(e) => setProject(e.target.value)}
              className="input flex-1" placeholder="项目名" />
            <button className="btn-primary" disabled={busy || !project.trim() || project === anchor.project && !myOv.length}
              onClick={async () => {
                setBusy(true);
                try {
                  await api.addOverride({
                    day_key: day, anchor_start: anchor.start, field: "project",
                    value: project.trim(), note: note || "",
                  });
                  setMsg("已修正 — 原值保留,重采集不覆盖");
                  onChanged();
                } catch (e) { setMsg(`失败:${e}`); }
                setBusy(false);
              }}>修正</button>
          </div>
          <input value={note} onChange={(e) => setNote(e.target.value)}
            className="input mt-2 w-full" placeholder="备注(可选)" />
          {msg && <div className="mt-1.5 text-[11px] text-green-700">{msg}</div>}

          {myOv.filter((o) => o.field === "project").map((o) => (
            <div key={o.id} className="mt-2 flex items-center gap-2 text-[11.5px] text-neutral-500">
              <span>#{o.id} → {o.value}(原 {o.original_value})</span>
              <button className="link" onClick={async () => {
                await api.revokeOverride(o.id); onChanged();
              }}>撤销</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function UnknownDetail({ interval, day, onChanged }: {
  interval: Interval; day: string; onChanged: () => void;
}) {
  const [project, setProject] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const dur = (t2m(interval.end) - t2m(interval.start)) * 60;
  return (
    <div>
      <h3 className="text-[15px] font-semibold text-amber-600">未知区间</h3>
      <div className="mt-1 text-[12px] text-neutral-500">
        {fmtHM(interval.start)}–{fmtHM(interval.end)} · {fmtDur(dur)} — 观察期内无窗口数据(虚拟桌面/全屏/采集缺口)。
      </div>
      <div className="mt-3 flex gap-2">
        <input value={project} onChange={(e) => setProject(e.target.value)}
          className="input flex-1" placeholder="把它标注为某个项目…" />
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
      {msg && <div className="mt-1.5 text-[11px] text-green-700">{msg}</div>}
    </div>
  );
}

function SessionDetail({ session, goTab, day }: {
  session: AgentLane; goTab: (t: string, day?: string) => void; day: string;
}) {
  const lifecycle = (t2m(session.end) - t2m(session.start)) * 60;
  const wait = Math.max(0, lifecycle - session.exec_sec);
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <div>
        <div className="flex items-center gap-2">
          <span className="h-2.5 w-2.5 rounded-full"
            style={{ background: session.agent === "pi" ? "#5E6AD2" : "#E8862E" }} />
          <h3 className="text-[15px] font-semibold text-neutral-900">
            {session.agent === "claude-code" ? "claude" : session.agent} 会话 · {session.project}
          </h3>
          <span className={`badge ${session.status === "running" ? "badge-accent" : "bg-neutral-100"}`}>
            {session.status === "running" ? "运行中" : "已结束"}
          </span>
          <span className="ml-auto text-[12px] tabular-nums text-neutral-500">
            {fmtHM(session.start)}–{fmtHM(session.end)}
          </span>
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-[12px]">
          <Row k="执行时长" v={fmtDur(session.exec_sec)} />
          <Row k="等待/挂起" v={fmtDur(wait)} />
          <Row k="执行段数" v={`${session.segments.length}`} />
          <Row k="事件数" v={`${session.n_events}`} />
          <Row k="用户输入" v={`${session.n_user} 次`} />
          <Row k="工作目录" v={session.cwd.split("/").slice(-2).join("/")} />
        </dl>
      </div>
      <div>
        <div className="label mb-1.5">执行段(10min 内事件聚为一簇)</div>
        <div className="thin-scroll max-h-40 overflow-y-auto text-[11.5px]">
          {session.segments.map((s, i) => (
            <div key={i} className="flex gap-3 border-b border-neutral-50 py-1 last:border-0">
              <span className="w-24 shrink-0 tabular-nums text-neutral-500">
                {fmtHM(s.start)}–{fmtHM(s.end)}
              </span>
              <span className="tabular-nums text-neutral-400">
                {fmtDur((t2m(s.end) - t2m(s.start)) * 60)}
              </span>
            </div>
          ))}
        </div>
        <button className="btn mt-3" onClick={() => goTab("sessions", day)}>
          在会话页查看完整对话与质量评审 →
        </button>
      </div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-2 border-b border-neutral-50 py-1">
      <dt className="text-neutral-400">{k}</dt>
      <dd className="tabular-nums text-neutral-700">{v}</dd>
    </div>
  );
}

// ================= right rail =================

function InsightsCard({ insights, day, onPick }: {
  insights: Insight[]; day: string; onPick: (d: string) => void;
}) {
  return (
    <div className="card px-4 py-3.5">
      <h3 className="mb-2.5 text-[13px] font-semibold text-neutral-800">每日洞察</h3>
      {!insights.length && (
        <div className="text-[12px] text-neutral-400">该日暂无可计算的洞察。</div>
      )}
      <ul className="space-y-2.5">
        {insights.map((i, idx) => (
          <li key={idx} className="flex gap-2.5">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-neutral-100 text-[11px] text-neutral-500">
              {KIND_ICON[i.kind] ?? "•"}
            </span>
            <div className="min-w-0">
              <div className="text-[12.5px] font-medium leading-snug text-neutral-800">{i.title}</div>
              <div className="mt-0.5 text-[11.5px] leading-relaxed text-neutral-400">{i.detail}</div>
            </div>
          </li>
        ))}
      </ul>
      <button className="link mt-3 text-[11.5px]"
        onClick={() => onPick(day)}>重新计算基于当日全部事件</button>
    </div>
  );
}

function AllocationCard({ today }: { today: TodayData | null }) {
  if (!today) return null;
  return (
    <div className="card px-4 py-3.5">
      <h3 className="mb-2.5 text-[13px] font-semibold text-neutral-800">时间分配</h3>
      <StackedBar allocation={today.allocation} />
      <div className="label mt-4 mb-1.5">人类 · 按项目</div>
      <AllocList allocation={today.allocation} />
      <div className="label mt-4 mb-1.5">Agent 执行 · 按项目</div>
      <AllocList allocation={today.agent_allocation} />
    </div>
  );
}

function StackedBar({ allocation }: { allocation: Record<string, number> }) {
  const items = Object.entries(allocation).sort((a, b) => b[1] - a[1]);
  const total = items.reduce((s, [, v]) => s + v, 0);
  if (!total) return <div className="text-[12px] text-neutral-400">无观察数据</div>;
  return (
    <div className="flex h-3 w-full overflow-hidden rounded-full bg-neutral-100">
      {items.map(([name, sec]) => (
        <div key={name} title={`${name} ${fmtDur(sec)}`}
          style={{ width: `${(sec / total) * 100}%`, background: isCat(name) ? CATEGORY_COLOR : projectColor(name) }} />
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
        <li key={name} className="flex items-center gap-2 text-[12px]">
          <span className="h-2 w-2 shrink-0 rounded-[2px]"
            style={{ background: isCat(name) ? CATEGORY_COLOR : projectColor(name) }} />
          <span className="min-w-0 flex-1 truncate text-neutral-600">{name}</span>
          <span className="tabular-nums text-neutral-400">
            {Math.round((sec / total) * 100)}%
          </span>
          <span className="w-12 text-right tabular-nums text-neutral-500">{fmtDur(sec)}</span>
        </li>
      ))}
    </ul>
  );
}

function OutcomesCard({ today }: { today: TodayData | null }) {
  if (!today) return null;
  const { commits, n_sessions, n_user_inputs } = today.outcomes;
  return (
    <div className="card px-4 py-3.5">
      <h3 className="mb-2.5 text-[13px] font-semibold text-neutral-800">
        今日成果
        <span className="ml-2 text-[11px] font-normal text-neutral-400">
          {n_sessions} 会话 · {n_user_inputs} 次输入
        </span>
      </h3>
      {commits.length ? (
        <ul className="thin-scroll max-h-44 space-y-1.5 overflow-y-auto">
          {commits.map((c, i) => (
            <CommitRow key={i} c={c} />
          ))}
        </ul>
      ) : (
        <div className="text-[12px] text-neutral-400">无 Git 提交(产出可能未提交或未走 Git)。</div>
      )}
    </div>
  );
}

function CommitRow({ c }: { c: CommitItem }) {
  return (
    <li className="flex gap-2 text-[11.5px] leading-relaxed">
      <span className="w-10 shrink-0 tabular-nums text-neutral-400">{fmtHM(c.at)}</span>
      <span className="badge shrink-0 bg-neutral-100 font-mono text-[10px]">{c.repo.slice(0, 10)}</span>
      <span className="min-w-0 flex-1 truncate text-neutral-600" title={c.subject}>{c.subject}</span>
    </li>
  );
}

function QualityCard({ today, activeOv, observedSpan }: {
  today: TodayData | null; activeOv: number; observedSpan: Interval | null;
}) {
  if (!today) return null;
  const q = today.quality;
  const cov = today.metrics.observed_coverage;
  return (
    <div className="card px-4 py-3.5">
      <h3 className="mb-2.5 text-[13px] font-semibold text-neutral-800">数据质量</h3>
      <ul className="space-y-1.5 text-[12px]">
        <QRow k="观察窗口" v={observedSpan
          ? `${fmtHM(observedSpan.start)}–${fmtHM(observedSpan.end)}(${fmtDur(q.observed_span_sec)})`
          : "无"} />
        <QRow k="窗口覆盖" v={cov != null ? `${Math.round(cov * 100)}%` : "—"} />
        <QRow k="未知区间" v={fmtDur(q.unknown_sec)} />
        <QRow k="已索引事件" v={`${q.events_indexed}`} />
        <QRow k="生效人工修正" v={activeOv ? `${activeOv} 条` : "0"} />
        <QRow k="未归属会话" v={`${q.unlinked_sessions}`} />
      </ul>
      <p className="mt-2.5 text-[11px] leading-relaxed text-neutral-400">
        人工修正为追加式记录:重采集与重新分析不会覆盖;修正撤销写入反向记录。
      </p>
    </div>
  );
}

function QRow({ k, v }: { k: string; v: string }) {
  return (
    <li className="flex justify-between gap-2 border-b border-neutral-50 py-1 last:border-0">
      <span className="text-neutral-400">{k}</span>
      <span className="tabular-nums text-neutral-700">{v}</span>
    </li>
  );
}

function fmtDate(d: string) {
  const wd = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][
    new Date(d + "T12:00:00").getDay()];
  const [, m, dd] = d.split("-");
  return `${m}月${+dd}日 ${wd}`;
}

// re-export for tests / other pages
export { mergeSegments };
export type { Block };
