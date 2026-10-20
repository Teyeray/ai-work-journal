import { useEffect, useMemo, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { useSse } from "./sse";
import { tr } from "./i18n";

/**
 * 日报: the journal pipeline's daily/weekly reports.
 *   left   when they are generated (weekday switches, weekly, Feishu push),
 *          timer status and a manual "generate now"; then the report list
 *   right  the selected report rendered, with its Feishu document link
 * The schedule is read by daily.py / weekly.py when the OS scheduler fires
 * (systemd timer · launchd agent · Windows Task Scheduler).
 */

interface ReportMeta {
  kind: "daily" | "weekly"; name: string; key: string; title: string; generated_at: string | null;
  chars: number; fallback: boolean; feishu_url: string | null; pushed_at: string | null; weekday?: number;
}
interface Schedule { daily_weekdays: number[]; weekly_enabled: boolean; feishu_push: boolean }
interface TimerInfo {
  installed?: boolean; backend?: string; timer_active: boolean; calendar: string | null; next_run: string | null; last_trigger: string | null;
  last_result: string | null; last_exit: string | null; running: boolean;
  manual: { started_at: string; running: boolean; exit_code: number | null; log_tail: string } | null;
}
interface ListResp { daily: ReportMeta[]; weekly: ReportMeta[]; schedule: Schedule; timers: Record<string, TimerInfo> }

const wd = (d: number) => tr(["", "一", "二", "三", "四", "五", "六", "日"][d], ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]);
const j = async <T,>(r: Response): Promise<T> => {
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `HTTP ${r.status}`);
  return r.json();
};

