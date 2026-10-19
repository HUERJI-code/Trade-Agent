# 自由 SELECT 查询与代码阅读指南

2026-09-10：按用户要求，网页与 CLI 已改为模型生成完整 SQL，由程序校验后执行；删除了 FastAPI 的 `TrustedHostMiddleware`。这是实际默认链路变更，不只是增加一个未被调用的示例工具。

## 当前数据链路

React 发送 `POST /api/ask`（question、可选 conversation_id）→ FastAPI 校验请求 → `ConversationAgent.ask()` → LangGraph 恢复该会话状态 → `SQLAnalysisAgent.ask()` → 向 Ollama 提供真实表结构、业务口径、最近三轮历史 → 模型调用 `query_database(sql)` → `SQLTool.run_sql()` → AST 校验 → MySQL 只读账户执行 → 通用结果表 → 回答模型与事实检查 → 保存会话 → 返回网页。

模型提交的是完整 SQL 文本；`bind_tools()` 只提供名称、说明和参数结构，不会执行 SQL。实际执行发生在 `SQLAnalysisAgent.ask()` 内 `self.runner(args)`，默认 runner 为 `query_database.invoke`。工具再调用现有 `tools/sql_tool.py`，只执行校验器重新生成的 SQL。

LangGraph 当前节点仍为 `resolve_context → query → write_answer → remember`。SQL 生成、执行及最多一次修正发生在 query 节点内部，未虚构成多个独立图节点。

## 按请求经过的顺序读代码

1. `api/main.py`：HTTP 中间件、AskRequest 数据校验、`/api/ask` 路由、线程池及单次分析锁。Host 白名单中间件已移除；请求体大小、JSON 类型、同源和跨站检查保留。
2. `agent/graph.py`：SQLite 会话与 LangGraph。默认实例为 `SQLAnalysisAgent`；SQL 模式不再调用旧的单时间/单国家条件补齐规则。将当前问题原样传入，附上本会话最近三轮的问题、计划和必要澄清说明；不复制完整历史结果表，也不读取其他会话。
3. `agent/sql_agent.py`：新主入口。按 `SQLArguments / QuestionArguments → @tool → load_sql_context → SQL_SYSTEM → SQLModelClient.propose → SQLAnalysisAgent.ask` 阅读。工具目录为 `query_database` 和 `ask_for_details`，SQL 提议失败最多修正一次。
4. `tools/schema_tool.py`：从真实 MySQL 反射结构并过滤字段。只提供获准表、列和外键关系，不传客户隐私字段或凭据。
5. `config/business_metrics.yaml`、`tools/business_tool.py`：提供既有口径说明和确定性指标参照；自由 SQL 不再通过 `run_metric` 执行，不再受六指标参数枚举、单日期范围和固定分组的限制。
6. `tools/sql_tool.py`：执行前调用 SQL 校验器，使用只读连接、5 秒查询超时、最多 2000 行输出及脱敏日志。
7. `security/sql_validator.py`、`security/permissions.py`：检查整棵语法树、表/列、函数、作用域和资源上限；仅执行重建的 SQL。本轮新增常见窗口函数支持，以及遗漏 JOIN 时的表别名提示。
8. `database/connection.py`：读取本地连接配置，创建并检查专用只读引擎。模型不能替换账户或获得数据库口令。
9. `agent/answer_style.py`：新 `rewrite_sql()` 接受任意结果列，少量结果交给现有微调模型；数值变动、漏值、估算标记缺失或生成不完整时回退。大表、无记录和触及系统上限时直接保留确定性说明。
10. `agent/graph.py` 的 `remember()` 与 `ask()`：保存回答、更新会话目录，将完整结果交回 FastAPI；`JSONResponse` 返回 JSON。`frontend/src/main.jsx` 已适配通用列，省略前端逐行讲解时读到这里即可。

新代码已附中文注释。旧 `agent/service.py`、`agent/planner.py`、`agent/langchain_model.py`、`agent/langchain_tools.py` 保留用于历史阅读、旧测试和复用帮助函数，已不是网页/CLI 默认 SQL 规划路径。不能再把旧 `preflight()` 的拦截词当成当前网页能力限制。

## 已放开的查询形状

