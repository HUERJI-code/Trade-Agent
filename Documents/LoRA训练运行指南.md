# 用标准 SFTTrainer 训练回答风格

清理后的当前状态（2026-09-10）：基座完整权重和中间检查点已删除，最终适配器、数据、训练源码和结果保留；`.venv-training` 清理中断，不能当作完整可用环境。下文命令是训练流程记录，重新训练前需重建独立环境并按固定版本下载基座；网页启动无需这些训练文件。见 [空间清理与范围局限](项目空间清理与范围局限.md)。

当前流程按用户提供的 Python 示例整理为 `datasets + Transformers + PEFT + TRL SFTTrainer`。主要训练逻辑在 `scripts/train_answer_style.py`，按步骤编号并附中文注释。模型、数据、缓存和训练输出均使用 D 盘项目内路径。

最新训练结果：已完成 88 个优化器步骤、2 个 epoch。第 1 轮验证损失为 0.111015，第 2 轮为 0.013877；最终适配器为 `outputs/training/answer_style_qwen3_8b_sft_v1/adapter/adapter_model.safetensors`（87,360,584 字节）。训练峰值已分配显存约 7.082 GiB。下面的暂停与恢复段落是过程记录，不代表当前仍暂停。

2026-09-10：用户要求暂停训练，训练进程已停止并释放 GPU。停止前最新日志为第 11/88 步，完整保存的检查点为 `checkpoint-10`，恢复将从第 10 步继续，未保存的进度需要重算。状态文件记为 `paused_by_user`；这表示进程已停止、保留检查点，不是在内存中挂起进程。用户要求恢复前不自动重启。

后续同日：用户已明确要求继续训练、完成后合并 LoRA 并用 Ollama 部署。训练已从 checkpoint-10 恢复。本轮新增 `scripts/merge_lora_model.py`、`scripts/import_merged_model.ps1`、`scripts/evaluate_answer_model.py`；实际完成情况以运行报告为准，不能将脚本存在当作已训练或已部署。

已完成合并、GGUF转换、Q4_K_M量化、Ollama导入、评估及网页切换。当前模型 `trade-agent-qwen3:8b-lora-v1`；结果和失败样例见 [最终运行报告](LoRA训练评估与部署结果.md)。无需重复启动训练。

## 先理解这一条流程

读取配置 → 加载 tokenizer → 读取 messages 数据 → NF4 加载 Qwen3-8B → 定义 LoRA → 定义 SFTConfig → 创建 SFTTrainer → train() → 保存适配器。

| 主脚本步骤 | 对应代码 | 做什么 |
|---|---|---|
| 1 | `yaml.safe_load` | 从配置读取模型路径、学习率、轮数等 |
| 2 | `AutoTokenizer.from_pretrained` | 把消息转换成模型认识的 token |
| 3 | `load_dataset('json', ...)` | 读取已确认的训练、验证和测试数据 |
| 4 | `BitsAndBytesConfig`、`AutoModelForCausalLM` | 将本地基座以 4 bit NF4 加载到 GPU |
| 5 | `LoraConfig` | 定义需要学习的低秩矩阵 |
| 6 | `SFTConfig` | 指定训练轮数、批量、学习率、监督范围等 |
| 7 | `SFTTrainer(..., peft_config=...)` | 框架挂载 LoRA、处理模板与标签 |
| 8 | `trainer.train()` | 自动执行前向、损失计算、反向传播、参数更新 |
| 9 | `save_pretrained` | 保存适配器和 tokenizer |

PowerShell 只是可选的后台启动入口。真正训练的是 Python 文件里的 `SFTTrainer`，可以直接在终端或 IDE 中运行该文件。

## 与你提供的示例有什么不同

- 模型使用项目已下载的官方 `Qwen/Qwen3-8B`，不是示例中的 Qwen2.5-7B；固定 revision 为 `b968826d9c46dd6066d109eabc6255188de91218`。下载文件已经过指纹校验。
- 数据已经是 `messages` 格式，无需再从 question/answer 拼接 text。user 消息包含问题和可信查询事实，模型学习根据事实组织语言。
- `assistant_only_loss=True`：输入条件不作为监督目标。TRL 自动使用带 generation 标记的 Qwen3 训练模板。标签包含 assistant 的空思考标记、回答和结束部分；没有训练实质性的思考内容。
- `loss_type='chunked_nll'`：使用 TRL 自带的分块交叉熵，降低大词表输出的显存开销。项目没有自定义损失函数或 Trainer 子类。
- RTX 4060 Laptop 只有 8GB 显存，所以每次一条、累积 16 次、启用梯度检查点。正式运行前用最长训练样本试跑一次。

## 阅读顺序和文件职责

