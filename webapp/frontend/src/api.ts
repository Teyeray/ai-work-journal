// API types matching backend responses (app/api.py)

export interface Block {
  id: string;
  start: string;
  end: string;
  project: string;
  activity_type: string;
  confidence: number;
  evidence_count: number;
  source: "inferred" | "manual";
  is_manual_override: boolean;
  n_events: number;
  titles: string[];
  attributed_by?: string | null;
}

export interface AgentLane {
  id: number;
  subagent: boolean;
  agent: string;
  cwd: string;
  project: string;
  status: string;
  start: string;
  end: string;
  segments: { start: string; end: string }[];
  exec_sec: number;
  n_events: number;
  n_user: number;
}

export interface EvidencePoint {
  id: number;
  at: string;
  kind: string;
  label: string;
  source: string;
  repo?: string;
  project?: string;
  session_id?: number;
}

export interface Episode {
  start: string;
  end: string;
  label: string;
  is_category: boolean;
  activity: string;
  summary: string;
  human_sec: number;
  share: number;
  mix: { label: string; sec: number }[];
  switches: number;
  sessions: { id: number; agent: string; project: string; exec_sec: number }[];
  n_sessions: number;
  agents_by_project: Record<string, number>;
  prompts: { at: string; text: string; session_id: number }[];
  n_prompts: number;
  commits: { at: string; repo: string; subject: string; project: string }[];
  titles: string[];
}

export interface Interval {
  start: string;
  end: string;
}

export interface WindowVisit {
  start: string;
  end: string;
  app: string;
  title: string;
  project: string;
  activity: string;
  reason?: string;
}

export interface Timeline {
  day_key: string;
  timezone: string;
  blocks: Block[];
  episodes: Episode[];
  windows: WindowVisit[];
  agent: AgentLane[];
  evidence: EvidencePoint[];
  unknown: Interval[];
  away: Interval[];
  observed_span: Interval | null;
  metrics: {
    human_active_sec: number;
    human_active_blocks: number;
    window_total_sec: number;
    afk_sec: number;
    observed_span_sec: number;
    observed_coverage: number | null;
    agent_runtime_sec: number;
    agent_runtime_union_sec: number;
    agent_exec_sec: number;
    agent_exec_union_sec: number;
    agent_peak_parallel: number;
    unknown_sec: number;
    evidence_count: number;
  };
  has_observation: boolean;
}

export interface Kpi {
  key: string;
  label: string;
  value: number | null;
  unit?: string;
  note?: string;
  state: string;
}

export interface Insight {
  kind: string;
  title: string;
  detail: string;
}

export interface CommitItem {
  at: string;
  repo: string;
  project?: string;
  subject: string;
  sha: string;
}

export interface TodayData {
  day_key: string;
  kpis: Kpi[];
  allocation: Record<string, number>;
  agent_allocation: Record<string, number>;
  insights: Insight[];
  outcomes: { commits: CommitItem[]; n_sessions: number; n_user_inputs: number };
  quality: {
    has_observation: boolean;
    unknown_sec: number;
    observed_span_sec: number;
    events_indexed: number;
    unlinked_sessions: number;
  };
  metrics: Timeline["metrics"];
  has_observation: boolean;
}

export interface SessionRow {
  id: number;
  agent: string;
  cwd: string;
  project: string;
  project_reason: string;
  start_at: string;
  end_at: string;
  status: string;
  n_events: number;
  title: string | null;
  subagent: boolean;
  parent_id?: number | null;
  children: number[];
  prompts: number;
  tools: number;
  tokens: number;
  models: string[];
  exec_sec: number;
  /** activity inside the listed date range (list endpoint with a day only) */
  range?: { first_at: string | null; last_at: string | null; prompts: number; exec_sec: number; last_prompt: string | null };
  /** opened before the range and still going into it */
  carried?: boolean;
}

export interface ScreenStatus {
  state: "ok" | "stalled" | "down" | "off";
  source?: "builtin";                     // absent = screenpipe
  ocr?: { engine: string; model?: string; url?: string; configured: boolean; problem?: string | null };
  last_frame?: string | null;
  monitors?: string[];
  reason?: string;
  hint?: string;
  day_frames?: number;
  restartable?: boolean;
  awaiting_approval?: boolean;
}

export interface SessionEvent {
  id: number;
  type: string;
  at: string;
  role?: string;
  text?: string;
  tools?: string[];
  tool_results: { name?: string; is_error?: boolean; text?: string }[];
  usage?: Record<string, number>;
  model?: string;
}

export interface SessionDetail extends SessionRow {
  events: SessionEvent[];
  review: Record<string, unknown>;
  review_available: boolean;
}

export interface ProjectRules {
  aliases: string[];
  paths: string[];
  patterns: string[];
  parent: string | null;
  ignore: boolean;
}

export interface UnclassifiedWindow {
  app: string; title: string; category: string; reason: string; sec: number; n: number;
}

