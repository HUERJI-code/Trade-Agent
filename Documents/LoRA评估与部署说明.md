# 回答风格 LoRA 的评估与部署

2026-09-10 清理说明：完整基座、合并权重、F16 和 Q4_K_M GGUF 中间文件及旧 Ollama 基线已删除；已导入的微调模型可以直接启动。下文合并、转换、重新导入及基线评估命令需要先恢复各自输入和环境，不能原样立即重跑。小体积权重来源、许可与合并核验报告已归档至 `outputs/model_provenance/`；导入脚本支持读取归档模板，不再要求旧基线常驻。见 [清理记录](项目空间清理与范围局限.md)。

本轮已完成训练、合并、GGUF部署、两组206条评估与网页切换，结果见 [最终运行报告](LoRA训练评估与部署结果.md)。

本轮目标是让模型依据 SQL 返回的可信汇总事实生成自然中文。模型不应记住固定销售额，也不负责计算金额或绕过查询工具。

## 先看训练是否正常

训练日志的 loss 是目标回答 token 的预测损失；grad_norm 是梯度范数；验证集 eval_loss 用于观察未参与更新的数据上表现。loss 降低和 token accuracy 提高，不等于业务问答准确率。

数据集按年度等条件划分，训练 698 条、验证 206 条、测试 206 条。测试记录中同一个事实有两种问法，因此 206 条不是 206 个完全独立事实。部分样本为明确标记的模拟边界或反事实情境，不能当成真实企业数据。

首轮参数固定为 2 epochs、学习率 1e-4、LoRA rank 8、alpha 16、micro batch 1、累积 16。没有进行参数搜索，也不能声称得到最优参数。训练因用户暂停从检查点恢复过；Windows 状态文件访问冲突也记录在运行目录，未保存的步骤会重算。

## 比较原模型和微调模型

`scripts/evaluate_answer_model.py` 对固定测试 JSON 调用本机 Ollama。原模型与微调模型使用相同 system/user 消息、temperature=0、seed=42、上下文 4096、生成上限 384 token。每条原始回答写入 generations.jsonl，失败不会由模板替换后再评分。

| 检查 | 含义与局限 |
|---|---|
| automatic_fact_checks_pass | 数值 display、估算标记、国家和部分边界等自动规则通过；不代表完整语义已被证明 |
| reference_numbers_match | 输出中的数字集合与参考回答一致；合法的不同表达也可能被判不一致 |
| exact_reference_match | 去除空白后与参考答案完全相同；自然语言可以有其他正确表达 |
| concise | 字数在 1～220 之间；简短不保证质量 |
| bold_main_value | 主指标按确认风格用 ** 加粗；无主数值的样本视为不适用并记通过 |
| margin_basis_before_value | 普通毛利率汇总回答先说明汇总毛利/汇总销售额，再给结果；排名场景不适用 |

还应人工阅读：所有失败样本、六种指标的代表样本、无数据/空值/部分覆盖/并列/截断，以及同一问题更换可信数值后的反事实样本。重点检查模型是否复制当前 context 的数值，而非背训练集的德国销售额。

部署比较的基线是项目现有 `qwen3:8b`，候选是本项目原始 HF 基座加 LoRA 后重新量化的模型。报告记录二者模型 digest。这是实际部署效果对比；基线权重来源版本及量化流程可能不同，不能将所有差异严格归因于 LoRA。若要隔离 LoRA 因果效果，应在同一精度、同一基座上比较启用与禁用 adapter。

## 合并并导入 Ollama

