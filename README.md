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

- 今日: 第一屏是 KPI(你在电脑前活跃 / Agent 去重后的在跑时长 + 峰值并行 / 会话 / 你发的 prompt / 提交)和每日洞察;核心是一条语义缩放时间轴:
  - 最上面是「全部」总览泳道: 整天放在一条轨道上,每一小段时间归给当时占主导的项目(全天看是大色块,放大自动拆成单个窗口),agent 执行以每个项目一条细线叠在同一泳道底部,▲ prompt 与 ◆ 提交也在其中;下方「按项目展开」才显示逐项目泳道,两者共用小地图取景框、时间轴、悬停读数与缩放/平移操作
  - 每个项目一条泳道: 上面是你的前台窗口,下面是该项目 agent 的执行(越深并行越多)、你发的 prompt(▲)和提交(◆);浏览/阅读等非项目活动与只碰了几分钟的项目合并在「其他活动」泳道
  - 语义缩放: 画不下的窄窗口合并成浅色段(内部实色是真实停留),够宽的窗口单独成块并显示标题和时长;全天看概况,放大到 10 分钟就是逐个窗口。顶部小地图显示全天活动密度和可拖动的取景框,工作段作为章节条,点击即缩放到该段
  - 鼠标悬停读出那一秒: 在哪个项目/应用/窗口、已停留多久、从哪个窗口跳来、哪些 agent 在跑;放大后项目之间的切换画成连线
  - 工作段: 5 分钟槽取主导标签,<15min 的短暂切出并入当前段;浏览/阅读时段若同时段的提交与 prompt 指向同一项目,则按该项目命名;摘要优先提交,其次有信息量的窗口标题或 prompt
  - 终端窗口标题是 tmux/shell 名,不代表项目: 终端时间按同一时刻(±3min)你发 prompt 的会话项目归属;claude 子代理和 harness 注入的 "user" 消息不计入你的 prompt;人工修正始终最高优先
  - 交互: 拖拽平移,⌘/Ctrl+滚轮或捏合缩放,双击放大,键盘 ←/→/+/−/0;右栏三个标签: 今天做了什么(工作段叙事)/ 活动流(跟随时间轴视野逐条列出窗口停留,穿插 prompt 与提交,与泳道双向高亮)/ 详情(时间构成、提交、会话、prompt、整段修正归属)