支持模型组合多个指标、多个国家和期间、筛选与排除、GROUP BY、HAVING、ORDER BY、LIMIT、Top N、子查询、非递归 CTE、UNION，以及 ROW_NUMBER、RANK、DENSE_RANK、LAG、LEAD 等已允许窗口函数。

示例：

- 2019年德国销售额比2018年德国销售额增加了多少？
- 那法国呢？（接着上一问）
- 2019年德国和法国分别有多少订单，销售额是多少？
- 2019年德国销量最多的前5个品牌，不包括Contoso，按销量从高到低排序。
- 2019年德国按月统计销量。

缺少时间且不是追问时，模型可按全部样例期间查询并说明范围；多个年份、多个国家不再被固定规则判成歧义。真正含义不清时仍可以澄清。差额、增长率等在 SQL 中计算，回答模型不负责心算。

## “只能 SELECT”仍包含哪些边界

自由生成指不再固定业务查询形状，不代表关闭所有数据库边界。仅允许一条业务 SELECT，或最终为只读查询的非递归 WITH/UNION。仍拒绝 INSERT、UPDATE、DELETE、DDL、多语句、SELECT INTO OUTFILE、文件/系统函数、变量赋值、锁、递归、笛卡尔积、未知及敏感字段。即使外层是 SELECT，也会检查嵌套内容。

当前要求显式输出字段，SELECT * 和不访问业务表的 SELECT 1 仍不属于该业务工具支持范围。SQL 函数采用白名单，未收录的合法 MySQL 函数也可能被拒绝；这不是“所有 SELECT 均可无条件执行”。默认输出最多 2000 行、执行超时 5 秒；扫描量并不由 LIMIT 保证。

自然语言到 SQL 的语义不再受旧 `ground_plan` 交叉检查保护：模型仍可能遗漏筛选、关联错误、混淆口径或错误处理空数据。AST 和只读账户保证访问边界，不能证明业务正确性。结果中保留实际 SQL 供核对。金额仍是估算，数据仍是历史教学样例，不能推断实时业务、实际净利润或未来预测。

结果表可显示任意数量的列。两列、第一列为文本类别、第二列为数值且行数适中的结果可绘图；其他结果展示表格，不猜测多指标共用单位。老会话原有结果仍可显示。

## Host 设置

已删除 `TrustedHostMiddleware` 导入及注册；`Host: trade.example:8000` 搭配同源 Origin 已通过真实接口验证。仍保留 Origin/跨站检查，两者与 Host 白名单不同。

服务启动地址仍为 `127.0.0.1:8000`。删除 Host 检查不等于改为监听局域网，也不代表已有登录、用户授权或多租户隔离。

## 验证与模型设置

本轮 257 项离线测试通过，前端构建通过；7 个真实网页/模型/数据库场景通过，记录见 `outputs/evaluation/sql_release.json`。场景包括跨期差额、追问、多国多指标、品牌 Top N 排除、按月统计、拒绝写库及拒绝敏感信息。跨期金额使用既有确定性业务工具作独立参照。

补充独立复核：品牌 Top 5 的值和排序、12 个月销量均与确定性查询一致，写入同一报告的 `additional_reference_checks`。浏览器实际验证两国多指标表格，以及 12 行月份结果和 Plotly 图表，无页面错误提示。最终配置下再次查询德国跨年差额成功，采用自然中文 LoRA 回答，见 `outputs/evaluation/sql_final_smoke.json`。

德国 2018 年估算销售额为 1,336,981.32 美元，2019 年为 1,874,921.45 美元，增加 537,940.13 美元；法国对应差额为 162,204.04 美元。这些值由数据库计算，不存入模型记忆。

该次 5 条成功查询中，1 条采用 LoRA 回答、4 条触发模板回退；其余 2 条为拒绝/澄清，不调用回答模型。模型的工具能力扩展不等于回答风格训练效果更好：本次没有重新训练模型。真实测试数量很小，不代表任意 SQL 生成准确率。早期 234 项测试和 16 个固定指标场景属于历史版本记录。

SQL 模型仍为 `trade-agent-qwen3:8b-lora-v1`。`config/local_model.yaml` 现设上下文 8192、SQL 生成上限 1024 token；回答上限仍为 384。没有下载新的模型，也没有进行新一轮调参实验。

重启方式不变：确认 MySQL 运行，执行 `./scripts/local_model.ps1 start` 和 `./scripts/start_web.ps1`。已有服务时直接刷新页面。
