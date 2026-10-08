# ai-work-journal

自动监控屏幕和 agent 会话,每天生成日报,每周生成周报。报告写入本地,并推送飞书。

## 架构

```
采集层(常驻)
  ActivityWatch   窗口焦点 + 空闲检测   systemd: aw-server + aw-awatcher
  ScreenPipe      屏幕 OCR + 录音        systemd: screenpipe (端口 3030)
  GNOME 扩展       focused-window-dbus   给 awatcher 提供 Wayland 窗口信息

数据源(按需读取)
  ~/.claude/projects/**.jsonl    Claude Code 会话
  ~/.pi/agent/sessions/**.jsonl  pi 会话
  project_roots 下各 git 仓库    当日提交
  飞书(lark-cli 用户身份)      当天会话消息、收发文件及内容预览

流水线
  collect.py    采集某天数据 → data/<date>.json(含小时分布/项目归属/token/逐轮对话) + data/<date>.digest.md
  analyze.py    会话记录 → pi 无头分析 → 写回 data/<date>.json 的 analysis 字段(每会话概括/产出/统计/改进建议)
  daily.py     collect → analyze → pi 总结 → reports/<date>.md → 推飞书 → 刷新看板
  weekly.py     7 天日报 → pi 总结 → reports/weekly/<isoyear>-W<ww>.md → 推飞书
  dashboard.py 汇总全部 data/*.json → reports/dashboard.html(离线交互看板)
  feishu.py    markdown 导入飞书为 docx(lark-cli 未授权时自动跳过)

模板
  templates/daily.md      日报模板+提示词(百度文库/飞书模板中心标准结构)
  templates/weekly.md     周报模板+提示词
  templates/dashboard.html 看板页面模板(内联 SVG,无外部依赖)
  改模板只需编辑这些文件,含 {date}/{label} 占位符,保存即生效。

调度(systemd 用户定时器)
  ai-journal-daily.timer    每天 23:00
  ai-journal-weekly.timer   每周五 23:30
```

## 日常使用

```bash
journal status                        # 看服务/定时器/最近报告(journal 已装到 ~/.local/bin)
journal daily                        # 立即生成今天日报
journal daily 2026-10-03             # 补生成某天
journal weekly                       # 生成本周周报
journal dashboard                    # 刷新可视化看板并打开浏览器
journal analyze 2026-10-03          # 补某天的对话 LLM 分析(--force 重跑)
journal feishu reports/2026-10-05.md # 手动推飞书
```

## 可视化看板(静态版)

`journal dashboard` 生成 `reports/dashboard.html`,浏览器直接打开,离线可用:

- 总览: 每日时长趋势、项目堆叠、时段热力图(小时×日期)、项目占比、token 消耗(按模型)、会话数、工程产出、最近 7 天对话分析摘要
- 单日: 顶部可任选日期(前/后一天、下拉);24h 时间线、小时明细、会话统计表
- 对话分析: LLM 生成的当日内容统计与改进建议;每个会话一句话概括+产出,展开看逐轮输入与触及文件
- 点击柱子或热力图行进入单日;支持 `dashboard.html?day=2026-10-07` 直链
- 每晚日报流程自动 collect → analyze → 报告 → 刷新看板;白天手动跑 `journal dashboard` 纳入实时数据,`journal analyze <日期>` 补分析

## Web 交互看板(新版)

`journal web` 启动本地 Web 应用(FastAPI + React,仅绑定 127.0.0.1:8799)并打开浏览器:

