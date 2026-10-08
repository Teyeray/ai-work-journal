# AI Work Intelligence Dashboard — Product & Technical Design

**Status:** v1.0 / implementation blueprint  
**Target:** 基于现有 `ai-work-journal` 的 Local-first Web Dashboard  
**Companion:** `GOAL_PROMPT.md`  
**Document purpose:** 提供给 Coding Agent 的实施约束、数据/接口契约、UI 规格、分析方法、实施与验收规则。  
**事实边界：** 目前没有读取实际 `ai-work-journal` 仓库。本文将已有来源能力（Agent Session、定时截图及 OCR）视为用户确认的功能方向，但**不假设现有代码的真实文件格式、目录或接口**。实施前必须侦察仓库，做薄适配层。

---

## 0. 决策摘要

| 决策 | 默认选择 | 理由 |
|---|---|---|
| 产品形式 | Local Web App | 实时更新、复杂交互、浏览器即开即用 |
| HTML | 仅作前端呈现及离线日报导出 | 避免将不断变化的数据直接追加进单个 HTML |
| 前端 | React + TypeScript + Vite | 类型化组件、交互、长期维护 |
| UI | Tailwind + shadcn/ui；图表 ECharts | 高信息密度、主题、图表灵活 |
| 后端 | FastAPI | Python 数据/分析侧易复用 |
| 默认存储 | SQLite WAL + 外部 artifact 文件 | 单机、事务、检索与增量更新 |
| 实时更新 | SSE 通知 + REST 拉取 | 单向变更推送足够简单；支持重连 |
| 复杂分析 | 异步 worker + versioned cache | 不阻塞 ingest/HTTP；可重算可追溯 |
| 数据模型 | 原始事件不可变 + 派生 projection 可重算 | 区分事实、推断和人工修正 |
| 部署 | 默认只绑定 127.0.0.1 | 本地敏感工作数据，不默认公开 |

**非目标（本期）：** 多人 SaaS、团队排行榜、跨机器实时同步、移动端原生应用、从 OCR 精确推导每秒的人类活动、根据截图推断情绪、没有证据的“生产力”排名、自动执行 Session 中出现的建议或命令。

---

## 1. 核心产品问题

- **时间流向**：今天真实可识别的时间花在哪里，哪些区间未知？
- **合理性**：计划 vs 实际、项目优先级与投入是否匹配、深度工作是否连续、切换是否过多、是否留出休息。
- **成果**：每个项目今天/本周产出了什么？哪些是日志可证实，哪些是用户手动输入或模型推断？
- **Agent 能力提升**：Prompt 是否提供目标、上下文、约束、验收；Agent 有没有合理探索、正确调用工具、验证结果并高质量处理错误？反复出现的问题是什么？
- **反馈闭环**：建议之后，用户是否改进了提示方式与验收习惯？相同问题是否减少？

### 1.1 信息层级

```text
Today (default)
  ├─ KPI Strip
  ├─ Timeline: Human / Agent / Evidence
  ├─ Planned vs Actual & Allocation
  ├─ Results, Unknown Time & Daily Insights
  └─ Activity Detail Drawer → Session Detail / Evidence

Analytics
  ├─ Weekly / Monthly Overview
  ├─ Time & Context Switching
  ├─ Project Priority Alignment
  ├─ Agent Quality Trends
  └─ Recommendations & Evidence

Sessions
  ├─ Session Explorer (search/filter)
  └─ Session Detail
       ├─ Metadata + Timeline
       ├─ Prompt Review
       ├─ Execution Review
       ├─ Evidence & Test Outcomes
       └─ Concrete Suggestions / Copy / Feedback

Projects
  ├─ Priority, goals, time allocation
  ├─ Milestones and verified results
  └─ Related activities/sessions + blockers
```

采用左侧导航（桌面）、右上日期/时间范围切换、可常驻的来源同步状态、全局项目筛选；窄屏使用抽屉导航。布局默认 1280–1600px 桌面优化，支持 768px 宽度基本可用。

---

## 2. Today — 页面详细规格

### 2.1 顶部指标

推荐仅显示 4–6 个有明确定义的 KPI：

- **Observed active time**：有观察依据的、人类活动区间并集时长，非“高效时长”。
- **Deep-work candidate time**：符合保守规则的连续专注候选区间总时长，标明 estimate / confidence；不可将长时间静态截图当作专注。
- **Agent runtime**：所有 Agent Session 的执行时长（总和或并发并集需明确标记）；与人类活动时长分开。
- **Project coverage**：可归属项目的人类活动时间占已识别时间比例。
- **Plan adherence**：与明确计划时间段的匹配程度；无计划则显示 N/A，不硬凑分数。
- **Verified outcomes**：经测试/Git/产物等支持的完成事项数；不可只根据聊天宣称成功。