1. `scripts/merge_lora_model.py` 确认训练 completed，按 Safetensors 索引逐层读取同一 revision 的原始 BF16 权重。
2. 每个目标线性层加载对应的已训练 A/B 矩阵，交给标准 PEFT `merge_and_unload(safe_merge=True)`。无 LoRA 的层保持原值；输出时转换为 FP16。该策略避免同时加载全部 8B 权重及其合并副本。
3. 完整权重另存为 `models/huggingface/Qwen3-8B-answer-merged-v1/`，共 399 个张量、18 个分片，252 个 LoRA 层全部合并。原始基座和适配器保留；tokenizer 使用原始推理/工具模板。`scripts/verify_merged_model.py` 独立核对七种目标层与两个未训练层，九项逐元素检查通过。
4. `scripts/convert_merged_gguf.py` 调用 llama.cpp b6500 官方转换脚本，生成 F16 GGUF；`scripts/quantize_merged_model.ps1` 再量化为 Q4_K_M。
5. `scripts/import_merged_model.ps1` 通过 GGUF 导入项目 Ollama，标签为 `trade-agent-qwen3:8b-lora-v1`，聊天模板沿用已有 qwen3:8b 的工具调用模板。
6. 比较测试回答，并验证候选仍能提出正确工具调用，随后切换 `config/local_model.yaml`，启用 `config/answer_model.yaml` 并重启网页服务。

实际运行发现，本机 Ollama 0.33.3 直接导入该 Safetensors 返回 unsupported architecture Qwen3ForCausalLM。虽然源码有相关模型代码，不能据此认定当前构建入口支持直接转换。因此最终使用 llama.cpp b6500，固定 commit a7a98e0fffed794396b3fbad4dcdbbc184963645；Windows CPU 工具 ZIP 已核对官方 SHA-256。转换源码与程序均在项目 .runtime 中。

整模型合并曾遇到自动设备分配产生 meta 张量，以及进程在高内存阶段退出的问题。最终采用上面的逐层方式，仍使用 PEFT 合并而非自定义训练或合并算法。核对计算遵循本机 PEFT 0.18.1 的实际行为：先把 LoRA 增量转为 BF16，再与 BF16 基座相加，最后导出 FP16；不能把“先用 FP32 相加再一次舍入”误当作完全相同的数值过程。

## 网页实际如何使用微调模型

图中的 query 节点执行工具，write_answer 节点把可信汇总结果整理成与训练相同的 question/context 输入，再调用模型生成文字。

`agent/answer_style.py` 独立检查关键数值、估算标记、国家和部分边界。发现不一致或生成失败时保留 SQL 返回结果，并使用原有可核对模板；response 中的 answer_source 和 answer_checks 会记录回退原因。评估脚本检验的是回退之前的原始回答，不能用回退后的正确率掩盖模型错误。

会话和工具回归也必须检查：德国→法国→订单数；新会话不继承旧条件；缺时间后补“2019年”；非法请求不执行 SQL；跨两年比较仍按当前能力说明不支持或澄清。LoRA 训练回答风格不会自动扩展查询工具。

## 运行方式

```powershell
# 训练完成后合并完整模型。
.\.venv-training\Scripts\python.exe scripts/merge_lora_model.py
# 核对合并，转换 GGUF，再量化。
.\.venv-training\Scripts\python.exe scripts/verify_merged_model.py
.\.venv-training\Scripts\python.exe scripts/convert_merged_gguf.py
pwsh -NoProfile -File scripts/quantize_merged_model.ps1
# 导入候选；暂不切换当前配置。
pwsh -NoProfile -File scripts/import_merged_model.ps1
# 相同测试集比较当前模型与候选，默认各 206 条。
.\.venv\Scripts\python.exe scripts/evaluate_answer_model.py --model qwen3:8b --output outputs/evaluation/base_v1
.\.venv\Scripts\python.exe scripts/evaluate_answer_model.py --model trade-agent-qwen3:8b-lora-v1 --output outputs/evaluation/lora_v1
```

所有模型、日志、检查点和结果都保存在 D 盘项目内。网页使用 `.venv`，训练合并使用 `.venv-training`。评估与推理需要 GPU，安排在训练结束后进行。

转换环境在训练环境上添加官方转换脚本所需的 mistral-common、sentencepiece、protobuf、tiktoken，并约束已有训练依赖不变。完整转换依赖锁定记录在 `requirements-conversion-lock.txt`，pip check 通过。
