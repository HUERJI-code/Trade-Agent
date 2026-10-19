# 外贸数据分析 Agent

最新查询链路：模型直接生成完整 SELECT，支持多指标、多国、跨期比较、筛选、排序与 Top N，再由只读 SQL 工具校验执行。网页和 CLI 已切换，Host 白名单中间件已移除；257 项离线测试及 7 项真实场景通过。接口、代码阅读顺序和剩余边界见 [自由 SELECT 查询指南](Documents/自由SELECT查询与代码阅读指南.md)。下文固定指标阶段描述属于历史记录。

2026-09-10 空间清理：项目由约 95.82 GiB 降至 18.60 GiB，释放约 77.22 GiB。只保留已部署的微调模型和最终 LoRA 适配器，旧基座、合并权重、重复 GGUF 与检查点已删除。额外清理训练环境和缓存被自动审批阻止，训练环境已部分删除，重新训练前需重建。网页可正常运行；详见 [清理记录与项目范围](Documents/项目空间清理与范围局限.md)。

此前完成：LangChain 工具调用、LangGraph 独立会话、回答风格 LoRA 训练与 Ollama 部署。当时 234 项离线测试、16 项现场查询/会话检查通过；风格指标改善，但全部测试样本的原始回答自动检查率没有全面提升，详见 [训练评估与部署结果](Documents/LoRA训练评估与部署结果.md)。


新增 **LangChain 工具调用 + LangGraph 持久化会话**：网页支持连续追问、新建/切换聊天和刷新恢复。实现及阅读顺序见 [LangChain 与 LangGraph 阅读指南](Documents/LangChain与LangGraph阅读指南.md)。下文 Phase 4 为历史验收记录，模型训练与部署状态以最新运行报告为准。

回答风格已由用户确认，已生成 LoRA 消息数据 v1（JSON / JSONL，含训练、验证、测试划分），位于 `Data/answer_style/v1/`。先读 [v1 数据说明](Documents/LoRA回答训练数据_v1说明.md) 和 [精选样例](Documents/LoRA回答训练数据_v1精选.md)。训练阶段已获授权，状态见下方指南；网页已使用微调模型组织查询结果，异常回答保留模板回退，最初 20 条风格审阅样例作为历史版本保留。

Phase 4 已完成，并补充国家最高／最低查询：本地 Qwen3-8B 理解一般中文问题，明确的国家极值句式使用受控规则；两者复用已确认指标及只读 MySQL。FastAPI + React + Plotly 提供网页、结果表与图表。该历史阶段 220 项 pytest 测试、4 项导入测试通过；此前 15 条真实本地模型开发冒烟样例通过。后者不是独立泛化评测，也不是 LoRA 效果。

**LoRA 训练采用标准 Python SFTTrainer 流程。** 独立环境中使用 Qwen3-8B、PEFT 和 QLoRA，按加载模型、读取数据、配置训练、训练和保存的顺序阅读 `scripts/train_answer_style.py`。运行方法与实时状态位置见 [LoRA 训练运行指南](Documents/LoRA训练运行指南.md)。网页模型已切换为 `trade-agent-qwen3:8b-lora-v1`，LoRA 已合并并通过 GGUF Q4_K_M 导入 Ollama；此前阶段验收见 `Documents/phase3_report.md`、`Documents/phase4_report.md`。

## 先打开网页

当前本机地址：**http://127.0.0.1:8000**。示例问题：“2019年德国销售额是多少？”、“2019年德国按月统计销量”。

现在也可以问“2019年哪个国家销售额最高？”，结果为美国、估算销售额 10,133,066.38 美元。最高／最低比较保留完整国家明细和并列结果，说明见 `Documents/国家最高最低查询说明.md`。Top N 等复杂排名仍未支持。

重启电脑后先确认 MySQL 运行，在项目 PowerShell 7 终端启动模型与网页：

```powershell
./scripts/local_model.ps1 start
./scripts/start_web.ps1
```