export interface RulePreview {
  windows: number; sec: number; from: Record<string, number>; samples: { title: string; sec: number }[];
}

export interface LlmStatus {
  enabled: boolean; running: boolean; done: number; total: number; labelled: number;
  model: string; error: string | null;
  last_run: { titles: number; labelled: number; at: string } | null;
}

export interface Project {
  id: number;
  name: string;
  active: number;
  rules: ProjectRules;
  discovered: boolean;
  recent_sec?: number;
  priority: number;
  goals_json: string | null;
  rules_json: string | null;
  notes: string | null;
  color?: string | null;
  created_at: string;
  updated_at: string;
}

export interface Override {
  id: number;
  day_key: string;
  anchor_start: string;
  field: string;
  value: string;
  original_value: string | null;
  note: string | null;
  created_at: string;
  superseded_by: number | null;
}

export interface Review {
  session_id: number;
  status: string;
  prompt_goal?: number;
  prompt_context?: number;
  prompt_constraints?: number;
  prompt_acceptance?: number;
  exec_planning?: number;
  exec_tools?: number;
  exec_error_recovery?: number;
  exec_verification?: number;
  exec_efficiency?: number;
  summary?: string;
  suggestions?: string[];
  rationale?: string;
  evidence_refs?: number[];
}

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const b = body as { message?: string; detail?: unknown };
    throw new Error(b.message || (typeof b.detail === "string" ? b.detail : "") || `HTTP ${r.status}`);
  }
  return r.json() as Promise<T>;
}

