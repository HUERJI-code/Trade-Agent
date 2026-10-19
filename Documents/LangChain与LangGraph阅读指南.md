# 工具调用和会话流程

最新版本：默认分析器已换为 `SQLAnalysisAgent`，工具目录为 `query_database(sql)` 与 `ask_for_details`。旧 `run_metric` 工具方案保留作历史学习，未作为当前网页默认执行路径。SQL 模式直接传当前问题和本会话最近三轮数据，由模型理解省略条件；不再运行旧单范围补齐。更新后的流程与逐文件阅读顺序见 [自由 SELECT 查询指南](自由SELECT查询与代码阅读指南.md)。

本轮按用户要求加入 LangChain、LangGraph、多轮追问和独立会话；原有 MySQL 只读、敏感字段、SQL 校验和业务指标计算继续复用。

## 旧调用与新调用

旧流程：`OllamaClient.generate` 收到指标说明和 PLAN_SCHEMA，模型返回 JSON；`AnalysisAgent` 校验计划后直接调用 Python `run_metric`。

新网页流程：React 提交 `question` 和 `conversation_id` → FastAPI → LangGraph 按 `thread_id` 恢复会话 → 补齐省略条件 → LangChain `ChatOllama.bind_tools` 提交工具目录 → 模型提出一个工具调用 → `QueryPlan.parse` 与 `ground_plan` 校验（失败时把具体原因反馈模型，最多重试一次） → LangChain 工具 `run_metric.invoke` → 原业务工具生成并执行只读 SQL → 回答阶段 → 保存会话。

`build_prompt(context, tool_call=True)` 为网页生成原生工具提示，去除旧版七字段 JSON 输出要求；CLI 默认仍使用 JSON 协议，避免同一次请求混用两种输出格式。country 是筛选条件，dimension 只表示明确要求的分组；模型混淆时校验器会拒绝，而不是直接执行。

模型仍由 `config/local_model.yaml` 选择。`QUERY_TOOLS` 是工具目录，不是模型目录；LLM 不扫描文件系统，也不自行选择另一模型。当前三个工具为 run_metric、clarify_query、unsupported_query。

## 每个新文件的职责

| 文件 | 职责 |
|---|---|
| `agent/langchain_tools.py` | `@tool` 包装业务查询、澄清与不支持动作 |
| `agent/langchain_model.py` | 用 ChatOllama 发送工具目录，将模型提议转成既有计划结构；不在这里执行数据库查询 |
| `agent/conversation.py` | 复用日期、国家识别，只继承本轮省略的已确认条件 |
| `agent/graph.py` | 定义状态、节点、连接方向、SQLite checkpointer 和会话目录 |
| `agent/answer_style.py` | 将可信结果转换为训练输入结构，调用回答模型，并检查数值等约束 |
| `config/answer_style_prompt.txt` | 与训练数据相同的回答风格提示 |
| `config/answer_model.yaml` | 只有实际评估和部署完成后才启用回答模型 |
| `api/main.py` | 创建、列出、读取会话；接收本轮问题 |
| `frontend/src/main.jsx` | 显示历史、切换会话、提交 ID、恢复刷新后的聊天、显示加粗文字 |

`agent/service.py` 仍是可单独测试的单次查询核心。CLI 目前继续使用原有单次入口；网页使用会话图。

## 状态如何工作

`question` 是当前原问题；`effective_question` 是补齐后的问题；`confirmed_scope` 是最近成功执行的计划；`pending_question` 用于缺时间/指标后的条件补充；`response` 是最新结果；`history` 提供最近 30 轮页面展示。

LangGraph checkpoint 保存在 `.runtime/conversations.sqlite3`，不会给业务 MySQL 连接增加写权限。每个服务端创建的 UUID 对应一个独立 `thread_id`。页面 URL 的 conversation 参数记录当前窗口；刷新后可以恢复该会话。SQLite 内还保留图检查点，30 轮只是展示上限，不是数据库清理策略。

同一会话示例：2019年德国销售额 → 那法国呢 → 订单数呢。第二轮继承时间和指标；第三轮继承时间和法国。新会话中直接问“那法国呢”没有可继承条件，需要澄清。

澄清示例：德国销售额是多少 → 请补充时间 → 2019年。只有明确补充缺少字段时才拼接待补充问题；完整的新问题不会套用旧条件。

## 没有自动改变的能力

跨两年比较、任意过滤、多个指标等仍要由查询计划与执行工具实现。LangGraph 保存会话不会自动增加 SQL 表达能力。自然语言条件继承目前是有边界的实现，并非所有省略、指代或话题跳转都已支持。

本项目仍是回环地址上的本机单用户应用，不提供多账号认证。不同会话用于隔离聊天状态，不等于权限隔离。后续若提供多人远程服务，需要认证、会话归属检查和适合并发的存储。

模型推理仍受全局锁限制：同时提交的第二个问题得到 429。多个独立聊天窗口已经可用，不代表 GPU 同时处理多个推理请求。

## 测试与阅读顺序

离线测试覆盖工具目录、未经校验不执行、日期与国家继承、完整问题重置、歧义保留、会话隔离、重启恢复、失败不污染已确认条件，以及后续补充时间。

聊天中的逐文件讲解先从 `agent/langchain_tools.py` 开始，随后是 `agent/langchain_model.py`、`agent/graph.py`、`agent/conversation.py`、`api/main.py`、前端，再向下阅读原有 planner、service、business_tool、SQLTool 和数据库连接。训练与部署文件在训练运行指南中串联。

官方参考：[工具](https://docs.langchain.com/oss/python/langchain/tools)、[ChatOllama](https://docs.langchain.com/oss/python/integrations/chat/ollama)、[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)。

2026-09-10 实际候选模型回归：`outputs/evaluation/live_lora_final/report.json` 的 12 个场景全部通过。初期失败报告保留用于说明协议修复过程，不能将它们删除后声称从未出错。聊天逐行讲解已完成 langchain_tools.py、langchain_model.py、graph.py（分两段）、conversation.py、api/main.py；阅读接下来进入 frontend/src/main.jsx，再向下阅读原有业务与 SQL 链路。

最终补测覆盖16个场景，报告为 `outputs/evaluation/live_lora_release/report.json`；全部流程检查通过，其中2条回答触发模板回退。工具字段已增加 Annotated 说明，可空字段字符串 null 被纠正为 JSON null 后仍通过原来的条件核对。全部国家分组问题在原模型同接口下也出现过失败，不能仅归因于 LoRA；这是工具协议说明与参数格式处理的修复。