已有服务时直接打开页面，不要重复启动。详细操作、前端构建方式、文件逐个讲解见 `Documents/网页运行与阅读指南.md`。网页已支持独立会话与条件继承：同一对话可追问“那法国呢？”，缺时间后可以补充“2019年”。

## 先用中文问一次

在项目终端执行（使用已有 `.venv`）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m agent.cli --check-model
.\.venv\Scripts\python.exe -X utf8 -m agent.cli --question "2019年德国销售额是多少？"
```

预期得到：**估算销售额为 1,874,921.45 美元**。返回的 `plan` 展示模型理解的指标、日期和国家；`result` 是实际数据库结果；`usage` 是本次模型 token 和耗时统计。数字由数据库计算，回答由代码按模板整理，模型不自行编造金额。

其他例子：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m agent.cli --question "2019年德国按月统计销量"
.\.venv\Scripts\python.exe -X utf8 -m agent.cli --question "德国销售额是多少？"
.\.venv\Scripts\python.exe -X utf8 -m agent.cli --question "2026年德国订单数是多少？"
```

依次演示分组查询、补充时间的提示、超出数据覆盖范围。当前每次命令独立处理；收到澄清后请重新输入带完整条件的问题，还没有跨轮保存上一句条件。

## 模型在哪里，怎样启动

- Ollama 独立运行库：`D:\Codex\Trade Agent\.runtime\ollama`。
- Qwen3-8B 模型权重：`D:\Codex\Trade Agent\models\ollama`。
- 服务自己的配置、缓存及临时目录：项目 `.runtime` 下；服务日志：项目 `logs` 下。
- 当前模型为完成回答风格 LoRA 微调、合并后量化的 `trade-agent-qwen3:8b-lora-v1`（Q4_K_M）；旧 `qwen3:8b` 已清理。

模型已下载，当前服务已经启动。电脑重启后，在 **PowerShell 7** 中运行：

```powershell
./scripts/local_model.ps1 start
```

只查状态用 `./scripts/local_model.ps1 status`。服务使用 `127.0.0.1:11434`，关闭 Ollama 云端功能；应用的模型客户端拒绝远程地址、代理与 HTTP 重定向。首次下载/部署全过程见 `Documents/本地模型部署指南.md`，不必为了阅读而重新下载模型。

## Phase 3 文件阅读顺序

1. `config/local_model.yaml`：模型名称、地址和推理参数。
2. `agent/local_model.py`：本机 HTTP 请求、结构化输出和错误处理。
3. `agent/planner.py`：提示词、JSON 计划、日期/国家/指标交叉检查。
4. `agent/service.py`：前置规则、模型调用、业务工具调用和答案整理。
5. `agent/cli.py`：终端参数与中文 JSON 输出。
6. `tests/test_agent.py`、`tests/test_local_model.py`：模拟测试与真实 MySQL 组合验证。
7. `scripts/model_smoke.py`：真实模型和真实数据库的开发冒烟验证。

完整学习路线见 `Documents/代码阅读指南.md`。当前接口支持单次一个指标、一个明确日期范围、一个或全部客户国家及一种分组，并支持明确年/月的国家最高最低比较。季度、Top N 排名、多国筛选、同比环比、预测和个人信息等请求暂不支持；展示结果时需核对返回的解析条件。模型语义理解仍可能错误，白名单校验不能证明任意问题都被正确理解。

**FastAPI + React + Plotly** 前后端已完成，并已加入 LangChain 工具调用与 LangGraph 持久化会话。LoRA 训练、合并、GGUF 部署与两组206条原始回答评估已完成，实际产物和指标见 [最终运行报告](Documents/LoRA训练评估与部署结果.md)。RAG、任意复杂分析及远程多用户部署尚未实现，不能列为现有成果。

## 按统一业务口径查询

以下定义已于 2026-09-09 由用户确认，并写入 `config/business_metrics.yaml`。