所有数值带数据窗口、时区、估计/确认状态。默认不做一个含糊的“生产力总分”。

### 2.2 主时间轴（页面视觉中心）

**三轨设计**，共享同一时间标尺：

1. `Human`：开发/阅读/会议/学习/休息/未知等活动块，按项目着色。支持重叠来源融合后**不重叠的主分类**，并允许手动修正。
2. `Agent`：Agent Session 起止、活跃执行/等待用户/终止等状态；**允许多个 Agent 并发**，需要折叠聚合或 lane stacking。
3. `Evidence`：OCR 样本、屏幕截图、工具调用、Git commit、测试产物等采样点；以标识或密度点表示。

行为：

- 时间轴支持缩放（1/2/4/12/24h）、水平拖动、Hover 摘要、点击打开 Evidence Drawer。
- 每一块展示：时间、持续时长、项目、活动类型、证据数、推断置信度、人工修正标记。
- 未覆盖的区间以 `Unknown` 而不是空白或自动填满形式呈现。
- 点击 `Agent lane` 直接打开关联 Session 的详细信息。
- 用户在 Drawer 中可更改项目、活动类型、时间范围；修正通过 API 持久化并保留修改历史。
- 颜色方案稳定按 `project_id` hash / 用户设置映射；颜色之外有标签和图例。

### 2.3 辅助面板

- **Plan vs Actual**：日程计划轨和真实活动轨对比；无计划来源时可手动设置计划块。
- **Project Allocation**：项目人类活动投入，以及独立的 Agent 运行总量；不混算。
- **Today Results**：可验证成果、用户手动补录成果、未完成/阻碍；全部显示来源类型。
- **Daily Insights**：3–5 条有证据的总结；每条附跳转链接及置信度。
- **Data Quality**：本日 OCR 采样覆盖、未知区间、未关联 Session 数、同步延迟、分析任务状态。

### 2.4 真实示例交互

```text
Today 10/08 → 时间轴 10:00–12:00 [Inolab / coding?] → 点击
  Drawer: 10:00–12:00 / project Inolab / confidence medium
  Evidence: 10:05 OCR, 10:15 Claude tool call, 10:58 Git diff
  Sessions: Session #12, Session #13 (overlap)
  [Correct activity] [Open session] [See source]
```

---

## 3. Analytics — 页面详细规格

### 3.1 页面筛选

日期范围（7d / 30d / 自定义）、项目、活动类型、Agent 类型、数据置信度最低门槛。默认按 **本地日历日** 聚合；数据存储使用 UTC timestamp，显示时转换为用户设置的 IANA timezone。

### 3.2 图表与指标

| 组件 | 描述 | 不可误导之处 |
|---|---|---|
| Daily stacked hours | 按项目堆叠的人类活动时间 | 未识别段单独展示，不纳入归属比例 |
| Plan vs actual | 计划与实际按项目/日比较 | 无计划不计算履约率 |
| Focus distribution | 连续活动块时长分布、疑似上下文切换 | OCR 间隔粗时显示估计误差 |
| Context switches | 人类活动的项目切换数 | Agent 后台并发不计人类切换 |
| Project priority alignment | 当前优先级 × 本周投入趋势 | 优先级由用户配置，禁止默认价值判断 |
| Agent session health | 评分维度趋势、缺失验证占比 | 仅对可评估 Session 计算同口径趋势 |
| Suggestions lifecycle | 建议提出、采纳、复测、复发情况 | 不把采纳与因果改善混为一谈 |
| Data quality | 已观测/未知比例、来源覆盖、丢样/延迟 | 大量未知时降低分析结论强度 |

### 3.3 洞察生成规则

洞察必须包含 `{claim, supporting_metrics, evidence_refs, uncertainty, suggestion, analysis_version}`。如果覆盖率较低或比较窗口样本过少，则输出“不足以判断”，并解释缺少什么。

**长期模式示例（假设）**：本周 4 天在 14–17 时切换项目至少 3 次；最近 6 次 Agent Session 有 4 次先写代码后补测试；本周高优先级项目投入占可识别项目时长的 48%。以上必须从数据计算后才可以显示，不可预置真实结论。

---

## 4. Sessions — 页面详细规格

### 4.1 Explorer 列表

