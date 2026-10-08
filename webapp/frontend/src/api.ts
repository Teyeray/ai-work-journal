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
}

export interface AgentLane {
  id: number;
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
}

export interface Interval {
  start: string;
  end: string;
}

export interface Timeline {
  day_key: string;
  timezone: string;
  blocks: Block[];
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
  start_at: string;
  end_at: string;
  status: string;
  n_events: number;
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

export interface SessionDetail {
  id: number;
  agent: string;
  cwd: string;
  project: string;
  start_at: string;
  end_at: string;
  status: string;
  events: SessionEvent[];
  review: Record<string, unknown>;
}

export interface Project {
  id: number;
  name: string;
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
    throw new Error((body as { message?: string }).message || `HTTP ${r.status}`);
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
    fetch("/api/v1/projects").then((r) => j<{ projects: Project[] }>(r)),
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
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
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

export function projectColor(name: string): string {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

export const CATEGORY_COLOR = "#9ca3af"; // 灰色:分类而非项目