export function Reports({ focus }: { focus?: { kind: "daily" | "weekly"; name: string } | null } = {}) {
  const [data, setData] = useState<ListResp | null>(null);
  const [kind, setKind] = useState<"daily" | "weekly">("daily");
  const [sel, setSel] = useState<string | null>(null);
  const [doc, setDoc] = useState<(ReportMeta & { markdown: string }) | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = () => fetch("/api/v1/reports").then((r) => j<ListResp>(r)).then(setData).catch((e) => setErr(String(e)));
  useEffect(() => { load(); }, []);
  useSse(load);

  useEffect(() => { if (focus) { setKind(focus.kind); setSel(focus.name); } }, [focus]);
  const list = data ? data[kind] : [];
  useEffect(() => { if (!sel && list.length) setSel(list[0].name); }, [list, sel]);
  useEffect(() => {
    if (!sel) { setDoc(null); return; }
    fetch(`/api/v1/reports/${kind}/${sel}`).then((r) => j<ReportMeta & { markdown: string }>(r)).then(setDoc)
      .catch(() => setDoc(null));
  }, [kind, sel]);

  // poll timer state while something is running
  const running = data && Object.values(data.timers).some((t) => t.running);
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => {
      fetch("/api/v1/reports/status").then((r) => j<{ schedule: Schedule; timers: Record<string, TimerInfo> }>(r))
        .then((s) => {
          setData((d) => (d ? { ...d, ...s } : d));
          if (!Object.values(s.timers).some((t) => t.running)) load();
        }).catch(() => {});
    }, 3000);
    return () => clearInterval(id);
  }, [running]); // eslint-disable-line

  const html = useMemo(() => (doc ? DOMPurify.sanitize(marked.parse(doc.markdown, { async: false }) as string) : ""), [doc]);

  return (
    <div className="grid items-start gap-4 xl:grid-cols-[400px_minmax(0,1fr)]">
      <div className="space-y-4 xl:sticky xl:top-16">
        <div>
          <h1 className="text-[26px] font-semibold tracking-tight text-neutral-900">{tr("日报", "Daily/Weekly report")}</h1>
          <div className="text-[12.5px] text-neutral-400">{tr("每晚自动生成", "Generated nightly")}</div>
          {err && <div className="mt-1 text-[12.5px] text-red-600">{err}</div>}
        </div>
        {data && <ScheduleCard data={data} onChange={load} />}
        <div className="card">
          <div className="rail-tabs">
            {(["daily", "weekly"] as const).map((k) => (
              <button key={k} className={kind === k ? "on" : ""} onClick={() => { setKind(k); setSel(null); }}>
                {k === "daily" ? `${tr("日报", "Daily")} ${data?.daily.length ?? ""}` : `${tr("周报", "Weekly")} ${data?.weekly.length ?? ""}`}
              </button>
            ))}
          </div>
          <div className="thin-scroll max-h-[calc(100vh-520px)] min-h-[200px] overflow-y-auto px-2 py-1.5">
            {list.map((r) => (
              <button key={r.name} onClick={() => setSel(r.name)}
                className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left hover:bg-neutral-50 ${sel === r.name ? "bg-neutral-100" : ""}`}>
                <span className="w-[86px] shrink-0 text-[13px] font-medium tabular-nums text-neutral-900">
                  {r.kind === "daily" ? `${r.key.slice(5).replace("-", "/")} ${tr(`周${wd(r.weekday!)}`, wd(r.weekday!))}` : r.key}
                </span>
                <span className="min-w-0 flex-1 truncate text-[12.5px] text-neutral-500">
                  {r.fallback ? <span className="text-amber-700">{tr("LLM 失败,原始数据", "LLM failed, raw data")}</span> : r.title}
                </span>
                {r.feishu_url
                  ? <a href={r.feishu_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}
                      className="shrink-0 rounded bg-[#e8f0fe] px-1.5 text-[11.5px] font-medium leading-[20px] text-[#2a5bd7] hover:bg-[#d7e4fd]" title={tr("在飞书打开", "Open in Feishu")}>{tr("飞书", "Feishu")} ↗</a>
                  : <span className="shrink-0 text-[11.5px] text-neutral-300" title={tr("未推送到飞书", "Not pushed to Feishu")}>{tr("未推送", "Not pushed")}</span>}
              </button>
            ))}
            {!list.length && <div className="px-2 py-6 text-[12.5px] text-neutral-400">{kind === "daily" ? tr("无日报", "No daily reports") : tr("无周报", "No weekly reports")}</div>}
          </div>
        </div>
      </div>

      <div className="card min-h-[60vh] px-8 py-6">
        {doc ? (<>
          <div className="mb-4 flex flex-wrap items-center gap-3 border-b border-neutral-100 pb-3">
            <div className="text-[12.5px] text-neutral-400">
              {doc.generated_at ? `${tr("生成于", "Generated")} ${doc.generated_at.slice(0, 16).replace("T", " ")}` : tr("生成时间未知", "Generation time unknown")} · {doc.chars} {tr("字", "chars")}
              {doc.pushed_at && ` · ${tr("推送于", "Pushed")} ${doc.pushed_at.slice(0, 16).replace("T", " ")}`}
            </div>
            <div className="ml-auto flex gap-2">
              <button className="btn h-8 text-[12.5px]" onClick={() => navigator.clipboard?.writeText(doc.markdown)}>{tr("复制 Markdown", "Copy Markdown")}</button>
              {doc.feishu_url
                ? <a className="btn-primary h-8 text-[12.5px] leading-[30px]" href={doc.feishu_url} target="_blank" rel="noreferrer">{tr("在飞书打开", "Open in Feishu")} ↗</a>
                : <span className="text-[12.5px] text-neutral-400">{tr("未推送", "Not pushed")}</span>}
            </div>
          </div>
          {doc.fallback && (
            <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[12.5px] text-amber-800">
              {tr("LLM 总结失败 · 原始数据 · 重新生成:", "LLM summary failed · raw data · regenerate: ")}<code>journal daily {doc.key}</code>
            </div>
          )}
          <article className="md" dangerouslySetInnerHTML={{ __html: html }} />
        </>) : <div className="py-24 text-center text-[13px] text-neutral-400">{tr("选择一篇报告", "Select a report")}</div>}
      </div>
    </div>
  );
}

function ScheduleCard({ data, onChange }: { data: ListResp; onChange: () => void }) {
  const s = data.schedule;
  const [busy, setBusy] = useState(false);
  const save = async (patch: Partial<Schedule>) => {
    setBusy(true);
    try {
      await fetch("/api/v1/reports/schedule", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...s, ...patch }) }).then((r) => j(r));
      onChange();
    } finally { setBusy(false); }
  };
  const run = async (k: "daily" | "weekly") => {
    const what = k === "daily" ? tr("今天的日报", "today's daily report") : tr("本周的周报", "this week's weekly report");
    if (!confirm(tr(`生成${what}?${s.feishu_push ? "(推送飞书)" : ""}`,
      `Generate ${what}?${s.feishu_push ? " (pushes to Feishu)" : ""}`))) return;
    await fetch(`/api/v1/reports/run/${k}`, { method: "POST" }).then((r) => j(r));
    onChange();
  };
  const toggleDay = (d: number) => save({
    daily_weekdays: s.daily_weekdays.includes(d) ? s.daily_weekdays.filter((x) => x !== d) : [...s.daily_weekdays, d].sort(),
  });
  const td = data.timers.daily, tw = data.timers.weekly;
  const nextWd = td?.next_run ? new Date(td.next_run.replace(" ", "T")).getDay() || 7 : null;

  return (
    <div className="card px-4 py-3.5">
      <h3 className="text-[14px] font-semibold text-neutral-900">{tr("自动生成", "Auto-generate")}</h3>

      <div className="mt-3">
        <div className="flex items-baseline justify-between">
          <span className="text-[13px] font-medium text-neutral-800">{tr("日报", "Daily report")}</span>
          <span className="text-[11.5px] text-neutral-400">{calText(td?.calendar) ?? tr("每天 23:00", "Daily 23:00")}</span>
        </div>
        <div className="mt-2 grid grid-cols-7 gap-1.5">
          {[1, 2, 3, 4, 5, 6, 7].map((d) => {
            const on = s.daily_weekdays.includes(d);
            return (
              <button key={d} disabled={busy} onClick={() => toggleDay(d)} title={on ? tr("跳过", "Skip") : tr("生成", "Generate")}
                className={`h-9 rounded-lg text-[13px] font-medium transition ${on ? "bg-neutral-900 text-white" : "bg-neutral-100 text-neutral-400 line-through"}`}>
                {wd(d)}
              </button>
            );
          })}
        </div>
        <TimerLine t={td} skip={nextWd != null && !s.daily_weekdays.includes(nextWd)} />
      </div>

      <div className="mt-3 space-y-2 border-t border-neutral-100 pt-3">
        <Switch label={tr("周报", "Weekly report")} sub={calText(tw?.calendar) ?? tr("每周五 23:30", "Fridays 23:30")} on={s.weekly_enabled} disabled={busy}
          onChange={(v) => save({ weekly_enabled: v })} />
        <TimerLine t={tw} skip={!s.weekly_enabled} />
        <Switch label={tr("推送到飞书", "Push to Feishu")} sub={tr("关闭则仅本地", "Off: local only")} on={s.feishu_push} disabled={busy}
          onChange={(v) => save({ feishu_push: v })} />
      </div>

      <div className="mt-3 flex gap-2 border-t border-neutral-100 pt-3">
        <button className="btn h-8 flex-1 text-[12.5px]" disabled={td?.running} onClick={() => run("daily")}>
          {td?.running ? tr("日报生成中…", "Generating daily…") : tr("立即生成日报", "Generate daily now")}</button>
        <button className="btn h-8 flex-1 text-[12.5px]" disabled={tw?.running} onClick={() => run("weekly")}>
          {tw?.running ? tr("周报生成中…", "Generating weekly…") : tr("立即生成周报", "Generate weekly now")}</button>
      </div>
      {[td, tw].map((t, i) => t?.manual && (
        <pre key={i} className="thin-scroll mt-2 max-h-28 overflow-auto whitespace-pre-wrap rounded-md bg-neutral-50 px-2 py-1.5 text-[11px] leading-snug text-neutral-500">
          {t.manual.running ? tr("运行中…\n", "Running…\n") : tr(`已结束(退出码 ${t.manual.exit_code})\n`, `Finished (exit code ${t.manual.exit_code})\n`)}{t.manual.log_tail.slice(-600)}
        </pre>
      ))}
    </div>
  );
}

/** "*-*-* 23:00:00" / "Fri *-*-* 23:30:00" (systemd) or "daily 23:00" / "Fri 23:30" → "每天 23:00" / "每周五 23:30". */
function calText(cal: string | null | undefined): string | null {
  if (!cal) return null;
  const tm = cal.match(/(\d{2}:\d{2})/)?.[1];
  if (!tm) return cal;
  const dayMap: Record<string, string> = {
    Mon: tr("周一", "Mondays"), Tue: tr("周二", "Tuesdays"), Wed: tr("周三", "Wednesdays"), Thu: tr("周四", "Thursdays"),
    Fri: tr("周五", "Fridays"), Sat: tr("周六", "Saturdays"), Sun: tr("周日", "Sundays"),
  };
  const dw = Object.keys(dayMap).find((k) => cal.includes(k));
  return `${dw ? tr(`每${dayMap[dw]}`, dayMap[dw]) : tr("每天", "Daily")} ${tm}`;
}

function TimerLine({ t, skip }: { t: TimerInfo | undefined; skip: boolean }) {
  if (!t) return null;
  const name = { systemd: tr("systemd 定时器", "systemd timer"), launchd: tr("launchd 任务", "launchd job"), schtasks: tr("Windows 计划任务", "Windows scheduled task") }[t.backend ?? "systemd"] ?? tr("定时任务", "Scheduled task");
  if (t.installed === false) return <div className="mt-1.5 text-[11.5px] text-amber-700">{name}{tr("未安装 · 运行 ", " not installed · run ")}<code>journal schedule install</code></div>;
  if (!t.timer_active) return <div className="mt-1.5 text-[11.5px] text-amber-700">{name}{tr("未启用", " not enabled")}</div>;
  const ok = t.last_result === "success";
  return (
    <div className="mt-1.5 flex flex-wrap gap-x-3 text-[11.5px] text-neutral-400">
      {t.next_run && <span>{tr("下次触发", "Next run")} {t.next_run.slice(5, 16)}{skip && <span className="text-amber-700">{tr("(跳过)", " (skipped)")}</span>}</span>}
      {t.last_trigger && <span>{tr("上次", "Last run")} {t.last_trigger.slice(5, 16)} · <span className={ok ? "text-green-700" : "text-red-600"}>{ok ? tr("成功", "success") : t.last_result ?? tr("未知", "unknown")}</span></span>}
    </div>
  );
}

function Switch({ label, sub, on, disabled, onChange }: { label: string; sub: string; on: boolean; disabled?: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex cursor-pointer items-center gap-2">
      <span className="min-w-0 flex-1">
        <span className="text-[13px] font-medium text-neutral-800">{label}</span>
        <span className="ml-2 text-[11.5px] text-neutral-400">{sub}</span>
      </span>
      <button type="button" role="switch" aria-checked={on} disabled={disabled} onClick={() => onChange(!on)}
        className={`relative h-5 w-9 rounded-full transition ${on ? "bg-neutral-900" : "bg-neutral-300"}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${on ? "left-[18px]" : "left-0.5"}`} />
      </button>
    </label>
  );
}