字段：开始/结束时间、Agent 来源、项目、Session 状态、摘要、Prompt 可评估性、Agent 质量评分/未评分状态、错误/恢复、验证证据是否存在、关联 artifacts。

功能：全文检索（对允许索引的脱敏文本）、日期/项目/Agent/状态筛选、按时间和评分排序、分页/虚拟列表；未完成 Session 标记 `running`、`stale` 或 `unknown`。

### 4.2 Detail 布局

1. **Header**：session ID、来源、项目、起止与时区、运行时长、消息/工具调用数量、数据完整性。
2. **Trajectory**：按时间排列 user prompts、assistant steps、tool calls、tool results、errors、tests、related code/artifacts；支持原文和摘要视图。
3. **Prompt Review**：对初始任务以及显著 follow-up 评估目标、上下文、约束、验收；给出具体引用与改写版本。
4. **Agent Execution Review**：规划、工具有效性、错误恢复、验证、执行效率；清楚区分可观察行为与模型推断。
5. **Evidence**：到原始消息/截图/Git/test 的可点击引用。
6. **Actionable improvements**：可以一键复制的改写 Prompt、项目规则/Skill 候选；提供「有用/无用/不准确」反馈及状态追踪。

### 4.3 评分 rubric（默认可配置）

**Prompt quality**：目标清晰度 25、上下文充分性 25、边界约束 25、验收条件 25。

**Agent execution quality**：规划分解 15、工具使用 20、错误定位与恢复 20、结果验证及证据 30、执行效率 15。

实际评分内部应是维度对象：

```json
{
  "dimension": "verification",
  "weight": 30,
  "score": null,
  "status": "unverified",
  "rationale": "未看到测试、构建或真实运行结果",
  "evidence_refs": [],
  "missing_evidence": ["test_result", "exit_code"],
  "recommendation": "在原 Prompt 中明确要求运行相关测试并汇报命令与结果"
}
```

- 维度 `score` 在 `[0,100]` 内，或 `null`；需要说明 **unscorable / missing / unverified** 的区别。
- 总分仅在达到最低证据覆盖要求时给出，并同时显示 `assessment_coverage`（被评估维度的权重占比），否则显示 `N/A`；不得把缺失当作 0，也不得重标定为看似确定的满分。
- 评分是用于**同一分析版本内**的改进信号，不是客观的工程能力测量。横向比较不同类型 Session 时需按任务类型分组。
- 人类修正可以标注「评分不准确」，后续生成需尊重反馈。
- 改写 Prompt 应遵循原任务目标和约束，不凭空要求额外工作。

### 4.4 Review Pipeline

```text
Normalised session events
    → trajectory reconstruction
    → deterministic metrics (turns/tool errors/test records/retries)
    → evidence selection & summarization
    → optional LLM rubric evaluation (strict JSON)
    → schema validation & grounded claim checking
    → versioned SessionReview (not overwrite source)
    → reviewer feedback / proposed revised prompt
```

评分规则尽量基于可数事实，LLM 用于定性解释与 Prompt 改写。解析 LLM JSON 失败则记录 `analysis_failed`，不展示虚构分数。敏感数据默认不发送到远程 LLM。

### 4.5 跨 Session 学习

- `issue_fingerprint`：将“测试缺失”“反复读取同一文件”“需求不清晰”“修改范围过大”等问题规范成可统计 issue types；保留自由文本具体描述。
- 同类任务/同一项目的趋势，显示发生率、证据样本数、反馈状态。
- 规则沉淀建议（例如放入 `CLAUDE.md`）需要显式用户采纳，系统**不可自动修改配置**。

---

## 5. Projects — 页面详细规格

项目字段：名称、描述、可选 repo/path 匹配规则、颜色、优先级、状态、当前目标、里程碑、时间预算、用户备注。项目识别既可从 repo/workspace / session cwd 规则获取，也可基于 OCR/LLM 提议，但不确定时显示 `Unassigned`。

页面展示：本周人类投入、Agent runtime、可验证成果、重要 Session、里程碑达成、阻碍、下一步行动、优先级与实际时间的偏差。项目名、优先级、目标可编辑；规则匹配出现冲突时优先用户显式标注。

---

## 6. 数据来源及事实/推断分离

### 6.1 输入来源

- `agent_session`：来自已接入 Agent 工具的消息、tool calls、tool results、error、session 生命周期；实际格式通过 adapter 对接。
- `screen_ocr`：截图采样的 `timestamp / text / source image ref / application or window metadata (if available)`；不能臆造 OCR 有哪些字段。
- `git_or_test`（可选）：提交、diff、测试/构建退出码等独立验证来源；仓库没有就后续可插拔接入。
- `manual`：项目、计划时间、活动修正、成果和反馈，由用户手工记录。