| 指标 ID | 中文名称 | 口径 |
|---|---|---|
| revenue | 估算销售额 | Σ（数量 × 产品美元标价） |
| gross_profit | 估算毛利 | Σ〔数量 ×（美元标价 − 单位生产成本）〕 |
| gross_margin | 估算毛利率 | 汇总估算毛利 ÷ 汇总估算销售额；分母零时为空 |
| order_count | 订单数 | 按订单号去重 |
| active_customer | 活跃客户数 | 所选范围内有订单明细的去重客户 |
| quantity | 销量 | 销售明细数量合计 |

时间按下单日期，市场默认按客户所在国家。金额已经是 USD，不再次按订单币种折算。必须明确起止日期；起始日包含，结束日不包含。相对时间例如“最近半年”需要先明确参考日期，当前工具只接收明确日期。缺失时间返回 time_range_required。没有真实成交价，金额与毛利率始终标注估算，不代表确认收入或净利润。

查询 2019 年德国客户的估算销售额：

```powershell
Set-Location 'D:\Codex\Trade Agent'
.\.venv\Scripts\python.exe -X utf8 -m tools.business_tool --metric revenue --start 2019-01-01 --end 2020-01-01 --country Germany
```

结果的 metric_value 应为 `1874921.45`，单位 USD；matching_sales_lines 为 `2208`。该列是参与计算的销售明细数，不是订单数。

将 `--metric revenue` 换成 `--metric gross_profit`、`--metric gross_margin`、`--metric order_count` 或 `--metric active_customer` 即可更换指标。毛利率返回比例值，展示百分比时乘 100。省略 --country 表示全部客户国家。

按产品类别查看估算毛利率：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m tools.business_tool --metric gross_margin --start 2019-01-01 --end 2020-01-01 --country Germany --dimension product_category
```

可选分组维度为 customer_country、product_category、order_month。每一组独立重算指标；订单数和活跃客户数属于去重指标，不可把不同类别或月份的结果直接相加当总数。

查看指标定义与运行测试：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m tools.business_tool --list
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
```

新环境按 `requirements-dev.txt` 安装；当前虚拟环境已安装 PyYAML 6.0.3，无需重复安装。模板 `config/business_metrics.template.yaml` 保持 draft，只供阅读与后续口径讨论；运行工具默认只读取 approved 的正式字典。

字典校验包括安全 YAML、重复键、必需指标、估算标签、去重定义、比率依赖及循环、真实字段与多对一关联约束。生成的查询仍必须通过 Phase 1 SQL 校验器和只读执行工具。为支持日期与国家组合条件，本阶段修复了 SQL 解析器将 AND/OR 当作函数时被误拦截的问题，并加入回归测试。

结果的 data_coverage 来自实际数据库日期范围。范围完全无交集时返回 outside_data_coverage 和空 rows，不能解释为零销售；部分超出范围时 partial 为 true。覆盖范围内没有匹配交易时为 no_matching_transactions；COUNT 返回 0，SUM 和比例保留空值。日期覆盖指最早至最晚记录之间的包络，不保证其中每天都有数据。

这套 Phase 2 指标工具现在由 Phase 3 的自然语言入口复用，仍可单独使用下面的 SQL 接口排查计算。

## 直接输入 SQL 的查询方式

在 PowerShell 中执行，使用项目现有虚拟环境：

```powershell
Set-Location 'D:\Codex\Trade Agent'
.\.venv\Scripts\python.exe -m tools.sql_tool --sql "SELECT COUNT(DISTINCT order_number) AS order_count FROM sales"
```

JSON 的 columns 是输出字段，rows 是查询结果。本例 rows 应为 `[[26326]]`，表示 26326 个订单；不是 62884 条销售明细。trace_id 可关联本次查询的审计日志。密码自动从 `.env` 读取。

客户国家订单排名示例：

```powershell
.\.venv\Scripts\python.exe -m tools.sql_tool --sql "SELECT c.country,COUNT(DISTINCT s.order_number) AS orders FROM sales s JOIN customers c ON c.customer_key=s.customer_key GROUP BY c.country ORDER BY orders DESC LIMIT 5"
```

观察字段访问被拒绝的示例：

