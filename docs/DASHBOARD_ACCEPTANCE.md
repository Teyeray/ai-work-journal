# DASHBOARD_ACCEPTANCE.md — 与 DESIGN_SPEC 的验收对应

状态标注:✅ 完成 | 🟡 部分 | ❌ 未完成(如实,不虚报)
验证方式:pytest(20 项,webapp/backend/tests)+ headless Chrome 端到端(puppeteer-core)。

## V1 — Usable Timeline

| DESIGN_SPEC 要求 | 状态 | 验证 |
|---|---|---|
| 数据适配与增量索引(AW / 会话 / git) | ✅ | test_sessions.py: 重复扫描不增长、追加只增新行 |
| 事件幂等(稳定 event_key) | ✅ | 生产库连续 3 次全量扫描新增 0;tests 覆盖 |
| 截断/轮转文件恢复 | ✅ | test_truncated_file_rescans(旧事件删除后重建;此为测试驱动修复的真 bug) |
| 跨午夜事件按日裁剪 | ✅ | test_midnight_event_clipped_to_day |
| Today 时间轴(人/Agent 双泳道) | ✅ | 前端 DayTimeline.tsx;E2E 截图验证 841 rects |
| 人的活动不与 Agent runtime 重复相加 | ✅ | test_agent_runtime_counted_once_not_added_to_human;KPI 单列"会话总和 vs 并集" |
| away(afk)与 unknown(观察空档)分开 | ✅ | test_human_afk_and_unknown_sum_to_observed(和恒等观察跨度) |
| OCR 缺一小时 → 显示未知,不补成工作 | ✅(以 AW watcher 缺失等同实现;仓库无 OCR 源) | test_missing_hour_is_unknown_not_work |
| 重复/重叠 afk 事件去重 | ✅ | test_dedupe_overlapping_afk(union 语义) |
| 项目/活动标签(规则+用户规则) | ✅ | projects.py 规则层 + DB 规则;分类回退 |
| 证据抽屉(commit/用户输入/会话) | ✅ | /api/v1/evidence + BlockDrawer;仅渲染关联事件 |
| 人工修正(append-only,不丢) | ✅ | test_override_survives_reprojection(撤销恢复推断值) |
| 重启后继续同步 | ✅ | 游标持久化(SQLite);uvicorn 重启后增量验证 |
| 无观察数据的日期优雅降级 | ✅ | test_no_observation_day + 前端空态 |
| SSE 当天数据变更刷新 | ✅ | sse.ts + api.py revision 广播 |
| 本地回环绑定 | ✅ | 127.0.0.1:8799;journal web 启动 |

## V2 — Agent Intelligence

| 要求 | 状态 | 验证 |
|---|---|---|
| Sessions 浏览器(筛选/搜索/分页) | ✅ | Sessions.tsx + list_sessions(project/agent/query/cursor) |
| 会话详情:完整对话 + 工具调用 | ✅ | 详情接口返回事件链;测试 test_sessions_list_and_detail |
| 分维度评分(提示 4×25 / 执行 5 项) | ✅ | reviews.py rubric;真实 LLM 会话评审一次通过(9 维分数+建议+证据引用) |
| 证据绑定(评分引用事件 id) | ✅ | evidence_refs;前端可跳转 |
| 改进建议可复制 | 🟡 | 建议已展示为列表;一键复制按钮未做(文本可选中复制) |
| 未验证状态(缺测试结果不装作已证实) | ✅ | 评审 LLM 提示词要求依据记录;会话级 evidence 显式列出;评审失败/未启用显示状态 |
| 人工反馈回路 | ✅ | /api/v1/reviews/feedback(helpful/partially/not_helpful/wrong) |
| LLM 默认禁用、显式开启 | ✅ | llm.enabled 默认 false;health 可见 |
| 密钥脱敏 | ✅ | _redact 修为变长匹配;test_evidence_redaction + reviews._redact 测试 |
| Prompt 改写建议(可复制整段) | 🟡 | rubric 输出建议条目;未生成完整改写 Prompt(标注为后续) |

## V3 — Analytics & Projects

| 要求 | 状态 | 验证 |
|---|---|---|
| 日期范围人类/Agent 趋势 | ✅ | analytics/time + Analytics.tsx ECharts |
| 项目投入堆叠 | ✅ | 项目堆叠柱图;类别伪项目灰色 |
| 日投影缓存(指纹) | ✅ | daily_projections;冷 0.2s / 热 1ms |
| 计划/实际对比 | ❌ | planned_blocks 表已建;无 UI(仓库无计划数据源,spec 允许 manual-only 后置) |
| 周/月视图联动 | 🟡 | 自由日期范围覆盖周/月;无预设周/月 tab |
| 重复问题识别(跨会话) | ❌ | 未做 |
| 成果/里程碑/阻碍 | 🟡 | outcomes 表已建;Projects 页有优先级+目标;里程碑/阻碍 UI 未做 |
| HTML 单日快照导出 | ❌ | 未做(现仅在线视图) |

## 关键验收场景(spec 15 条中的核心)

| 场景 | 结果 |
|---|---|
| 同一批日志扫三次,事件数不增 | ✅ 生产库 26,396 → 26,396 → 26,396 |
| Agent 与人重叠不重复计时 | ✅ human_active 不含 agent;runtime 单独两口径(总和/并集) |
| 观察缺失一小时 | ✅ unknown 区间琥珀色渲染,可标注;不自动补工作 |
| 修正项目标签后重分析仍保留 | ✅ append-only + anchor 语义;撤销可逆 |
| 会话缺测试结果 | ✅ 评审标注"无最终校验";不显示已验证 |
| 原始日志中的指令不作为系统指令执行 | ✅ 会话内容只作数据传入 pi(附件),评审提示词为固定模板;secret 脱敏 |
| 日志追加/跨午夜/重启 | ✅ tests + 生产验证 |

## 技术限制(如实)

1. pi headless 调用需要位置参数 prompt + @附件 + stdin=DEVNULL,约 60s/次评审;评审按 (session, analyzer_version, input_fingerprint) 缓存,重触发秒回。
2. ActivityWatch 历史自 2026-10-05 起;更早日期仅会话与 git 证据。
3. pi 会话目录名推断 cwd 对含连字符路径是近似的;项目归属用子串匹配兜底。
4. reviews 的 analyzer_version 现为 "v1";rubric 变更时需 bump 使缓存失效。

## 测试与复现

```bash
cd webapp/backend && ../.venv/bin/python -m pytest tests/ -q   # 20 passed
journal web    # http://127.0.0.1:8799
```