### 6.2 标准化事件信封（示意）

```json
{
  "event_id": "evt_sha256_or_uuid",
  "source_type": "agent_session",
  "source_instance": "local/claude-code",
  "source_event_id": "stable-upstream-id-if-available",
  "session_external_id": "source-session-id",
  "observed_at_utc": "2026-10-08T02:32:00Z",
  "occurred_at_utc": "2026-10-08T02:31:58Z",
  "event_type": "tool_call",
  "project_hint": "inolab",
  "payload_ref": "file:///...or-internal-artifact-id",
  "content_sha256": "...",
  "schema_version": 1,
  "ingest_version": "1.0.0"
}
```

`observed_at` 与 `occurred_at` 不可混淆。原始事件保持不可变；人工修正和算法重估作为新的独立记录，不直接覆盖来源日志。原始路径仅在后端使用，不在公开/导出 JSON 中原样暴露敏感绝对路径。

### 6.3 核心存储实体

| 表/实体 | 核心字段 | 索引/约束 |
|---|---|---|
| `source_cursors` | source_key, cursor_json, last_scan_at, revision | source_key unique |
| `source_events` | id, source_key, source_event_key, event_type, occurred_at, observed_at, payload_ref, content_hash | unique(source_key, source_event_key); timestamp index |
| `evidence_artifacts` | id, kind, storage_ref, checksum, created_at, retention_class, redaction_state | checksum / created_at |
| `agent_sessions` | id, source, external_id, start_at, end_at, status, project_id, summary, completeness | unique(source,external_id) |
| `session_event_links` | session_id, event_id, ordinal | unique(session_id,event_id) |
| `activity_segments` | id, start_at, end_at, project_id, activity_type, confidence, origin, inference_version | start/end interval index |
| `activity_evidence_links` | segment_id, event_id, support_type | both IDs indexed |
| `manual_overrides` | id, target_type, target_id, operation, payload_json, created_at, user_note | target index, append-only history |
| `projects` | id, name, color, priority, active, goals_json, rules_json | name optional unique |
| `planned_blocks` | id, start_at, end_at, project_id, activity_type, description | date index |
| `outcomes` | id, project_id, day_key, title, verification_state, evidence_json, source_type | day/project |
| `session_reviews` | id, session_id, analyzer_version, prompt_review_json, execution_review_json, coverage, created_at, status | (session_id, analyzer_version, input_fingerprint) unique |
| `review_feedback` | id, review_id, category, feedback, note, created_at | review index |
| `daily_projections` | date_key, timezone, projection_version, input_fingerprint, metrics_json, updated_at | (date_key,timezone,projection_version) unique |
| `analysis_jobs` | id, job_type, target_key, fingerprint, state, attempt, error, timestamps | unique(job_type,target_key,fingerprint) |

具体 SQL 由实现前的仓库摸排决定：若现有 journal DB 已可满足需求，允许使用 view / 新表；若与现有写入进程耦合风险高，可把 Dashboard 的派生索引放在独立 `dashboard.db`。**禁止为了满足这张表强行迁移/重构原项目的原始日志格式。**

### 6.4 ActivitySegment 分类规则

- 将 Agent `session open` 视为 `agent_runtime` 证据，而不是 `human_activity`。
- 将 OCR 截图视为时间点附近的证据，推断活动连续性必须有最大间隔阈值、连续一致证据和显式置信度；超过阈值自动分段并保留 `unknown`。
- 人类主活动同一时刻至多有一个主类型；允许标记辅助上下文（如与 Agent 交互、视频会议）。
- “读代码与 Agent 后台运行”允许并存于不同 lane，分开统计。
- Source overlap 通过时间并集计算；同一活动有多个证据不叠加时长。
- 跨午夜切割为时区内自然日的 segments 供日统计，但保留原始连续活动 ID。
- 含糊来源 → unknown / unassigned；人工修正最高优先级，后续分析可更新证据但不得静默覆盖修正。

### 6.5 建议追溯机制

`Insight` 和 `ReviewFinding` 至少包含 `claim`、`analysis_version`、`evidence_refs[]`、`confidence`、`limitations[]`、`suggestion`。前端必须能打开该证据，而不是只展示无法核对的 AI 结论。缺失 artifact 时返回 `unavailable` 并说明原因。

---

## 7. 增量 Pipeline / 同步一致性