```powershell
.\.venv\Scripts\python.exe -m tools.sql_tool --sql "SELECT customer_name FROM customers"
```

预期返回结构化错误与 trace_id，进程退出码为 1。客户姓名不会被查询返回。

## Phase 1 的代码分工

| 文件 | 用途 |
|---|---|
| database/connection.py | 读取配置；每次取连接核对身份与权限，并设置只读事务和超时 |
| security/permissions.py | 五张表的可用字段清单、行数和复杂度上限 |
| tools/schema_tool.py | 从真实数据库读取并筛选 Schema，不返回客户姓名、生日等字段 |
| security/sql_validator.py | 解析 MySQL AST、核对表与字段、限制语句类型和函数，并添加 LIMIT |
| tools/sql_tool.py | 只执行校验后的 SQL，返回结构化结果与 trace_id，写入脱敏审计日志 |
| tests/ | 单元测试及本机教学数据库集成测试 |

查询路径：真实 Schema → SQL 解析与字段检查 → 只读数据库执行 → 结构化结果。

接受明确列名的 SELECT、非递归只读 WITH、UNION、聚合及带 ON/USING 的关联查询。仅 COUNT(*) 可使用星号。系统库、未允许的表/字段、写操作、多语句、注释/优化器提示、递归 CTE、无条件关联、变量、文件读写、锁定查询和未批准函数均会被拒绝。当前函数集合是有限白名单，合法但未支持的 SQL 也可能被拒绝。

默认上限为 2000 行：缺少外层 LIMIT 时添加，超过上限时缩小，更小的 LIMIT 保留。返回中的 limit_reached 只说明达到上限，不证明还有更多数据。服务端语句超时默认 5000 毫秒，另设连接和客户端读写超时。金额 Decimal 序列化为字符串以保留精度，后续分析需要显式转换为 Decimal。

日志在 `logs/sql_queries.jsonl`，记录 trace_id、耗时、行数、错误分类、SQL 哈希与去掉字面量和自定义别名的 SQL 模板，不记录凭证、原始问题或查询结果。完整执行 SQL 仅随查询结果返回。日志目录已排除 Git。

客户敏感列限制属于本工具的访问策略；现有只读账号本身仍有五张完整教学表的 SELECT 权限。当前没有登录系统、按用户限制行范围或企业级权限隔离。

## Phase 1 测试与依赖

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/check_connection.py
```

已验证 94 项测试通过，包括实际 MySQL 查询、只读 CTE、关联聚合、按月分组、行数限制、SQL 特殊字符和超时后恢复。check_connection.py 另行复验了 MySQL 的 INSERT/UPDATE/DELETE 拒绝权限。

不连接 MySQL 时只运行单元测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not integration"
```

