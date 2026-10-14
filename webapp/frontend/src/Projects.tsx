import { useEffect, useMemo, useState } from "react";
import { api, appName, fmtDur, projectColor } from "./api";
import type { LlmStatus, Project, ProjectRules, RulePreview, UnclassifiedWindow } from "./api";
import { useSse } from "./sse";
import { getLang, label, reason, tr } from "./i18n";

/**
 * Projects & classification rules.
 *
 * Left: every project with the rules that file windows/sessions under it —
 * keywords, directories, regex patterns, parent, ignore — editable in place.
 * Right: the windows that matched no project (by time), with a one-step
 * "file these under …" that previews the impact before saving the rule, and a
 * tester that explains how any title would be classified.
 */

type Kind = "alias" | "pattern" | "path";
const kindLabel = (): Record<Kind, string> => ({ alias: tr("关键词", "Keyword"), pattern: tr("正则", "Regex"), path: tr("目录", "Directory") });
const RULE_KEY: Record<Kind, keyof Pick<ProjectRules, "aliases" | "patterns" | "paths">> =
  { alias: "aliases", pattern: "patterns", path: "paths" };

interface PStat { human_sec: number; agent_sec: number; sessions: number; commits: number; last_active: string | null; series: number[] }

export function Projects() {
  const [rows, setRows] = useState<Project[]>([]);
  const [stats, setStats] = useState<{ days: string[]; projects: Record<string, PStat> } | null>(null);
  const [uncl, setUncl] = useState<UnclassifiedWindow[]>([]);
  const [showHidden, setShowHidden] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = () => {
    api.projects().then((r) => setRows(r.projects)).catch((e) => setErr(String(e)));
    api.unclassified(14).then((r) => setUncl(r.windows)).catch(() => {});
    fetch("/api/v1/projects/stats?days=30").then((r) => r.json()).then(setStats).catch(() => {});
  };
  useEffect(() => { load(); }, []);
  useSse(load);

  const saveRules = async (p: Project, rules: ProjectRules) => {
    setErr(null);
    try { await api.patchProject(p.id, { rules }); load(); } catch (e) { setErr(String(e)); }
  };

  const st = (n: string) => stats?.projects[n];
  const weight = (n: string) => (st(n)?.human_sec ?? 0) * 2 + (st(n)?.agent_sec ?? 0);
  const allTop = rows.filter((p) => p.active && !p.rules.parent && !p.rules.ignore)
    .sort((a, b) => weight(b.name) - weight(a.name) || a.priority - b.priority);
  const top = allTop.filter((p) => weight(p.name) > 0);
  const quiet = allTop.filter((p) => weight(p.name) === 0);
  const [showQuiet, setShowQuiet] = useState(false);
  const children = rows.filter((p) => p.active && (p.rules.parent || p.rules.ignore));
  const hidden = rows.filter((p) => !p.active);
  const names = allTop.map((p) => p.name);

  return (
    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_480px]">
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="text-[26px] font-semibold tracking-tight text-neutral-900">{tr("项目", "Projects")}</h1>
          <span className="text-[12.5px] text-neutral-500">
            {tr("近 30 天的投入与产出;展开「归类规则」决定哪些窗口、会话算作这个项目(路径 → 正则 → 关键词 → 语义判断 → 上下文)。改动后所有日期自动重算。", "Effort and output over the last 30 days; expand \"Classification rules\" to decide which windows and sessions count toward a project (path → regex → keyword → semantic → context). All dates are recomputed after a change.")}
          </span>
        </div>
        {err && <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[12.5px] text-red-700">{err}</div>}
        {top.map((p) => <ProjectCard key={p.id} p={p} names={names} onRules={saveRules} onReload={load} stat={st(p.name)} days={stats?.days ?? []} />)}
        {quiet.length > 0 && (
          <div className="pt-1">
            <button className="link text-[12.5px]" onClick={() => setShowQuiet(!showQuiet)}>
              {showQuiet ? "▾" : "▸"} {tr(`近 30 天没有活动的项目 ${quiet.length} 个`, `${quiet.length} ${quiet.length === 1 ? "project" : "projects"} with no activity in 30 days`)}
            </button>
            {showQuiet && <div className="mt-2 space-y-3">{quiet.map((p) => <ProjectCard key={p.id} p={p} names={names} onRules={saveRules} onReload={load} days={[]} />)}</div>}
          </div>
        )}
        {children.length > 0 && (
          <div className="pt-2">
            <div className="label mb-2">{tr("归并到其他项目 / 已忽略", "Merged into other projects / ignored")}</div>
            <div className="grid gap-3 lg:grid-cols-2">
              {children.map((p) => <ProjectCard key={p.id} p={p} names={names} onRules={saveRules} onReload={load} compact days={[]} />)}
            </div>
          </div>
        )}
        {hidden.length > 0 && (
          <div className="pt-2">
            <button className="link text-[12.5px]" onClick={() => setShowHidden(!showHidden)}>
              {showHidden ? "▾" : "▸"} {tr(`已隐藏 ${hidden.length} 个(旧规则把应用名误当成的项目)`, `${hidden.length} hidden (apps mistaken for projects by old rules)`)}
            </button>
            {showHidden && (
              <div className="mt-2 flex flex-wrap gap-2">
                {hidden.map((p) => (
                  <span key={p.id} className="chip-x">{p.name}
                    <button title={tr("恢复为项目", "Restore as project")} onClick={async () => { await api.patchProject(p.id, { active: 1 }); load(); }}>↺</button>
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <aside className="space-y-3 xl:sticky xl:top-16">
        <Tester />
        <LlmCard onDone={load} />
        <Unclassified rows={uncl} projects={names} onApplied={load} allProjects={rows} />
      </aside>
    </div>
  );
}

// ---------------- project card ----------------

function ProjectCard({ p, names, onRules, onReload, compact, stat, days }: {
  p: Project; names: string[]; compact?: boolean; stat?: PStat; days: string[];
  onRules: (p: Project, r: ProjectRules) => void; onReload: () => void;
}) {
  const r = p.rules;
  const [showPat, setShowPat] = useState(false);
  const [open, setOpen] = useState(compact ?? false);
  const [goal, setGoal] = useState<string | null>(null);
  const set = (patch: Partial<ProjectRules>) => onRules(p, { ...r, ...patch });
  const col = r.parent || r.ignore ? "#d4d4d8" : projectColor(p.name);
  const nRules = r.aliases.filter((a) => a.toLowerCase() !== p.name.toLowerCase()).length + r.paths.length + r.patterns.length;
  const ago = stat?.last_active ? Math.round((Date.now() - new Date(stat.last_active + "T12:00:00").getTime()) / 86400000) : null;
  return (
    <div className="card px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="h-3 w-3 rounded-[4px]" style={{ background: col }} />
        <h3 className="text-[16px] font-semibold text-neutral-900">{p.name}</h3>
        {r.parent && <span className="badge bg-neutral-100 text-neutral-600">{tr("计入", "Counts toward")} {r.parent}</span>}
        {r.ignore && <span className="badge bg-amber-50 text-amber-700">{tr("已忽略", "Ignored")}</span>}
        {ago != null && <span className="text-[12px] text-neutral-400">{ago <= 0 ? tr("今天活跃", "Active today") : tr(`${ago} 天前活跃`, `Active ${ago} ${ago === 1 ? "day" : "days"} ago`)}</span>}
        {!compact && (
          <select className="input ml-auto h-7 w-[72px] text-[12px]" value={p.priority} title={tr("优先级(P1 最高)", "Priority (P1 is highest)")}
            onChange={async (e) => { await api.patchProject(p.id, { priority: +e.target.value }); onReload(); }}>
            {[1, 2, 3, 4, 5].map((v) => <option key={v} value={v}>P{v}</option>)}
          </select>
        )}
      </div>

      {!compact && stat && (
        <div className="mt-2.5 grid grid-cols-[repeat(4,minmax(0,1fr))_minmax(0,1.6fr)] items-end gap-3">
          <Mini v={fmtDur(stat.human_sec)} l={tr("你的时间", "Your time")} />
          <Mini v={fmtDur(stat.agent_sec)} l={tr("Agent 执行", "Agent execution")} />
          <Mini v={`${stat.sessions}`} l={tr("会话", "Sessions")} />
          <Mini v={`${stat.commits}`} l={tr("提交", "Commits")} />
          <Spark series={stat.series} days={days} color={col} />
        </div>
      )}

      {!compact && (
        <div className="mt-2 text-[12.5px]">
          {goal == null ? (
            <button className="text-left text-neutral-500 hover:text-neutral-800" onClick={() => setGoal(p.goals_json ?? "")}>
              {p.goals_json ? <>{tr("目标:", "Goal: ")}<span className="text-neutral-700">{p.goals_json}</span></> : <span className="text-neutral-400">{tr("+ 写下当前目标", "+ Write the current goal")}</span>}
            </button>
          ) : (
            <div className="flex gap-2">
              <input autoFocus value={goal} onChange={(e) => setGoal(e.target.value)} className="input h-7 flex-1 text-[12.5px]" placeholder={tr("这个项目当前要达成什么", "What this project is aiming for now")}
                onKeyDown={async (e) => {
                  if (e.key === "Escape") setGoal(null);
                  if (e.key === "Enter") { await api.patchProject(p.id, { goals_json: goal.trim() || null }); setGoal(null); onReload(); }
                }} />
              <button className="btn-primary h-7 text-[12px]" onClick={async () => { await api.patchProject(p.id, { goals_json: goal.trim() || null }); setGoal(null); onReload(); }}>{tr("保存", "Save")}</button>
              <button className="btn h-7 text-[12px]" onClick={() => setGoal(null)}>{tr("取消", "Cancel")}</button>
            </div>
          )}
        </div>
      )}

      <div className="mt-2 border-t border-neutral-100 pt-2">
        <button className="text-[12.5px] font-medium text-neutral-600 hover:text-neutral-900" onClick={() => setOpen(!open)}>
          {open ? "▾" : "▸"} {tr("归类规则", "Classification rules")} <span className="font-normal text-neutral-400">· {tr(`${nRules} 条`, `${nRules} ${nRules === 1 ? "rule" : "rules"}`)}{p.discovered ? tr(" · 目录自动登记", " · auto-registered from directory") : ""}</span>
        </button>
        {open && (
          <div className="mt-2 space-y-1.5">
            <div className="flex flex-wrap items-center gap-2 text-[12px] text-neutral-500">
              <select className="input h-7 w-[132px] text-[12px]" value={r.parent ?? ""} title={tr("归并到", "Merge into")}
                onChange={(e) => set({ parent: e.target.value || null })}>
                <option value="">{tr("独立项目", "Standalone project")}</option>
                {names.filter((n) => n !== p.name).map((n) => <option key={n} value={n}>{tr("计入", "Counts toward")} {n}</option>)}
              </select>
              <label className="tl-check" title={tr("忽略:不再把任何窗口归到它", "Ignore: never file any window under it")}>
                <input type="checkbox" checked={r.ignore} onChange={(e) => set({ ignore: e.target.checked })} />{tr("忽略", "Ignore")}
              </label>
              <button className="link text-[12px]" title={tr("从列表隐藏这个项目", "Hide this project from the list")}
                onClick={async () => { await api.patchProject(p.id, { active: 0 }); onReload(); }}>{tr("隐藏", "Hide")}</button>
            </div>
            {!r.ignore && (<>
              <RuleRow kind="alias" values={r.aliases.filter((a) => a.toLowerCase() !== p.name.toLowerCase())} implicit={p.name}
                onChange={(v) => set({ aliases: v })} placeholder={tr("添加关键词,如 超声、CATL", "Add keywords, e.g. ultrasound, CATL")} />
              <RuleRow kind="path" values={r.paths} onChange={(v) => set({ paths: v })} placeholder={tr("添加目录", "Add directory")} mono />
              {r.patterns.length > 0 || showPat ? (
                <RuleRow kind="pattern" values={r.patterns} onChange={(v) => set({ patterns: v })} placeholder={tr("正则,如 (?i)harness.*bench", "Regex, e.g. (?i)harness.*bench")} mono />
              ) : (
                <button className="link ml-[54px] text-[11.5px]" onClick={() => setShowPat(true)}>{tr("+ 正则规则", "+ Regex rule")}</button>
              )}
            </>)}
          </div>
        )}
      </div>
    </div>
  );
}

function Mini({ v, l }: { v: string; l: string }) {
  return (
    <div>
      <div className="text-[17px] font-semibold leading-tight tabular-nums text-neutral-900">{v}</div>
      <div className="text-[11.5px] text-neutral-400">{l}</div>
    </div>
  );
}

/** 30-day bars of your time; hover a bar for the day. */
function Spark({ series, days, color }: { series: number[]; days: string[]; color: string }) {
  const max = Math.max(1, ...series);
  return (
    <div>
      <div className="flex h-9 items-end gap-[2px]">
        {series.map((v, i) => (
          <div key={i} className="flex-1 rounded-t-[2px]" title={`${days[i]?.slice(5) ?? ""} · ${fmtDur(v)}`}
            style={{ height: `${Math.max(v ? 8 : 3, (v / max) * 100)}%`, background: v ? color : "#eef0f3" }} />
        ))}
      </div>
      <div className="mt-0.5 flex justify-between text-[10.5px] text-neutral-400"><span>{tr("30 天前", "30 days ago")}</span><span>{tr("今天", "Today")}</span></div>
    </div>
  );
}

function RuleRow({ kind, values, onChange, placeholder, implicit, mono }: {
  kind: Kind; values: string[]; onChange: (v: string[]) => void;
  placeholder: string; implicit?: string; mono?: boolean;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const v = draft.trim();
    if (v && !values.includes(v)) onChange([...values, v]);
    setDraft("");
  };
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="w-12 shrink-0 text-[11.5px] text-neutral-400">{kindLabel()[kind]}</span>
      {implicit && <span className="chip-x muted" title={tr("项目名本身总是关键词", "The project name is always a keyword")}>{implicit}</span>}
      {values.map((v) => (
        <span key={v} className={`chip-x ${mono ? "font-mono text-[11px]" : ""}`}>{v}
          <button title={tr("删除", "Remove")} onClick={() => onChange(values.filter((x) => x !== v))}>×</button>
        </span>
      ))}
      <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={placeholder}
        onKeyDown={(e) => { if (e.key === "Enter") add(); }}
        className={`rule-input ${mono ? "font-mono" : ""}`} />
      {draft.trim() && <button className="btn h-7 text-[12px]" onClick={add}>{tr("添加", "Add")}</button>}
    </div>
  );
}

// ---------------- tester ----------------

function Tester() {
  const [title, setTitle] = useState("");
  const [app, setApp] = useState("google-chrome");
  const [res, setRes] = useState<{ label: string; reason: string; is_category: boolean } | null>(null);
  useEffect(() => {
    if (!title.trim()) { setRes(null); return; }
    const t = setTimeout(() => api.explain(title, app).then(setRes).catch(() => {}), 200);
    return () => clearTimeout(t);
  }, [title, app]);
  return (
    <div className="card px-4 py-3">
      <h3 className="mb-2 text-[14px] font-semibold text-neutral-900">{tr("试一试", "Try it")}</h3>
      <div className="flex gap-2">
        <select className="input h-8 w-[110px] text-[12px]" value={app} onChange={(e) => setApp(e.target.value)}>
          <option value="google-chrome">Chrome</option>
          <option value="com.mitchellh.ghostty">Ghostty</option>
          <option value="zotero">Zotero</option>
          <option value="jetbrains-pycharm">PyCharm</option>
          <option value="unknown-app">{tr("其他应用", "Other apps")}</option>
        </select>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={tr("粘贴一个窗口标题…", "Paste a window title…")}
          className="input h-8 flex-1 text-[12.5px]" />
      </div>
      {res && (
        <div className="mt-2 flex items-center gap-2 text-[12.5px]">
          <span className="rounded px-1.5 font-semibold text-white" style={{ background: res.is_category ? "#a1a1aa" : projectColor(res.label) }}>{label(res.label)}</span>
          <span className="text-neutral-500">{tr("因为", "Because")} {reason(res.reason)}</span>
        </div>
      )}
    </div>
  );
}

// ---------------- unclassified windows ----------------

function suggestKeyword(w: UnclassifiedWindow): string {
  const t = w.title.replace(/^t\s+/, "").replace(/^π - /, "");
  // first meaningful chunk: before " | ", " - ", "｜" or "（"
  return t.split(/ \| | - |｜|（|\(/)[0].trim().slice(0, 24);
}

function Unclassified({ rows, projects, allProjects, onApplied }: {
  rows: UnclassifiedWindow[]; projects: string[]; allProjects: Project[]; onApplied: () => void;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const total = useMemo(() => rows.reduce((s, r) => s + r.sec, 0), [rows]);
  return (
    <div className="card">
      <div className="border-b border-neutral-100 px-4 pb-2.5 pt-3">
        <h3 className="text-[14px] font-semibold text-neutral-900">{tr("未归类的窗口 · 近 14 天", "Unclassified windows · last 14 days")}</h3>
        <div className="mt-0.5 text-[12px] text-neutral-500">{tr(`前 ${rows.length} 组共 ${fmtDur(total)};点一行把它和类似窗口归到某个项目`, `Top ${rows.length} ${rows.length === 1 ? "group" : "groups"}, ${fmtDur(total)} in total; click a row to file it and similar windows under a project`)}</div>
      </div>
      <div className="thin-scroll max-h-[calc(100vh-300px)] overflow-y-auto px-2 py-1.5">
        {rows.map((w, i) => (
          <div key={`${w.app}|${w.title}`} className={`rounded-lg ${open === i ? "bg-neutral-50" : ""}`}>
            <button className="grid w-full grid-cols-[1fr_auto] gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-neutral-50"
              onClick={() => setOpen(open === i ? null : i)}>
              <span className="min-w-0">
                <span className="block truncate text-[12.5px] text-neutral-800" title={w.title}>{w.title || tr("(无标题)", "(untitled)")}</span>
                <span className="text-[11px] text-neutral-400">{appName(w.app)} · {label(w.category)} · {tr(`${w.n} 次`, `${w.n}×`)}</span>
              </span>
              <span className="pt-0.5 text-[12px] tabular-nums text-neutral-500">{fmtDur(w.sec)}</span>
            </button>
            {open === i && (
              <AssignForm w={w} projects={projects} allProjects={allProjects}
                onDone={() => { setOpen(null); onApplied(); }} />
            )}
          </div>
        ))}
        {!rows.length && <div className="px-2 py-6 text-[12.5px] text-neutral-400">{tr("近 14 天的窗口都已归类。", "All windows from the last 14 days are classified.")}</div>}
      </div>
    </div>
  );
}

function AssignForm({ w, projects, allProjects, onDone }: {
  w: UnclassifiedWindow; projects: string[]; allProjects: Project[]; onDone: () => void;
}) {
  const [project, setProject] = useState("");
  const [kind, setKind] = useState<Kind>("alias");
  const [value, setValue] = useState(suggestKeyword(w));
  const [prev, setPrev] = useState<RulePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { setPrev(null); setErr(null); }, [project, kind, value]);

  const preview = async () => {
    setBusy(true); setErr(null);
    try { setPrev(await api.previewRule({ project, kind, value })); } catch (e) { setErr(String(e)); }
    setBusy(false);
  };
  const apply = async () => {
    const p = allProjects.find((x) => x.name === project);
    if (!p) return;
    const key = RULE_KEY[kind];
    if (p.rules[key].includes(value.trim())) { onDone(); return; }
    setBusy(true);
    try {
      await api.patchProject(p.id, { rules: { ...p.rules, [key]: [...p.rules[key], value.trim()] } });
      onDone();
    } catch (e) { setErr(String(e)); }
    setBusy(false);
  };

  return (
    <div className="space-y-2 px-2 pb-3 pt-1">
      <div className="flex flex-wrap items-center gap-1.5 text-[12.5px]">
        <span className="text-neutral-500">{tr("含有", "Containing")}</span>
        <select className="input h-7 w-[76px] text-[12px]" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
          <option value="alias">{tr("关键词", "Keyword")}</option><option value="pattern">{tr("正则", "Regex")}</option>
        </select>
        <input className="input h-7 min-w-[140px] flex-1 text-[12px]" value={value} onChange={(e) => setValue(e.target.value)} />
        <span className="text-neutral-500">{tr("的窗口归到", "windows go to")}</span>
        <select className="input h-7 w-[140px] text-[12px]" value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">{tr("选择项目…", "Select project…")}</option>
          {projects.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </div>
      <div className="flex items-center gap-2">
        <button className="btn h-7 text-[12px]" disabled={busy || !value.trim() || !project} onClick={preview}>{tr("预览影响", "Preview impact")}</button>
        <button className="btn-primary h-7 text-[12px]" disabled={busy || !prev || prev.windows === 0} onClick={apply}>{tr("保存规则", "Save rule")}</button>
        {err && <span className="text-[12px] text-red-600">{err}</span>}
      </div>
      {prev && (
        <div className="rounded-md border border-neutral-200 bg-white px-2.5 py-2 text-[12px] text-neutral-600">
          {prev.windows === 0 ? tr("近 14 天没有窗口会因此改变,换个关键词试试。", "No windows in the last 14 days would change; try another keyword.") : (<>
            {getLang() === "en"
              ? <>Will move <b className="text-neutral-900">{prev.windows}</b> {prev.windows === 1 ? "window" : "windows"}, <b className="text-neutral-900">{fmtDur(prev.sec)}</b>, to {project}</>
              : <>将把 <b className="text-neutral-900">{prev.windows}</b> 个窗口、<b className="text-neutral-900">{fmtDur(prev.sec)}</b> 改归到 {project}</>}
            {tr("(来自 ", " (from ")}{Object.entries(prev.from).map(([k, v]) => `${label(k)} ${fmtDur(v)}`).join(tr("、", ", "))}{tr(")", ")")}
            <ul className="mt-1.5 space-y-0.5">
              {prev.samples.map((s) => (
                <li key={s.title} className="flex gap-2"><span className="min-w-0 flex-1 truncate">{s.title}</span>
                  <span className="tabular-nums text-neutral-400">{fmtDur(s.sec)}</span></li>
              ))}
            </ul>
          </>)}
        </div>
      )}
    </div>
  );
}

// ---------------- LLM semantic layer ----------------

function LlmCard({ onDone }: { onDone: () => void }) {
  const [st, setSt] = useState<LlmStatus | null>(null);
  useEffect(() => {
    let alive = true, was = false;
    const tick = () => api.llmStatus().then((s) => {
      if (!alive) return;
      if (was && !s.running) onDone();
      was = s.running;
      setSt(s);
    }).catch(() => {});
    tick();
    const id = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(id); };
  }, []); // eslint-disable-line
  if (!st) return null;
  return (
    <div className="card px-4 py-3">
      <div className="flex items-center gap-2">
        <h3 className="text-[14px] font-semibold text-neutral-900">{tr("语义归类", "Semantic classification")}</h3>
        <span className="text-[12px] text-neutral-400">{st.enabled ? st.model : tr("未启用", "Disabled")}</span>
        <button className="btn ml-auto h-7 text-[12px]" disabled={!st.enabled || st.running}
          onClick={async () => setSt(await api.llmRun())}>{st.running ? tr(`判断中 ${st.done}/${st.total}`, `Judging ${st.done}/${st.total}`) : tr("重新判断", "Re-judge")}</button>
      </div>
      <p className="mt-1.5 text-[12px] leading-relaxed text-neutral-500">
        {tr("规则匹配不到的窗口标题(去重后)交给模型按语义判断,只采用把握 ≥0.7 的结果;同一标题只问一次,项目变化后会重问。", "Window titles no rule matches (de-duplicated) are judged semantically by a model; only results with confidence ≥0.7 are used. Each title is asked once, and again after projects change.")}
        {st.last_run && <> {tr(`上次判断 ${st.last_run.titles} 个标题,${st.last_run.labelled} 个归入项目。`, `Last run judged ${st.last_run.titles} ${st.last_run.titles === 1 ? "title" : "titles"}; ${st.last_run.labelled} assigned to projects.`)}</>}
      </p>
      {st.error && <div className="mt-1 text-[12px] text-red-600">{st.error}</div>}
    </div>
  );
}