### 7.1 Ingest 步骤

```text
Discover sources
  → identify file/source revision
  → load durable cursor
  → read new COMPLETE records (or incremental upstream API page)
  → validate + normalize
  → transaction: upsert events, links, cursor
  → enqueue affected targets
  → update projections / analyses asynchronously
  → publish data_changed to SSE subscribers
```

### 7.2 关键规则

1. **幂等键**：优先 `(source, stable_event_id)`，缺失时由可稳定重算的 `(source instance, file identity, logical record locator, content hash)` 组合；同一行重复扫描不能增加事件数。
2. **增量游标**：对于 append-only JSONL 可用 byte offset + file fingerprint；保留未结束的最后一行；对日志轮转/截断检测 source revision，必要时重新扫描并靠 dedup 去重。不同来源用专属 cursor strategy。
3. **原子提交**：新事件和游标应在同一事务提交；解析失败的内容进 error/quarantine，不盲目跳过导致永久丢数据。
4. **异步派生**：新事件计算受影响的 `[start,end]`，包含迟到时间戳、跨日窗口和重叠 ActivitySegment；针对受影响范围失效缓存，不能始终全量计算。
5. **Session Review**：Session 结束后自动 review；运行中可选 provisional 版本，但必须标识。新片段使旧的 input fingerprint 失效并按预算重新分析。
6. **恢复**：worker/task durable 状态、失败可重试、幂等结果；SSE 断线后靠 REST 重取最新 revision，不能仅依赖不持久化的推送。
7. **源数据变更**：被编辑而非 append-only 的文件需要差异探测（mtime/size/hash/revision），更新事件版本或 tombstone 标记；原始历史记录仍可审计。

### 7.3 分析成本策略

- OCR/文件事件首先走确定性解析/分类与缓存，**不为每张截图都调用 LLM**。
- 先生成时间桶/聚合特征，仅在候选活动切换、明显不一致或 Session 结束时调用更昂贵分析。
- 每个分析 job 携带 `analyzer_version`, `input_fingerprint`, `model_id`, `prompt_template_version`, `token/cost metrics when available`。
- 重复输入命中缓存；外部 LLM 需显式启用，并设置预算、速率限制、重试上限及脱敏策略。

---

## 8. API 契约（建议 v1）

建议统一前缀 `/api/v1`，JSON 响应。所有日期范围参数为 `start/end` ISO 8601，`timezone` 必须为 IANA 名（如 `Asia/Hong_Kong`）。分页返回 cursor/next_cursor；错误遵循 `{error:{code,message,details}}`。

### 8.1 Read API

| Method | Endpoint | Payload/用途 |
|---|---|---|
| GET | `/health` | 存储/worker 状态（不返回敏感信息） |
| GET | `/sync/status` | 最新采集时间、数据源状态、revision、故障信息 |
| GET | `/today?date=YYYY-MM-DD&timezone=...` | 顶部指标/成果/计划/数据质量/分析摘要 |
| GET | `/timeline?start=...&end=...&project_id=...` | human/agent/evidence 三轨数据、confidence、evidence count |
| GET | `/activities/{id}` | 活动详情及关联 evidence/session |
| GET | `/analytics/time?start=...&end=...&group_by=day` | 时间分类与任务切换等统计 |
| GET | `/analytics/agents?start=...&end=...` | Session 质量和缺失证据趋势 |
| GET | `/analytics/projects?start=...&end=...` | 项目优先级 vs 投入 |
| GET | `/sessions?query=...&project_id=...&cursor=...` | 过滤、分页 Session 列表 |
| GET | `/sessions/{id}` | 元数据、摘要、timeline identifiers |
| GET | `/sessions/{id}/events?cursor=...` | 分页原始轨迹 / 脱敏 preview |
| GET | `/sessions/{id}/review` | 最新或选定版本 Review 与证据 |
| GET | `/projects` | 项目列表与基本状态 |
| GET | `/projects/{id}` | 单项目目标、时间、成果、Session |
| GET | `/evidence/{id}` | 脱敏元数据与预览/授权 artifact 请求 |
| GET | `/events` | SSE 变更通知（event id / type / revision，非全文） |

### 8.2 Write API

