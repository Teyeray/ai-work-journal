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
  collect.py    采集某天数据 → data/<date>.json(含小时分布/项目归属/token) + data/<date>.digest.md
  daily.py      digest → pi 无头总结 → reports/<date>.md → 推飞书 → 刷新看板
  weekly.py     7 天日报 → pi 总结 → reports/weekly/<isoyear>-W<ww>.md → 推飞书
  dashboard.py  汇总全部 data/*.json → reports/dashboard.html(离线交互看板)
  feishu.py     markdown 导入飞书为 docx(lark-cli 未授权时自动跳过)

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
~/Documents/projects/ai-work-journal/journal status   # 看服务/定时器/最近报告
journal daily                                        # 立即生成今天日报
journal daily 2026-10-03                             # 补生成某天
journal weekly                                       # 生成本周周报
journal dashboard                                    # 刷新可视化看板
journal feishu reports/2026-10-05.md                 # 手动推飞书
```

## 可视化看板

`journal dashboard` 生成 `reports/dashboard.html`,浏览器直接打开,离线可用:

- 总览: 每日时长趋势、项目堆叠、时段热力图(小时×日期)、项目占比、token 消耗(按模型)、会话数、工程产出
- 单日: 24h 时间线(前台项目分段 + agent 会话轨道 + 提交/飞书事件点)、小时明细、会话列表(任务/token/工具)、飞书搞录
- 点击柱子或热力图行进入单日;支持 `dashboard.html?day=2026-10-07` 直链
- 每晚日报生成后自动刷新;白天手动跑 `journal dashboard` 可纳入当天实时数据
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
| 项目代码 | ~/Documents/projects/ai-work-journal |
| 原始数据/摘要 | data/*.json, data/*.digest.md |
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