export const api = {
  days: () => fetch("/api/v1/days").then((r) => j<{ days: string[] }>(r)),
  sync: () =>
    fetch("/api/v1/sync", { method: "POST" }).then((r) =>
      j<Record<string, unknown>>(r),
    ),
  timeline: (day: string) =>
    fetch(`/api/v1/timeline?day=${day}`).then((r) => j<Timeline>(r)),
  today: (day: string) =>
    fetch(`/api/v1/today?day=${day}`).then((r) => j<TodayData>(r)),
  evidence: (id: number) =>
    fetch(`/api/v1/evidence/${id}`).then((r) =>
      j<{ id: number; type: string; occurred_at: string; payload: Record<string, unknown> }>(r),
    ),
  sessions: (params: Record<string, string | number>) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) q.set(k, String(v));
    return fetch(`/api/v1/sessions?${q}`).then((r) =>
      j<{ sessions: SessionRow[]; next_cursor: number | null }>(r),
    );
  },
  session: (id: number) =>
    fetch(`/api/v1/sessions/${id}`).then((r) => j<SessionDetail>(r)),
  review: (id: number) =>
    fetch(`/api/v1/sessions/${id}/review`, { method: "POST" }).then((r) =>
      j<{ status: string; message?: string; review?: Review }>(r),
    ),
  feedback: (body: { session_id: number; rating: string; comment: string }) =>
    fetch("/api/v1/reviews/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<{ ok: boolean }>(r)),
  overrides: (day: string) =>
    fetch(`/api/v1/overrides?day=${day}`).then((r) =>
      j<{ overrides: Override[] }>(r),
    ),
  addOverride: (body: Record<string, string>) =>
    fetch("/api/v1/overrides", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<Record<string, unknown>>(r)),
  revokeOverride: (id: number) =>
    fetch(`/api/v1/overrides/${id}`, { method: "DELETE" }).then((r) =>
      j<{ ok: boolean }>(r),
    ),
  projects: () =>
    fetch("/api/v1/projects").then((r) => j<{ projects: Project[]; categories: string[] }>(r)),
  unclassified: (days = 14) =>
    fetch(`/api/v1/classify/unclassified?days=${days}`).then((r) =>
      j<{ days: number; windows: UnclassifiedWindow[] }>(r)),
  previewRule: (body: { project: string; kind: string; value: string; days?: number }) =>
    fetch("/api/v1/classify/preview", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then((r) => j<RulePreview>(r)),
  llmStatus: () => fetch("/api/v1/classify/llm").then((r) => j<LlmStatus>(r)),
  llmRun: () => fetch("/api/v1/classify/llm/run", { method: "POST" }).then((r) => j<LlmStatus>(r)),
  explain: (title: string, app = "google-chrome") =>
    fetch(`/api/v1/classify/explain?title=${encodeURIComponent(title)}&app=${encodeURIComponent(app)}`)
      .then((r) => j<{ label: string; reason: string; is_category: boolean }>(r)),
  patchProject: (id: number, body: Record<string, unknown>) =>
    fetch(`/api/v1/projects/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<{ ok: boolean }>(r)),
  addProject: (body: Record<string, unknown>) =>
    fetch("/api/v1/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<{ ok: boolean }>(r)),
  screenStatus: (day?: string) =>
    fetch(`/api/v1/screen/status${day ? `?day=${day}` : ""}`).then((r) => j<ScreenStatus>(r)),
  screenRestart: () =>
    fetch("/api/v1/screen/restart", { method: "POST" }).then((r) => j<{ ok: boolean }>(r)),
  analyticsTime: (start: string, end: string) =>
    fetch(`/api/v1/analytics/time?start=${start}&end=${end}`).then((r) =>
      j<{
        days: {
          day: string;
          human_sec: number;
          agent_sec: number;
          unknown_sec: number;
          has_observation: boolean;
          allocation: Record<string, number>;
        }[];
      }>(r),
    ),
};

// ---------- helpers ----------

export function fmtDur(sec: number): string {
  if (sec <= 0) return "0m";
  const total = Math.round(sec / 60);       // round once, or 7199 s reads "1h60m"
  const h = Math.floor(total / 60), m = total % 60;
  return h ? `${h}h${m ? m + "m" : ""}` : `${m}m`;
}

/** Data timestamps carry the data timezone offset (+08:00), so slice the
 *  string directly — exact and independent of the browser timezone. */
export function fmtHM(iso: string): string {
  return iso.slice(11, 16);
}

export function hhmm(iso: string): number {
  return +iso.slice(11, 13) + +iso.slice(14, 16) / 60 + +iso.slice(17, 19) / 3600;
}

export const PALETTE = [
  "#5E6AD2", "#7C6FD9", "#0EA5E9", "#14B8A6", "#2FA36B",
  "#B5A12A", "#E8862E", "#D95762", "#C94F8C", "#8C8C99",
];

export const AGENT_COLORS: Record<string, string> = {
  pi: "#5E6AD2",
  "claude-code": "#E8862E",
};

/** Categorical slots, validated for colour-vision deficiency (dataviz skill,
 *  scripts/validate_palette.js: adjacent CVD ΔE ≥ 9.1, normal-vision ≥ 19.6).
 *  Fixed order, never cycled: the 8 projects with the most of your time over
 *  the last 30 days take a slot (most first); every other project is grey. */
export const PROJECT_HUES = [
  "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948",
];
export const OTHER_PROJECT_COLOR = "#8a8f98";
const assigned: Record<string, string> = {};

export function setProjectColors(projects: { id: number; name: string; color?: string | null; recent_sec?: number }[]) {
  for (const k of Object.keys(assigned)) delete assigned[k];
  const ranked = projects.filter((p) => p.name !== "Unassigned" && !isCategory(p.name))
    .sort((a, b) => (b.recent_sec ?? 0) - (a.recent_sec ?? 0) || a.id - b.id);
  let slot = 0;
  for (const p of ranked) {
    if (p.color) assigned[p.name] = p.color;
    else if (slot < PROJECT_HUES.length && (p.recent_sec ?? 0) > 0) assigned[p.name] = PROJECT_HUES[slot++];
  }
}

export function projectColor(name: string): string {
  if (name === "Unassigned") return "#9AA1AD";
  if (isCategory(name) || name === "其他活动") return CATEGORY_COLORS[name] ?? CATEGORY_COLOR;
  return assigned[name] ?? OTHER_PROJECT_COLOR;
}

export const CATEGORY_COLOR = "#C6CAD2"; // 非项目分类:中性灰
const CATEGORY_COLORS: Record<string, string> = {
  "网页浏览": "#B9BFCA", "文献阅读": "#C9BFA8", "终端·其他": "#A7AEBB",
  "个人事务": "#CDB8C4", "文件管理": "#C3C7CF", "文本编辑": "#BCC6B4",
};

export const CATEGORY_NAMES = [
  "终端·其他", "网页浏览", "文献阅读", "文本编辑", "文件管理", "个人事务",
  "系统", "PyCharm", "通信", "阅读", "其他",
];

export function isCategory(p: string): boolean {
  return CATEGORY_NAMES.includes(p);
}

const APP_NAMES: Record<string, string> = {
  "com.mitchellh.ghostty": "Ghostty", "google-chrome": "Chrome", "zotero": "Zotero",
  "org.gnome.nautilus": "文件", "jetbrains-pycharm": "PyCharm", "org.gnome.texteditor": "文本编辑器",
  "org.gnome.ptyxis": "Ptyxis", "warp-taskbar": "任务栏", "org.gnome.settings": "设置",
  "org.gnome.loupe": "图片查看器", "code": "VS Code", "firefox": "Firefox",
};

/** Human name for an ActivityWatch app id. */
export function appName(app: string): string {
  const a = (app || "").toLowerCase();
  if (APP_NAMES[a]) return APP_NAMES[a];
  if (a.startsWith("chrome-") && a.endsWith("-default")) return "Chrome 应用";
  if (!a) return "未知应用";
  const last = app.split(".").pop() || app;
  return last.charAt(0).toUpperCase() + last.slice(1);
}

/** Window title without the trailing app suffix ("… - Google Chrome"). */
export function cleanTitle(title: string): string {
  return (title || "")
    .replace(/ - (Google Chrome|Zotero|Mozilla Firefox|Visual Studio Code)$/, "")
    .replace(/^π - /, "pi · ")
    .trim() || "(无标题)";
}