在新的项目虚拟环境安装依赖：`python -m pip install -r requirements-dev.txt`。当前 `.venv` 已准备完成，无需再次安装。SQLGlot 解析与字段绑定参考其[项目文档](https://github.com/tobymao/sqlglot)，执行时百分号与流式结果设置参考 [SQLAlchemy 连接文档](https://docs.sqlalchemy.org/en/20/core/connections.html)。

Phase 1 之上的业务指标字典已在 Phase 2 完成；后续自然语言查询应复用已确认的定义与本查询层。

## 数据来源

用户从 [Kaggle Global Electronics Retailers](https://www.kaggle.com/datasets/bhavikjikadara/global-electronics-retailers/data) 下载的 `Data/archive.zip`。发布者为 Bhavik Jikadara，Kaggle 页面标注 CC BY 4.0。该数据描述虚构全球电子零售企业，用于本项目开发练习。保留原始 ZIP；导入脚本仅转换字段名、日期、金额表示和空值，不修改业务事实。

## 导入前检查

在 PowerShell 中执行以下命令。脚本仅使用 Python 标准库，不需要安装依赖。

```powershell
Set-Location 'D:\Codex\Trade Agent'
python scripts/prepare_data.py
python scripts/import_mysql.py --check-only
python scripts/test_import_tools.py
```

准备脚本核对重复主键、外键、日期、金额、字段长度和必填项，然后生成 `Data/prepared/import_global_electronics.sql` 与 `Data/prepared/data_profile.json`。

## 创建数据库并导入

在自己的交互式 PowerShell 终端执行：

```powershell
Set-Location 'D:\Codex\Trade Agent'
python scripts/import_mysql.py
```

在 MySQL 的密码提示中输入本机 root 密码。脚本不记录密码。它会连接本机 3306 端口，创建独立数据库 `trade_agent_demo`，按顺序导入五张业务表并显示行数核对结果。账号或端口不同可传入 `--user 用户名 --port 端口`。

脚本不包含 DROP、TRUNCATE 或覆盖已有数据库的操作。数据库已存在时，CREATE DATABASE 失败，批处理立即停止。不要使用 mysql --force 执行导入文件。若发生错误，先保留错误信息供诊断；MySQL 建库建表不会随数据事务回滚，失败可能留下空表，不能把再次运行视为自动恢复。

验收期望：customers 15266 行、products 2517 行、stores 67 行、exchange_rates 11215 行、sales 62884 行，五行 passed 均为 1。订单去重后为 26326 个，订单日期覆盖 2016-01-01 至 2021-02-20。

## 已知数据口径

- sales 每行是一条订单明细；订单数必须按 order_number 去重。
- customers 原文件采用 Windows-1252 编码；导入转换为 utf8mb4。邮编按文本保留，避免丢失前导零或字母。
- 产品表提供美元标价与单位生产成本，销售明细没有实际成交单价、折扣、税费、运费或历史成本。因此数量乘标价只能作为教学销售额估算，不能声称是真实确认收入；成本变化的原因分析缺少历史证据。
- 订单日期是历史日期，后续问题使用真实覆盖区间，不将其改成 2026 年。
- delivery_date 的 49719 个空值和 square_meters 的 1 个空值保留为 NULL；不假定交付日期缺失就是尚未送达。
- 客户国家与门店国家是不同维度。汇率字典只描述相对 USD 的汇率，换算方向须在业务口径阶段进一步确认。
- 姓名、生日和地址字段供本地样例保留；后续提供给模型的 Schema 和结果需按用途筛选。
- Data_Dictionary.csv 是原始字段说明，保留在 ZIP 中，不作为第六张业务表导入。

## 当前连接与复验

已创建 `trade_agent_readonly@127.0.0.1`，仅对五张教学表授予 SELECT。随机密码保存在根目录 `.env`，已由 `.gitignore` 排除；不要将 `.env` 内容发送到聊天或提交版本库。

当前使用已有 Python 3.12 环境，所需库已安装，验证过的依赖版本记录在 `requirements.txt`。其他机器可在项目虚拟环境中执行 `python -m pip install -r requirements.txt` 安装。

```powershell
Set-Location 'D:\Codex\Trade Agent'
python scripts/check_connection.py
python scripts/inspect_database.py
```

第一条命令验证专用账号身份、精确的表级 SELECT 权限、五张表行数，以及 26326 个订单、2016-01-01 至 2021-02-20 的日期范围和 197757 件商品。随后发送恒假条件的 UPDATE/DELETE/INSERT，检查 MySQL 是否以 1142 拒绝；这些探针即便意外获得写权限也影响零行，不执行 DROP 或 ALTER。

第二条命令通过只读连接读取元数据及固定聚合统计，生成结构报告，不输出客户个人记录。两项真实数据库检查已于 2026-09-09 通过。导入前的四项离线测试也已通过。

连接配置使用 URL.create 处理密码字符，设置连接与读写超时，禁用 SQL 日志回显。Phase 0 的固定语句侦察脚本通过 scripts/db_config.py 复用新的连接层；后续用户或模型 SQL 应统一调用本阶段的 tools.sql_tool.run_sql。

`scripts/setup_readonly.py` 是一次性的管理员配置工具；它不会覆盖已有 `.env` 或修改同名账号。当前已完成配置，无需再次执行。