| Method | Endpoint | 用途 |
|---|---|---|
| PATCH | `/activities/{id}` | 写入人工修正记录（需要校验版本冲突） |
| POST | `/planned-blocks` | 设置计划区块 |
| PATCH | `/planned-blocks/{id}` | 更改计划区块 |
| POST | `/outcomes` | 手动成果/证据 |
| POST | `/projects` | 新建项目 |
| PATCH | `/projects/{id}` | 优先级、规则、目标等 |
| POST | `/sessions/{id}/review:request` | 触发一次 review（去重异步 job） |
| POST | `/reviews/{id}/feedback` | 用户反馈 |
| POST | `/exports/daily` | 异步导出离线 HTML，返回 job id |
| GET | `/jobs/{id}` | 导出/分析任务状态和受控产物下载标识 |

### 8.3 示例：Timeline response

```json
{
  "revision": 287,
  "timezone": "Asia/Hong_Kong",
  "window": {"start": "2026-10-08T08:00:00+08:00", "end": "2026-10-08T13:00:00+08:00"},
  "human": [{
    "id": "act_007", "start": "2026-10-08T10:00:00+08:00", "end": "2026-10-08T11:10:00+08:00",
    "activity_type": "coding", "project_id": "inolab", "confidence": 0.72,
    "evidence_count": 6, "source": "inferred", "is_manual_override": false
  }],
  "agent": [{
    "session_id": "session_abc", "agent": "claude_code",
    "start": "2026-10-08T10:10:00+08:00", "end": "2026-10-08T11:20:00+08:00", "status": "ended"
  }],
  "evidence": [{"id": "ev_003", "at": "2026-10-08T10:15:00+08:00", "kind": "ocr"}],
  "unknown": [{"start": "2026-10-08T11:10:00+08:00", "end": "2026-10-08T12:00:00+08:00"}]
}
```

### 8.4 SSE 事件约束

`event: data_changed` + `{revision, affected_days[], affected_session_ids[], affected_project_ids[]}`。前端收到事件后对相关 React Query cache keys 失效并 GET 最新数据；不要通过 SSE 传截图原文或超大轨迹。重连后比对 revision，必要时全量刷新**当前视窗**（不是所有历史）。

---

## 9. UI 组件架构、状态及交互

```text
AppShell
 ├─ NavRail
 ├─ GlobalDateRangePicker / ProjectFilter / SyncIndicator
 └─ Router
     ├─ TodayPage
     │   ├─ KPIGrid
     │   ├─ WorkTimeline (HumanLane / AgentLane / EvidenceLane)
     │   ├─ PlanVsActual
     │   ├─ ProjectAllocation
     │   ├─ OutcomesPanel / DailyInsights / DataQuality
     │   └─ ActivityDrawer
     ├─ AnalyticsPage
     │   ├─ RangeToolbar
     │   ├─ TimeSeries / Focus / ContextSwitches
     │   ├─ PriorityAlignment / AgentQualityTrends
     │   └─ InsightEvidenceDrawer
     ├─ SessionsPage
     │   ├─ SessionFilters / SessionVirtualList
     │   └─ SessionDetail
     │       ├─ Trajectory
     │       ├─ RubricScoreBreakdown
     │       ├─ EvidenceViewer
     │       ├─ RecommendationCards / CopyPrompt
     │       └─ FeedbackControls
     └─ ProjectsPage
         ├─ ProjectCards / PrioritySettings
         └─ ProjectDetail
```

### 9.1 UI design system

- 视觉方向：专业、安静、高信息密度，非游戏化；颜色表示**项目**，语义状态再用 icon/边框/文本表达。
- 固定的 8px spacing scale、明确 typography hierarchy、`tabular-nums` 用于时长/分数；控制图表色盲可读性。
- 支持 light/dark、键盘导航、aria labels、低动效偏好、空状态/加载/错误/部分数据状态。
- Timeline 是主设计难点：优先自定义 canvas/SVG 或 DOM 轨道 + 虚拟化；按时间比例渲染，避免用等宽块误导时长。图表用 ECharts；时间轴若 ECharts 足以承载交互亦可使用它。
- 不把不确定推断包装成确定事实；在 KPI、推断区间、评分旁展示 confidence/coverage。

### 9.2 Query/cache strategy

- 前端使用 TanStack Query（如仓库已有同类库则沿用），query key 包含 range / filters / timezone / data revision。
- Session events 和 OCR previews 懒加载；截图预览在受控本地 API 按需取；关闭 Drawer 可清理持有的数据引用。
- SSE 触发失效，人工修正 mutation 成功后刷新受影响区间；冲突返回 409 并要求重新获取。
- 全局筛选状态可用 URL query params 存储，支持复制链接到本机某天某 Session。

---

## 10. 计算规则：特别需要防止的统计错误