- 今日: 活动时间轴为页面核心 —— 滚轮缩放/拖拽平移,聚焦活动/工作时段/全天三档预设,聚合 LOD(远视图合并同项目相邻块并标时长,近视图还原真实窗口块);人类/Agent/证据三轨分开绘制不重复计数
- Agent 时长区分“生命周期”与“真实执行”: 会话事件按 >10min 空闲切分为执行段,泳道画细生命周期线 + 实心执行条,KPI 用执行时长(长会话等待不冒充运行)
- 证据链: 点击人类块/Agent 会话打开详情面板(窗口标题、期内证据、同时段会话、追加式修正表单);未知区间可标注归属
- 右栏为基于证据的 Daily Insights(最长专注段、人机重叠比、凌晨自动执行、最大未知区间、项目投入、commit 结果),非 LLM 生成
- 修正不丢数据: 所有人工修正 append-only 追加(带 anchor),重跑采集/重新投影后仍然生效,可随时撤销
- 分析: 日期范围内人类/Agent 时长趋势、项目堆叠柱图(ECharts)
- 会话: 列表+详情,完整对话与工具调用;质量评审按固定 rubric(提示 4×25 + 执行 5 项)打分并给可执行改进建议,评审结果按内容指纹缓存
- 项目: 自动生成项目列表,优先级/目标为用户所有,重跑不覆盖
- 实时: SSE 推送采集变更;"同步"按钮增量拉取 ActivityWatch/会话/git 事件(幂等,重复扫描不产生重复事件)
- LLM 评审默认关闭;开启: `webapp/config.json` 里 `"llm": {"enabled": true}`

技术结构:

```
webapp/
  backend/    FastAPI: app/adapters(aw|sessions|git) → SQLite WAL (data/webapp.db)
              app/timeline.py 投影, app/projects.py 归属, app/reviews.py 评审
  frontend/   React+TS+Vite+Tailwind+ECharts, 构建产物由后端托管
  config.json webapp 覆盖配置(llm.enabled 等)
```

验收要点(已验证): 重复扫描 3 次事件数不变;人工修正撤销后恢复推断值;跨天事件正确归属;无 ActivityWatch 数据的日期仅显示会话与证据;前端 17 项 DOM/几何断言(`webapp/frontend/verify-today.mjs`,截图在 `webapp/screenshots/`)。
```

## 首次配置残留事项

飞书推送需要授权一次(浏览器点击):

```bash
lark-cli config init --new
```

授权完成后,日报自动同时写入飞书,无需其他操作。

## 关键路径

| 内容 | 路径 |
|---|---|
| 项目代码 | ~/Projects/ai-work-journal |
| journal 命令 | ~/.local/bin/journal (符号链接) |
| 原始数据/摘要 | data/*.json, data/*.digest.md |
| Web 看板数据库 | data/webapp.db (webapp 专用,gitignore) |
| 日报 | reports/*.md |
| 周报 | reports/weekly/*.md |
| 可视化看板 | reports/dashboard.html |
| 配置 | config.json |
| ScreenPipe API 令牌 | ~/.config/ai-work-journal/screenpipe-token |
| ActivityWatch 界面 | http://localhost:5601 |

## 修改采集范围

编辑 config.json:

- project_roots: git 扫描根目录列表
- top_apps / top_titles_per_app / sp_samples / sp_sample_chars: 摘要容量(sp_samples 条 OCR 样本,每条 sp_sample_chars 字)

改完运行 `journal collect` 验证。

## 修改报告模板

编辑 templates/daily.md 或 templates/weekly.md,保存即生效。
下次生成(每晚 23:00 / 每周五 23:30,或手动 journal daily/weekly)即按新模板输出。
模板里 {date} 和 {label} 是占位符,保留即可。

## 服务管理

```bash
systemctl --user status aw-server aw-awatcher screenpipe   # 采集服务
journalctl --user -u ai-journal-daily.service -n 50        # 看日报生成日志
```

## 已知限制

- ScreenPipe 无麦克风输入时健康检查显示 degraded,不影响屏幕采集。
- OCR 中文样本有乱码,仅作线索,不影响日报主体。
- 飞书消息只采集授权身份可见的会话;最多下载 8 个文件,每个预览 1200 字。
- 图片只记录不读内容;PDF 读前 3 页;docx 提取正文文本。
- ActivityWatch 应用时长从部署时刻起累计,历史天数无窗口数据。
- pi 会话目录名按 "-" 解码路径,目录名本身含 "-" 时会得到近似路径。