1. `scripts/train_answer_style.py`：先看完整训练流程，注释中的 1～9 对应上表。
2. `config/lora_training.yaml`：修改路径和训练参数，每个字段有说明。
3. `Data/answer_style/v1/train.json`：看一条 system/user/assistant 数据如何表达任务。
4. `scripts/training_support.py`：缓存位置、数据指纹、长度、回答标签核验与进度日志。它不接管训练循环。
5. `scripts/start_lora_training.ps1`：可选的隐藏后台启动、日志重定向与 PID 记录。

`requirements-training.txt` 固定主要依赖，`requirements-training-lock.txt` 记录实际安装版本。训练使用独立 `.venv-training`；网页仍使用 `.venv`。

旧自定义训练器已退出使用，历史代码保留在 `work/before_sft_trainer/` 供比较。旧目录 `outputs/training/answer_style_qwen3_8b_v1/` 只有旧流程报告；本轮全部输出在 `outputs/training/answer_style_qwen3_8b_sft_v1/`。

## 当前参数与数据

| 项目 | 配置 |
|---|---|
| 框架 | PyTorch 2.8.0+cu128、Transformers 4.57.6、PEFT 0.18.1、TRL 1.13.0、datasets 4.7.0 |
| 基座 | Qwen3-8B，NF4 + double quant，BF16 计算 |
| LoRA | rank 8、alpha 16、dropout 0.05 |
| 目标层 | q/k/v/o_proj、gate/up/down_proj；不训练 lm_head |
| 轮数 / 学习率 | 2 / 0.0001 |
| micro batch / 梯度累积 | 1 / 16 |
| 最大长度 | 2048，超长即报错，不静默截断 |
| 保存 / 验证 | 每 10 个优化器步骤保存；每轮计算验证损失 |

训练 698 条，验证 206 条，测试 206 条。全部 1,110 条检查长度和标签；测试集不参与梯度更新、验证损失或检查点选择。最长训练样本 1,145 token，验证和测试最长 1,128 token。

这些是首轮固定参数，不是参数搜索后得到的最优结果。训练目标是回答风格，loss 降低不能直接解释为问答准确率提高。

## 本机验证记录（2026-09-10）

- 独立训练环境通过 `pip check`。
- 全部 1,110 条数据通过指纹、长度和 assistant 标签检查，无截断。
- 标准 SFTTrainer 使用最长的 1,145 token 样本完成一次真实更新；验证了 LoRA 权重发生变化且均为有限数值。
- 试跑 loss 为 2.3776，PyTorch 峰值已分配显存约 6.798 GiB；该数值是单条试跑结果，不代表正式训练质量或整个运行的最大显存。
- 试跑报告见 `outputs/training/answer_style_qwen3_8b_sft_v1/smoke_report.json`，正式训练状态以同目录 `status.json` 为准。

## 直接使用 Python

在 `D:\Codex\Trade Agent` 的终端依次执行；如果已经有训练进程运行，不要重复启动：

```powershell
# 仅检查数据和标签，不加载 8B 模型。
.\.venv-training\Scripts\python.exe scripts/train_answer_style.py --mode prepare
# 最长样本一次真实 GPU 更新，结果写 smoke_report.json，不保存这次适配器。
.\.venv-training\Scripts\python.exe scripts/train_answer_style.py --mode smoke
# 重新加载基座并正式训练。也可以在 IDE 选中 .venv-training 后运行该文件。
.\.venv-training\Scripts\python.exe scripts/train_answer_style.py
```

需要后台运行时，最后一条可替换为 `pwsh -NoProfile -File scripts/start_lora_training.ps1`。它调用同一个 Python 文件，没有第二套训练逻辑。

## 查看进度与恢复

```powershell
Get-Content outputs/training/answer_style_qwen3_8b_sft_v1/status.json
Get-Content outputs/training/answer_style_qwen3_8b_sft_v1/train.stdout.log -Tail 10
```

`status.json` 的 starting 表示准备进入循环，training 表示已有训练日志，completed 表示流程完成，failed_or_interrupted 表示失败或中断。一步试跑的状态单独写在 `smoke/status.json`。`step` 是优化器更新次数，`epoch` 是训练轮次，`gpu_peak_gib` 是 PyTorch 记录的峰值已分配显存。

正式训练完成后，`adapter/adapter_model.safetensors` 保存 LoRA 权重，`adapter/adapter_config.json` 保存结构配置，`adapter/` 还包含 tokenizer。检查点另存于 `checkpoint-*`，用于恢复优化器等运行状态。

中断后确认原训练进程已退出且存在检查点，再执行：

```powershell
.\.venv-training\Scripts\python.exe scripts/train_answer_style.py --resume
```

## 接下来的边界

训练完成后还要评测生成文字的数值、单位、估算标记与回答风格，再考虑合并适配器、导出 GGUF、导入 Ollama 和接入回答阶段。当前网页仍使用原来的确定性回答模板。训练不会自动解除查询范围限制，也不会修改数据库权限。

官方参考：[TRL SFTTrainer](https://huggingface.co/docs/trl/sft_trainer)、[Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B)、[PEFT 量化训练](https://huggingface.co/docs/peft/developer_guides/quantization)。