1. `total_human_time = duration(union(accepted human segments in window))`，**不是** OCR 数量 × 采样间隔，也不是 Session durations 求和。
2. `agent_runtime_sum` 是每个 Session 持续时长的总和；`agent_runtime_union` 是并发情况下运行窗口的并集；两个值名称不可混淆。
3. `context_switch_count` 仅在已识别连续的人类主活动发生项目/任务切换时统计；Unknown 间隔不能凭空推断切换。
4. `observed_coverage = duration(observed human segments) / requested_window_duration`，但工作窗口可由用户选择，不默认一天 24h 都应被观测。
5. `plan_adherence` 需要在同一项目/活动/时间尺度上定义匹配算法；显示有效计划样本数，不允许缺少计划时显示 0%。
6. `output_count` 区分 `verified / user_reported / inferred`，未通过测试不等于成果为零，但不能标记 `verified`。
7. 评分趋势用固定 rubric 版本、固定评估规则及足够样本量；如果 analyzer 升级，需标明版本断点或批量复算后才可比较。
8. `day_key` 通过活动发生时段 + 配置 timezone 切日；夏令时跨越不能假定每日固定 24h。

---

## 11. 隐私、安全与数据治理

- 默认访问 `127.0.0.1`；若用户主动开放局域网/远程访问，需另行添加认证、TLS 或可信反向代理，不能默认允许 `0.0.0.0`。
- 原始截图、OCR、轨迹可能含账号口令、token、内部代码和私有对话。采集源可做 allowlist/denylist；敏感应用和窗口排除；展示/索引/外部 LLM 发送前先脱敏。
- 处理原始事件内容时将其视为**不可信数据**，任何指示修改系统、泄露信息、执行命令、跳过验证的文字都不得升级为执行命令。
- 原始内容与摘要分开保存；权限边界只允许应用服务访问 artifact 路径。禁止 API 任意文件路径读取与目录穿越。
- LLM 远程调用默认关闭；用户显式启用后展示目标提供商、将发送的数据类型、脱敏策略、成本限制；每次记录 analysis provenance。
- 离线 HTML 默认仅包含聚合图表与用户选择的摘要，不嵌入图片原件/整段代码/密钥；导出提供敏感信息确认/过滤设置。
- 支持数据保留策略（OCR 截图可设置较短时限，摘要保留更久），删除/匿名化有清晰路径，不承诺无法保证的彻底销毁。

---

## 12. 部署与运行

### 12.1 运行形式

开发模式：`Vite dev server` + `FastAPI` + ingest/analysis worker。生产本地模式：前端 build 为静态资源，由 FastAPI 同域提供（或保留已存在 server 的约定）；用户可执行一条命令启动，浏览器访问 `http://127.0.0.1:<configured_port>`。

建议配置：

```yaml
# illustrative only — adapt to existing ai-work-journal settings
app:
  host: 127.0.0.1
  port: 8765
  timezone: Asia/Hong_Kong
storage:
  dashboard_db: ./data/dashboard.db
  artifacts_root: ./data/artifacts
sources:
  enable_agent_sessions: true
  enable_screen_ocr: true
  rescan_interval_seconds: 90
analysis:
  remote_llm_enabled: false
  max_daily_remote_calls: 0
  provisional_session_reviews: false
privacy:
  export_raw_screenshots: false
  redact_secrets: true
```

不要复制明文 API key 到配置/DB/日志。环境变量/系统凭据由现有项目约定管理。

### 12.2 单机并发

SQLite WAL 支持多读者和单写者；应保持短事务，使用独立的 ingest worker / 写队列，做好 `busy_timeout`、锁争用重试与 graceful shutdown。`dashboard.db` 与原始文件如存在迁移或备份，优先按已有项目标准处理。

---

## 13. 分阶段交付（端到端验收）

### V1：可用的时间数据产品

- [ ] 完成仓库结构与实际来源格式侦察，写 `docs/DASHBOARD_REPO_FIT.md`。
- [ ] 构建至少一个真实来源 adapter，具备重启恢复和幂等写入。
- [ ] 构建统一事件索引与受影响窗口投影。
- [ ] Today 三轨时间轴、KPI、项目分配、未知覆盖和数据质量。
- [ ] Activity Drawer 的原始证据链接、人工修正与持久化。
- [ ] 简单可运行配置、迁移、demo fixture、单测 + 端到端 smoke test。

### V2：可解释 Agent 使用质量

