import { useEffect, useState } from "react";
import { api, fmtDur, fmtHM, projectColor, CATEGORY_COLOR } from "./api";
import type { Block, Timeline, TodayData, Interval, Override } from "./api";
import { DayTimeline, isCat } from "./DayTimeline";
import { useSse } from "./sse";

export function Today({ day, days, onDayChange }: {
  day: string;
  days: string[];
  onDayChange: (d: string) => void;
}) {
  const [tl, setTl] = useState<Timeline | null>(null);
  const [today, setToday] = useState<TodayData | null>(null);
  const [sel, setSel] = useState<Block | Interval | null>(null);
  const [selKind, setSelKind] = useState<"block" | "unknown">("block");
  const [ovs, setOvs] = useState<Override[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const load = () => {
    api.timeline(day).then(setTl).catch((e) => setErr(String(e)));
    api.today(day).then(setToday).catch(() => {});
    api.overrides(day).then((r) => setOvs(r.overrides)).catch(() => {});
    setSel(null);
  };

  useEffect(() => { load(); }, [day]);
  useSse((msg) => {
    if (msg.affected_days.includes(day)) load();
  });

  const idx = days.indexOf(day);

  return (
    <div className="space-y-4">
      {/* date nav */}
      <div className="flex items-center gap-3">
        <button className="btn" disabled={idx <= 0} onClick={() => onDayChange(days[idx - 1])}>←</button>
        <h2 className="text-lg font-semibold">{day} {weekday(day)}</h2>
        <button className="btn" disabled={idx < 0 || idx >= days.length - 1}
          onClick={() => onDayChange(days[idx + 1])}>→</button>
        <select value={day} onChange={(e) => onDayChange(e.target.value)}
          className="input w-40">
          {[...days].reverse().map((d) => <option key={d}>{d}</option>)}
        </select>
        {err && <span className="text-red-600 text-sm">{err}</span>}
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {(today?.kpis ?? []).map((k) => (
          <div key={k.key} className="card">
            <div className="text-xs text-neutral-500">{k.label}</div>
            <div className="mt-1 flex items-baseline gap-1">
              <span className={`text-2xl font-semibold ${k.state === "n/a" || k.state === "no_observation" ? "text-neutral-400" : ""}`}>
                {k.value ?? "—"}
              </span>
              {k.unit && <span className="text-xs text-neutral-400">{k.unit}</span>}
            </div>
            {k.note && <div className="text-[11px] text-neutral-400">{k.note}</div>}
            {k.state === "no_observation" &&
              <div className="text-[11px] text-amber-600">无观察数据</div>}
          </div>
        ))}
        {!today && <div className="card md:col-span-4">加载中…</div>}
      </div>

      {/* timeline */}
      <div className="card p-4">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="font-medium">时间轴</h3>
          <div className="flex gap-3 text-[11px] text-neutral-500">
            <span>■ 项目(彩) / 分类(灰)</span>
            <span><span className="text-neutral-400">▬</span> 离开(afk)</span>
            <span className="text-amber-500">▨ 未知</span>
            <span>● commit</span>
            <span><span className="text-cyan-600">▬</span> pi</span>
            <span><span className="text-orange-500">▬</span> claude</span>
          </div>
        </div>
        {tl && tl.has_observation ? (
          <DayTimeline
            blocks={tl.blocks}
            away={tl.away}
            unknown={tl.unknown}
            agent={tl.agent}
            evidence={tl.evidence}
            onBlockClick={(b) => { setSel(b); setSelKind("block"); }}
            onUnknownClick={(iv) => { setSel(iv); setSelKind("unknown"); }}
          />
        ) : tl ? (
          <div className="py-8 text-center text-sm text-neutral-400">
            该日无前台活动观察(仅 Agent 会话与证据)
            <div className="mt-2 text-xs">
              {tl.metrics.evidence_count} 条事件证据 · {tl.agent.length} 个 Agent 会话
            </div>
          </div>
        ) : (
          <div className="py-8 text-center text-sm text-neutral-400">加载中…</div>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        {/* drawer: selected block / unknown */}
        <div className="card p-4 lg:col-span-2">
          {selKind === "block" && sel && "titles" in sel ? (
            <BlockDrawer block={sel as Block} tl={tl} day={day}
              overrides={ovs} onChanged={load} />
          ) : selKind === "unknown" && sel ? (
            <UnknownDrawer iv={sel as Interval} day={day}
              onChanged={load} />
          ) : (
            <div className="py-6 text-center text-sm text-neutral-400">
              点击时间轴上的区块或未知区间查看证据与修正
            </div>
          )}
        </div>

        {/* allocation + quality */}
        <div className="space-y-4">
          {today && (
            <div className="card p-4">
              <h3 className="mb-2 font-medium">项目分布(人)</h3>
              <Allocation allocation={today.allocation} />
            </div>
          )}
          {today && (
            <div className="card p-4">
              <h3 className="mb-2 font-medium">Agent 分布(会话时长)</h3>
              <Allocation allocation={today.agent_allocation} />
            </div>
          )}
          {today && (
            <div className="card p-4 text-xs text-neutral-500">
              <h3 className="mb-2 font-medium text-neutral-800">数据质量</h3>
              <ul className="space-y-1">
                <li>观察窗口:{fmtDur(today.quality.observed_span_sec)} {
                  today.quality.has_observation ? "" : "(无)"}</li>
                <li>未知区间:{fmtDur(today.quality.unknown_sec)}</li>
                <li>已索引事件:{today.quality.events_indexed}</li>
                <li>人工修正:{ovs.filter((o) => !o.superseded_by && o.field !== "__revoke__").length} 条(当前生效)</li>
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Allocation({ allocation }: { allocation: Record<string, number> }) {
  const items = Object.entries(allocation).sort((a, b) => b[1] - a[1]).slice(0, 10);
  const total = Object.values(allocation).reduce((s, v) => s + v, 0);
  if (!total) return <div className="text-xs text-neutral-400">无数据</div>;
  return (
    <div className="space-y-1.5">
      {items.map(([name, sec]) => (
        <div key={name} className="flex items-center gap-2 text-xs">
          <span className="w-24 truncate text-right">{name}</span>
          <div className="h-3.5 flex-1 overflow-hidden rounded bg-neutral-100">
            <div className="h-full rounded"
              style={{ width: `${(sec / total) * 100}%`, background: isCat(name) ? CATEGORY_COLOR : projectColor(name) }} />
          </div>
          <span className="w-12 text-neutral-500">{fmtDur(sec)}</span>
        </div>
      ))}
    </div>
  );
}

function BlockDrawer({ block, tl, day, overrides, onChanged }: {
  block: Block; tl: Timeline | null; day: string;
  overrides: Override[]; onChanged: () => void;
}) {
  const [project, setProject] = useState(block.project);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const evIn = (tl?.evidence ?? []).filter(
    (e) => e.at >= block.start && e.at < block.end,
  ).slice(0, 12);
  const myOv = overrides.filter(
    (o) => o.anchor_start >= block.start && o.anchor_start < block.end,
  );

  const correct = async () => {
    setBusy(true);
    try {
      await api.addOverride({
        day_key: day, anchor_start: block.start, field: "project", value: project,
        note: note || "",
      });
      setMsg("已修正(原值保留,可撤销)");
      onChanged();
    } catch (e) { setMsg(`失败: ${e}`); }
    setBusy(false);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="h-3 w-3 rounded" style={{ background: projectColor(block.project) }} />
        <h3 className="font-semibold">{block.project}</h3>
        <span className="badge">{block.activity_type}</span>
        {block.is_manual_override && <span className="badge bg-neutral-900 text-white">人工修正</span>}
        <span className="ml-auto text-xs text-neutral-500">
          {fmtHM(block.start)}–{fmtHM(block.end)} · {fmtDur((+new Date(block.end) - +new Date(block.start)) / 1000)}
        </span>
      </div>

      <div className="text-xs text-neutral-500">
        置信度 {Math.round(block.confidence * 100)}% · 原始窗口事件 {block.n_events} · 证据 {block.evidence_count} 条
      </div>

      {block.titles.length > 0 && (
        <div className="rounded bg-neutral-100 p-2 text-[11px] leading-relaxed text-neutral-600">
          {block.titles.slice(0, 6).map((t, i) => <div key={i} className="truncate">“{t}”</div>)}
        </div>
      )}

      <div>
        <div className="mb-1 text-xs font-medium text-neutral-600">证据(该区间内)</div>
        {evIn.length ? (
          <ul className="max-h-32 space-y-0.5 overflow-auto text-[11px]">
            {evIn.map((e) => (
              <li key={e.id} className="flex gap-2">
                <span className="text-neutral-400">{fmtHM(e.at)}</span>
                <span className={
                  e.kind === "commit" ? "text-neutral-800" :
                  e.kind === "agent_user" ? "text-cyan-700" : "text-neutral-500"}>
                  {e.kind === "commit" ? "⎇" : e.kind === "agent_user" ? "❯" : "·"} {e.label || e.kind}
                </span>
              </li>
            ))}
          </ul>
        ) : <div className="text-[11px] text-neutral-400">无(仅窗口观察)</div>}
      </div>

      <div className="border-t pt-3">
        <div className="mb-1 text-xs font-medium text-neutral-600">修正项目归属</div>
        <div className="flex gap-2">
          <input value={project} onChange={(e) => setProject(e.target.value)}
            className="input flex-1" placeholder="项目名" />
          <button className="btn-primary" disabled={busy || project === block.project && !myOv.length}
            onClick={correct}>修正</button>
        </div>
        <input value={note} onChange={(e) => setNote(e.target.value)}
          className="input mt-2 w-full text-xs" placeholder="备注(可选)" />
        {msg && <div className="mt-1 text-[11px] text-green-700">{msg}</div>}
      </div>

      {myOv.filter((o) => !o.superseded_by && o.field === "project").length > 0 && (
        <div className="border-t pt-2">
          <div className="text-xs font-medium text-neutral-600">该区间的人工修正</div>
          {myOv.filter((o) => !o.superseded_by && o.field === "project").map((o) => (
            <div key={o.id} className="flex items-center gap-2 text-[11px]">
              <span className="text-neutral-500">#{o.id} → {o.value}</span>
              <button className="link" onClick={async () => {
                await api.revokeOverride(o.id); onChanged();
              }}>撤销</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function UnknownDrawer({ iv, day, onChanged }: { iv: Interval; day: string; onChanged: () => void }) {
  const [project, setProject] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const dur = (+new Date(iv.end) - +new Date(iv.start)) / 1000;

  return (
    <div className="space-y-3">
      <h3 className="font-semibold text-amber-600">未知区间</h3>
      <div className="text-sm text-neutral-600">
        {fmtHM(iv.start)}–{fmtHM(iv.end)} · {fmtDur(dur)}
      </div>
      <p className="text-xs text-neutral-500">
        观察窗口内无前台窗口数据(切换虚拟桌面/全屏应用/数据缺口)。
        你可以把它标注为某个项目;标注仅追加,不会覆盖原始事件。
      </p>
      <div className="flex gap-2">
        <input value={project} onChange={(e) => setProject(e.target.value)}
          className="input flex-1" placeholder="标注为项目…" />
        <button className="btn-primary" disabled={busy || !project.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              await api.addOverride({
                day_key: day, anchor_start: iv.start, field: "project",
                value: project.trim(), note: "unknown interval annotation",
              });
              setMsg("已标注");
              onChanged();
            } catch (e) { setMsg(`失败: ${e}`); }
            setBusy(false);
          }}>标注</button>
      </div>
      {msg && <div className="text-[11px] text-green-700">{msg}</div>}
    </div>
  );
}

function weekday(d: string) {
  return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][
    new Date(d + "T12:00:00").getDay()
  ];
}