- 修正不丢数据: 所有人工修正 append-only 追加(带 anchor),重跑采集/重新投影后仍然生效,可随时撤销
- 分析: 近 7/14/30 天或自定义范围;顶部 6 个指标(日均电脑前活跃、日均 Agent 在跑(去重)、归入项目比例、长时间专注占比、每小时切换、提交/Prompt)都与前一个等长周期对比;「时间都去哪了」按项目堆叠(无窗口数据的日子标出而非画成 0,点柱子打开那天);由数据计算的洞察(附依据和可信度,样本少会明说);你 vs Agent 分项目投入;专注分布(连续段长度);日期×小时的节奏热力图;Agent 每日执行与 prompt。只统计你自己的提交(全局与各仓库的 git user.name,可在 webapp/config.json 的 git_authors 补充),协作者被拉进来的提交不计。项目配色使用经色盲校验的 8 色调色板,近 30 天投入最多的 8 个项目各占一色,其余为灰色
- 会话: 按当天/近 3 天/近 7 天、项目、agent 筛选与全文搜索;每个会话以你的第一条指令为标题,显示执行时长、指令数、工具调用数、token,claude 子代理折叠在父会话下;详情按对话排版(你的指令突出,工具调用合并成标签,可「只看我的指令」);质量评审按固定 rubric(你的指令 4×25 + agent 执行 5 项)打分并给改进建议,配置了百炼密钥时用 qwen3.8-max,否则用本地 pi
- 项目: 每个项目近 30 天你的时间、Agent 执行、会话、提交、最近活跃和 30 天趋势,优先级与当前目标可直接编辑;归类规则折叠在卡片里
- 日报: 浏览所有日报/周报(渲染后的正文 + 飞书文档链接);按星期几开关日报自动生成、开关周报和飞书推送(设置存于仓库外 ~/.config/ai-work-journal/schedule.json,daily.py/weekly.py 仅在被 systemd 定时器触发时遵守,手动 journal daily 不受限);显示定时器下次/上次运行结果,可「立即生成」
- 项目与归类规则: 每个项目的规则是数据(关键词/目录/正则/归并到父项目/忽略),存于项目表,在「项目」页直接编辑;project_roots 下的目录自动登记为项目并带上目录规则,webapp/config.json 的 classification.seed_projects 提供一次性初始规则。窗口按「标题里的路径 → 你的正则 → 关键词(最长优先,_-./ 视为分隔,中文按子串,允许数字后缀/名称缩写) → 应用分类 → 其他应用」判定,会话与提交按目录最长前缀;每个窗口都带归类原因(时间轴悬停可见)。右侧「未归类的窗口」按时长列出,可一步建规则并先预览影响;「试一试」可解释任意标题的归类。未知应用不再变成项目
- 问 AI: 每页右下角(或 ⌘/Ctrl+J)打开对话面板,用自然语言问自己的工作,例如“上周 paperfacts 有哪些进展”“10-07 下午两点我在干嘛”“我给 agent 的指令有什么坏习惯”。
  - 模型(百炼 qwen3.8-max)按渐进式披露调用 12 个只读工具,一般 1–4 次调用就够:
    - 先看系统提示里的一屏目录:今天/本周/上周的日期、已同步的日期范围、各项目近 30 天投入;
    - 再看概要:`list_projects` / `range_summary` / `get_day`,或用 `search` 跨所有来源搜一个词;
    - 需要细节再列行:`find_sessions` / `get_windows` / `list_commits` / `screen_text`(screenpipe 截图 OCR)/ `feishu` / `list_reports`;
    - 最后才读原文:`read_session`(默认只看你的指令)/ `read_report`。
  - 每个工具结果有长度上限和分页,并提示下一步该调哪个工具。
  - 回答里的会话、日期、日报都是链接,点击跳到对应页面;OCR 截图直接嵌在回答里。screenpipe 压缩进 mp4 的帧会用 ffmpeg 现取,并缓存在 `~/.cache/ai-work-journal/frames`。
  - 提问时会带上当前页面、日期和选中的会话,所以可以直接问“这个会话做了什么”。
  - 对话存在 data/webapp.db,可从「历史」继续。较早轮次的工具结果在回放时会省略,长对话也不会越来越贵。
  - 模型能看到你的会话原文和窗口标题,这些内容会发给百炼。
- 实时: SSE 推送采集变更;服务每 5 分钟自动增量同步一次,"同步"按钮可立即拉取 ActivityWatch/会话/git 事件(幂等,重复扫描不产生重复事件)
- LLM 评审默认关闭;开启: `webapp/config.json` 里 `"llm": {"enabled": true}`
- 语义归类(阿里百炼,OpenAI 兼容接口): 规则匹配不到的窗口标题去重后交给模型(默认 qwen3.8-max)判断,只采用把握 ≥0.7 的结果,按标题缓存,项目集合变化后重问;服务启动、同步新数据、改项目规则时在后台自动运行。密钥放在仓库外 `~/.config/ai-work-journal/.env`(`BAILIAN_API_KEY`、`BAILIAN_BASE_URL`,权限 600),开关与模型在 `webapp/config.json` 的 `llm_classify`。发送的内容只有窗口标题、项目名/关键词/目录名和每个项目的若干已归类标题样例

技术结构:

```
webapp/
  backend/    FastAPI: app/adapters(aw|sessions|git) → SQLite WAL (data/webapp.db)
              app/timeline.py 投影, app/projects.py 归属, app/reviews.py 评审,
              app/chat.py 问 AI(工具循环 + 对话存储)
  frontend/   React+TS+Vite+Tailwind+ECharts, 构建产物由后端托管
  config.json webapp 覆盖配置(llm.enabled 等)
```

验收要点(已验证): 重复扫描 3 次事件数不变;人工修正撤销后恢复推断值;跨天事件正确归属;无 ActivityWatch 数据的日期仅显示会话与证据;前端 73 项真实浏览器断言(含 问 AI 的真实对话、会话跳转与截图加载、日期热力日历、跨天会话与截屏状态提示)(`node webapp/frontend/verify-today.mjs`,需服务运行;截图在 `webapp/screenshots/`)。
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
