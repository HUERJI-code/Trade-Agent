# 回答风格 LoRA 数据 v1

本目录全部保存在 D 盘项目中，风格已由用户确认，尚未启动训练。

| 用途 | JSON 数组 | JSONL 等价文件 | 样例数 |
|---|---|---|---:|
| 训练 | train.json | train.jsonl | 698 |
| 验证 | validation.json | validation.jsonl | 206 |
| 测试 | test.json | test.jsonl | 206 |

总计 1,110 条，格式为 `messages`（system / user / assistant）。user 内容包含问题和可信的查询上下文，assistant 是目标回答。同一行的两种文件格式二选一，不要拼接重复训练。

862 条来自真实教学数据库汇总，170 条是模拟边界与反事实，78 条是手工流程情境。每个事实场景有两种问法，不能把这些改写算作独立数据事实。

`provenance.json` 记录来源和只读查询快照；`manifest.json` 记录划分方法、版本和文件摘要；`validation_report.json` 与 `audit_report.json` 保存检查结果。这些文件不作为训练输入。

从项目根目录阅读：

- `Documents/LoRA回答训练数据_v1说明.md`：格式、来源、划分与使用方法。
- `Documents/LoRA回答训练数据_v1精选.md`：代表性问题与目标回答。
- `scripts/build_answer_style_dataset.py`：带中文注释的数据生成脚本。
- `scripts/validate_answer_style_dataset.py`：独立的离线核验脚本。

当前模板在训练、验证和测试中复用，评测的是给定风格下对不同事实的表达能力；尚无模型训练或泛化评测结论。正式训练前需要按所选模型与框架配置聊天模板、监督目标和长度检查。
