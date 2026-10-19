"""按标准 SFTTrainer + PEFT + QLoRA 流程训练中文回答风格。"""

import argparse  # 区分数据检查、一步试跑和正式训练。
import json  # 读取已有校验报告。
from training_support import ROOT, write_json, check_dataset, check_trainer_labels, ProgressWriter  # 同时设置 D 盘缓存。
import torch  # GPU 计算与 BF16 精度。
import yaml  # 从配置文件读取训练参数。
from datasets import load_dataset  # 直接读取 messages 格式 JSON。
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig, set_seed  # 加载模型和分词器。
from transformers.trainer_utils import get_last_checkpoint  # 找到可恢复的检查点。
from peft import LoraConfig  # 定义仅需训练的低秩适配器。
from trl import SFTConfig, SFTTrainer  # 标准监督微调配置与训练器。


def main():
    # 1. 读取配置：不传参数时就是正式训练，也可单独检查数据或试跑。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('prepare', 'smoke', 'train'), default='train')
    parser.add_argument('--resume', action='store_true', help='从最近检查点恢复正式训练')
    args = parser.parse_args()
    config = yaml.safe_load((ROOT / 'config/lora_training.yaml').read_text(encoding='utf-8'))
    model_dir, data_dir, output = (ROOT / config[key] for key in ('model_directory', 'dataset_directory', 'output_directory'))
    output.mkdir(parents=True, exist_ok=True)
    set_seed(config['seed'])  # 固定适配器初始化与数据打乱的随机种子。

    # 2. 加载本地分词器：使用已经下载到 D 盘的官方 Qwen3-8B。
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
    tokenizer.pad_token = tokenizer.eos_token  # 填充位置会由训练器屏蔽，不参与损失。

    # 3. 加载数据：保留 system/user/assistant，user 中包含问题和可信查询事实。
    datasets = load_dataset('json', data_files={split: str(data_dir / f'{split}.json') for split in ('train', 'validation', 'test')},
                            cache_dir=str(ROOT / '.runtime/training-cache/datasets'))
    datasets = datasets.map(lambda example: {'chat_template_kwargs': {'enable_thinking': False}})  # 使用非思考回答格式。
    report, manifest = check_dataset(tokenizer, datasets, data_dir, config['max_length'])
    write_json(output / 'tokenization_report.json', report)  # 只检查测试集，不用它训练或选参数。
    print(json.dumps({'tokenization': report}, ensure_ascii=False), flush=True)
    if args.mode == 'prepare':
        return  # 数据检查不加载 8B 模型，也不占用训练显存。

    verified = json.loads((model_dir / 'download_verified.json').read_text(encoding='utf-8'))
    assert verified['revision'] == config['model_revision'], '训练基座版本必须与已校验版本一致。'
    smoke = args.mode == 'smoke'
    run_dir = output / 'smoke' if smoke else output  # 试跑与正式训练状态分开存放。
    run_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = get_last_checkpoint(str(output)) if args.resume else None
    if not smoke:
        assert json.loads((output / 'smoke_report.json').read_text(encoding='utf-8'))['status'] == 'passed', '先完成一步 GPU 试跑。'
        if args.resume and not checkpoint:
            raise RuntimeError('没有可恢复的检查点。')
        if not args.resume and ((output / 'status.json').exists() or get_last_checkpoint(str(output))):
            raise RuntimeError('已有正式训练记录，请核对后用 --resume 恢复，不能覆盖。')

    # 4. 以 QLoRA 的 NF4 方式加载基座：4 bit 保存线性层权重，BF16 参与计算。
    assert torch.cuda.is_available() and torch.cuda.is_bf16_supported(), '需要支持 BF16 的 NVIDIA GPU。'
    quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
                                     bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16)
    model = AutoModelForCausalLM.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False,
        quantization_config=quantization, dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
    model.config.use_cache = False  # 训练不用生成时的 KV 缓存，节省显存。

    # 5. 定义 LoRA：由下面的 SFTTrainer 自动挂载适配器并冻结原模型参数。
    lora_config = LoraConfig(r=config['lora_r'], lora_alpha=config['lora_alpha'], lora_dropout=config['lora_dropout'],
        target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj'],
        bias='none', task_type='CAUSAL_LM')  # 不训练输出头 lm_head。

    # 6. 配置 SFT：只监督 assistant，标准训练器完成模板处理、标签屏蔽和训练循环。
    training_args = SFTConfig(
        output_dir=str(run_dir), num_train_epochs=config['epochs'], max_steps=1 if smoke else -1,
        per_device_train_batch_size=1, per_device_eval_batch_size=1,  # 8GB 显卡一次处理一条。
        gradient_accumulation_steps=1 if smoke else config['gradient_accumulation_steps'],  # 正式累积 16 条再更新。
        learning_rate=config['learning_rate'], warmup_ratio=0.0 if smoke else config['warmup_ratio'], lr_scheduler_type='cosine',
        bf16=True, tf32=True, optim='adamw_torch', max_grad_norm=1.0,
        gradient_checkpointing=True, gradient_checkpointing_kwargs={'use_reentrant': False},  # 用重算换显存。
        assistant_only_loss=True, loss_type='chunked_nll',  # TRL 内置分块损失，降低大词表的显存开销。
        max_length=config['max_length'], packing=False, padding_free=False,
        eval_strategy='no' if smoke else 'epoch', prediction_loss_only=True,  # 每轮只评估验证集损失。
        save_strategy='no' if smoke else 'steps', save_steps=config['save_steps'], save_total_limit=2, save_safetensors=True,
        logging_steps=config['logging_steps'], logging_first_step=True, logging_nan_inf_filter=False,
        report_to=[], push_to_hub=False, dataloader_num_workers=0, dataloader_pin_memory=False,
        seed=config['seed'], data_seed=config['seed'], disable_tqdm=True)
    train_data = datasets['train'].select([report['train']['longest_index']]) if smoke else datasets['train']

    # 7. 创建标准训练器：peft_config 自动安装 LoRA，processing_class 负责分词。
    trainer = SFTTrainer(model=model, args=training_args, peft_config=lora_config,
        train_dataset=train_data, eval_dataset=None if smoke else datasets['validation'],
        processing_class=tokenizer, callbacks=[ProgressWriter(run_dir)])
    check_trainer_labels(trainer, train_data)  # 验证实际 labels 只监督回答与模板结束部分。
    trainable = [name for name, parameter in trainer.model.named_parameters() if parameter.requires_grad]
    assert trainable and all('lora_' in name for name in trainable), '只有 LoRA 参数可以更新。'
    trainer.model.print_trainable_parameters()
    write_json(run_dir / 'run_config.json', {'framework': 'TRL SFTTrainer', 'config': config,
        'device': torch.cuda.get_device_name(0), 'torch': torch.__version__, 'input_files_sha256': manifest['report']['files_sha256']})

    # 8. 开始训练：smoke 也使用同一个训练器，执行最长样本的一次更新。
    before = {name: parameter.detach().cpu().clone() for name, parameter in trainer.model.named_parameters()
              if smoke and parameter.requires_grad}  # 试跑前留副本，用于确认权重真的发生更新。
    write_json(run_dir / 'status.json', {'status': 'starting', 'step': 0, 'training_samples': len(train_data)})
    try:
        result = trainer.train(resume_from_checkpoint=checkpoint)
        if smoke:
            assert trainer.state.global_step == 1
            assert any(not torch.equal(before[name], parameter.detach().cpu()) for name, parameter in trainer.model.named_parameters()
                       if parameter.requires_grad), '试跑必须实际更新适配器权重。'
            assert all(torch.isfinite(parameter).all() for parameter in trainer.model.parameters() if parameter.requires_grad)
            smoke_report = {'status': 'passed', 'framework': 'TRL SFTTrainer', 'metrics': result.metrics,
                'peak_gpu_gib': torch.cuda.max_memory_allocated() / 1024 ** 3,
                'longest_train_tokens': report['train']['max_tokens'], 'adapter_update_verified': True, 'weights_saved': False}
            write_json(output / 'smoke_report.json', smoke_report)
            write_json(run_dir / 'status.json', {**smoke_report, 'status': 'smoke_completed'})
            return  # 不保存试跑权重；正式训练重新加载基座并初始化适配器。

        # 9. 保存适配器和 tokenizer：原始模型保留，后续评测通过后再考虑部署。
        adapter_dir = output / 'adapter'
        trainer.model.save_pretrained(adapter_dir, safe_serialization=True)
        tokenizer.save_pretrained(adapter_dir)
        trainer.save_state()  # 保存训练步数等运行状态。
        write_json(output / 'train_metrics.json', result.metrics)
        write_json(output / 'status.json', {'status': 'completed', 'step': trainer.state.global_step,
            'metrics': result.metrics, 'adapter_directory': str(adapter_dir), 'deployed_to_ollama': False,
            'test_generation_evaluated': False})
    except BaseException as error:
        write_json(run_dir / 'status.json', {'status': 'failed_or_interrupted', 'step': trainer.state.global_step,
            'error_type': type(error).__name__, 'message': str(error)[:1500]})
        raise


if __name__ == '__main__':  # 直接运行此 Python 文件时执行上述流程。
    main()
