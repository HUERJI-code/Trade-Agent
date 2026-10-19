"""绘制训练与验证损失；同一步恢复重算时采用最后一次记录。"""
import json  # 读取标准训练器日志。
import os  # 将绘图库缓存指向 D 盘。
from pathlib import Path  # 定位输出路径。
ROOT = Path(__file__).resolve().parents[1]
os.environ['MPLCONFIGDIR'] = str(ROOT / '.runtime/matplotlib')
import matplotlib  # 使用标准绘图工具生成可导出的图片。
matplotlib.use('Agg')  # 后台生成图片，不弹出额外窗口。
from matplotlib import pyplot as plt


def main():
    directory = ROOT / 'outputs/training/answer_style_qwen3_8b_sft_v1'
    training, validation = {}, {}
    for line in (directory / 'progress.jsonl').read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        if 'loss' in row['metrics']:
            training[row['step']] = row['metrics']['loss']
        if 'eval_loss' in row['metrics']:
            validation[row['step']] = row['metrics']['eval_loss']
    fig, axis = plt.subplots(figsize=(9, 4.8), constrained_layout=True)
    steps = sorted(training)
    axis.plot(steps, [training[step] for step in steps], color='#197c6f', linewidth=1.6, label='Training loss (per optimizer step)')
    if validation:
        val_steps = sorted(validation)
        axis.plot(val_steps, [validation[step] for step in val_steps], 'o--', color='#c06928', markersize=7, label='Validation loss (per epoch)')
    axis.set(title='Qwen3-8B answer-style QLoRA', xlabel='Optimizer step', ylabel='Supervised answer loss')
    axis.grid(alpha=0.18)
    axis.legend(frameon=False)
    fig.savefig(directory / 'loss_curve.png', dpi=170)
    plt.close(fig)
    print(directory / 'loss_curve.png')


if __name__ == '__main__':
    main()
