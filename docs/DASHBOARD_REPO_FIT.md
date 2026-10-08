# DASHBOARD_REPO_FIT — 仓库事实与设计假设对照

日期: 2026-10-08
用途: 记录 `ai-work-journal` 仓库侦察结果、`DESIGN_SPEC.md` 中被仓库事实替代的默认假设、最终文件结构与迁移方案。

## 1. 仓库事实(实测)

### 1.1 现有能力(全部保留,不复用即不破坏)

| 模块 | 作用 | 与 Dashboard 的关系 |
|---|---|---|
| `collect.py` | 按天采集 AW/会话/git/GitHub/飞书 → `data/<date>.json` | 保持独立运行;Web 后端直接读原始来源,不依赖日 JSON |
| `analyze.py` | 每日一次 pi 无头 LLM 分析(会话概括/建议) | review 管线复用同一 pi 调用方式 |
| `daily.py`/`weekly.py`/`feishu.py`/`dashboard.py` | 日报/周报/飞书推送/静态 HTML 看板 | 不改动;静态 HTML 保留为"导出格式"的一种 |
| `journal` | CLI(status/daily/weekly/dashboard/analyze/install) | 新增 `web` 子命令启动 Dashboard |
| `config.json` | AW URL、project_roots、pi_bin、gh/lark 路径 | Web 后端读取同一份配置 |

### 1.2 数据源真实格式

| 来源 | 位置/接口 | 格式 | 事件粒度 | 稳定事件键 |
|---|---|---|---|---|
| ActivityWatch window | `GET :5601/api/0/buckets/aw-watcher-window_workstation-2700/events?start&end` | `{id:int, timestamp:UTC ISO, duration:sec, data:{app,title}}` | 每窗口切换一条,秒级 | AW `id`(库内自增,稳定) |
| ActivityWatch afk | 同上 `aw-watcher-afk_workstation-2700` | `{id, timestamp, duration, data:{status: "afk"/"not-afk"}}` | 状态变化点 | AW `id` |
| pi 会话 | `~/.pi/agent/sessions/<sanitized-cwd>/<uuid>.jsonl` | append-only JSONL: `session`/`message` 行,toolCall parts,usage | 每消息一条 | (文件,行号,行内容 hash) |
| claude-code 会话 | `~/.claude/projects/<sanitized-path>/<uuid>.jsonl` | append-only JSONL: user/assistant 行,tool_use/tool_result blocks | 每消息一条 | 同上 |
| git | `project_roots` 下仓库 `git log` | commit hash/时间/subject | 每提交一条 | commit hash |
| GitHub / 飞书 | `gh` / `lark-cli` | 事件带时间 | 按条 | URL/消息 id |
| screenpipe | `:3030` | **实测无 frames(服务未产生数据)** | — | 未启用 |

### 1.3 关键仓库事实 → 设计修正

1. **无 OCR/截图来源**。规格假设的 `screen_ocr` 在本机不存在(screenpipe 服务运行但 0 帧)。人类活动观察证据 = **ActivityWatch**(window 事件 + afk 状态),粒度秒级、连续、可靠。`Evidence` 轨的采样点 = git 提交、GitHub/飞书活动、会话消息。规格的"OCR 缺一小时→unknown"场景映射为"AW watcher 停摆区间→unknown"。
2. **人类在场判定** = afk 桶的 `not-afk` 区间与 window 事件相交。窗口事件在 afk 期间不算人类活动(机器亮屏但人不在)。
3. **无计划数据源**。滴答清单仅作为窗口标题出现,无 API 接入。Plan 面板 = 用户手工建立 `planned_blocks`(规格允许);无计划时 Plan adherence 显示 N/A。
4. **LLM**: 本机唯一可用 LLM 通道是 `config.json.pi_bin`(pi headless,实际后端为远程 GLM API)。按规格"外部 LLM 显式配置"处理:`webapp/config.json` 中 `llm.enabled` 默认 true(用户既有日报管线已显式使用同一通道),发送前做密钥脱敏;可一键关闭,关闭时评分为 `unreviewed`,时间轴/统计完全不受影响。
5. **时区**: 本机 `Asia/Singapore`(+08,无 DST)。`day_key` 按本地自然日切分;事件存储 UTC。
6. **历史数据可用性**: AW 数据自 2026-10-05 起;更早日期只有会话/git。时间轴在这些天显示"无观察数据"状态,不虚构。
7. **前端工程**: 仓库无任何 node 工程(README 曾引用的 `~/Documents/projects` 路径已过时,实际在 `~/Projects`)。因此按规格默认栈新建 React+TS+Vite;shadcn/ui 依赖链过重,以自研轻量组件替代(Tailwind 保留),此为记录在案的偏差。
8. **Python 环境**: 系统 Python 3.14 受 PEP 668 管控 → webapp 使用独立 venv `webapp/.venv`;journal 原脚本继续用系统 python,互不影响。

## 2. 最终文件结构

```
webapp/
  .venv/                  # python 虚拟环境(fastapi/uvicorn/httpx/pytest)
  config.json             # webapp 配置(端口/时区/LLM 开关/敏感排除)
  backend/
    app/
      config.py           # 合并 config.json + webapp/config.json
      db.py               # SQLite WAL + 迁移(schema.sql, user_version)
      timeutil.py         # UTC/本地日边界
      adapters/           # aw.py, sessions.py(pi/claude 共用 JSONL 逻辑), git.py
      ingest.py           # 游标驱动增量采集 + 幂等 upsert + 失效通知
      projects.py         # 项目规则解析 + 人工修正优先
      timeline.py         # 日时间轴投影(human/agent/evidence/unknown)
      sessions.py         # 会话投影与检索
      reviews.py          # rubric 评分(pi LLM,结构校验,版本化缓存)
      api.py              # FastAPI: REST + SSE + 静态前端
      export.py           # 离线 HTML 日报导出(脱敏)
    schema.sql
    tests/                # 幂等/边界/投影/API 测试
  frontend/               # Vite React TS(vite build → backend 静态托管)
data/webapp.db            # Dashboard 专用 SQLite(WAL);不影响 data/*.json
```

## 3. 迁移与安全策略

- 原 journal 的 `data/*.json`、`reports/`、模板全部只读,Dashboard 不写这些路径。
- `data/webapp.db` 为新增文件,纳入 `.gitignore`;删库重扫即重建(事件源不变)。
- 原始 JSONL 绝不修改;adapter 只读。会话文本在入库时保留原文(本地库),API/导出/LLM 边界统一走 `redact.py` 脱敏(密钥 pattern)。
- 服务只绑定 127.0.0.1;默认端口 8799(可配)。

## 4. 保留假设(遇到未知时的最小适配)

- AW 事件 `id` 全局稳定且不回收(实测 10-05 至今成立);若上游换库导致 id 重复语义,以 (bucket, id, timestamp) 复合键兜底。
- pi/claude JSONL 为 append-only;截断(size 变小)触发整文件重扫 + 内容键去重。
- 会话"运行中"判定:JSONL mtime 距今 < 30 分钟视为 running,不自动 review(规格 provisional 默认关闭)。