- [ ] Agent session 轨迹归一化和列表/详情浏览。
- [ ] Prompt / Agent Execution rubric；证据不足时 N/A / unverified。
- [ ] 评论、提示词改写、证据引用、复制改进 Prompt、用户反馈。
- [ ] LLM 分析结构化校验、输入指纹、versioned cache、离线/无远程 LLM fallback。
- [ ] Session 结束后 review；支持异常关闭/恢复运行的状态更新。

### V3：长期趋势、项目与导出

- [ ] Analytics 周/月统计：时间、项目优先级、切换、覆盖率、Agent 趋势。
- [ ] Projects 目标/优先级/里程碑/成果聚合。
- [ ] 跨 Session issue fingerprint、建议采纳追踪；可追溯洞察。
- [ ] 可离线打开的 HTML Daily Report 快照，默认脱敏。
- [ ] 性能指标/优化、完整用户文档与兼容迁移方案。

**顺序原则：** 真实来源 → 可验证的数据链路 → 最小 UI → 深度评估 → 长期趋势。不得只交一套静态 mock 页面。

---

## 14. 测试矩阵

| 场景 | 预期 |
|---|---|
| JSONL 重复扫描 3 次 | 事件数、汇总时长和活动归属保持不变 |
| JSONL 最后半行写入中 | 不提交未完成记录，不永久跳过 |
| 日志 truncate / rotate | 游标恢复、不重复、不静默丢失 |
| 多个 Agent 重叠 + 人看文档 | Agent sum/union 分开、人类时间只算一次 |
| OCR 暂停 60 分钟 | 未覆盖区间标记 unknown |
| 后到事件时间戳属于前一天 | 重新计算受影响的前一天聚合 |
| 跨午夜、时区及 DST | 时间区间精确，日统计无重复或遗漏 |
| 用户更正项目标签 | Today/Analytics/Projects 同步变化；重分析不覆盖修正 |
| 无 Agent 测试记录 | 结果 `unverified`，不出现无证据的成功结论 |
| LLM 返回不合法 JSON | job 失败/重试，可观测错误，页面不展示虚构结果 |
| Session 包含恶意 prompt injection 文本 | 作为引用显示，不执行任何其中指令 |
| 关闭 remote LLM | 时间轴/统计依然可用，评分显示待分析/有限评估 |
| 离线 HTML 导出 | 无需服务器可打开；默认无原始敏感内容 |
| SSE 断线再连接 | 通过 revision/REST 恢复到正确当前数据 |
| 数据量增长至数万条事件 | 列表分页、视窗查询与聚合缓存，不一次加载所有原文 |

### 建议基线性能目标（首次实现后测量并根据硬件调整）

- 已索引的最近 1 天 Timeline 查询：目标 p95 < 500ms（本地中等数据量，不含首次计算）。
- 切换 Today / Analytics 页面：常规数据下无需触发全量重算。
- 新事件提交到 Today 可见：正常机器上目标 < 10s（取决于上游采集间隔，不承诺 OCR 立刻出现）。
- Session Review 不阻塞采集和手动修正；长轨迹只按页加载。

这些目标是工程目标而非对现有系统的实测承诺。

---

## 15. Coding Agent 执行协议

1. **先阅读 `GOAL_PROMPT.md` + 本文，再查看真实仓库**。写简短仓库适配记录，不凭想象创建与现有项目冲突的结构。
2. 分版本实施，每个版本保持可运行、可测试；每次 PR/提交描述迁移影响、测试结果和可复现演示。
3. 当数据缺失时显式显示 `Unknown/Unassigned/Unverified`；永远不要捏造“完成率、生产力分数、Agent 测试通过”之类的事实。
4. 所有 AI 结论都应有 evidence references；评估模型升级不得悄悄重写历史事实。
5. 新功能必须先确认是否与已有采集器/DB/任务系统重复；优先扩展现有模块或通过 adapter 对接。
6. 如果无法访问实际 journal 仓库，**仅实现架构骨架和明确标为 demo 的 fixtures**，并列出需要对接的真实接口；不可声称已集成真实数据。

---

## 16. 最终验收演示流程

打开 Today → 选择某天 → 看三轨时间轴（含 unknown） → 点击一个时间块 → 查看 OCR / Session / tool call 原始证据 → 将活动归属修正到另一个项目 → 查看统计同步变化 → 打开对应 Session Review → 检查两个评分维度的依据、未验证项与 Prompt 改写建议 → 复制改写建议 → 进入 Analytics 验证趋势口径 → 查看 Projects 的成果和里程碑 → 导出脱敏的离线 HTML 日报。

如果以上流程在真实数据上可以操作、行为可追溯、测试覆盖关键边界，本系统才算达到设计目标。
