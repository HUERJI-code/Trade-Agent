"""用命令行提问或检查本地模型；当前每个问题独立处理。"""
import argparse # 提供 --question 和 --check-model 两种入口。
import json # 打印中文 JSON，方便后续 API 复用相同结果结构。
from agent.local_model import OllamaClient, ModelError # 本地模型健康检查。
from agent.sql_agent import SQLAnalysisAgent # 与网页一样使用完整 SELECT 生成和只读执行。

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True) # 两种模式只能选择一种。
    group.add_argument('--question',help='例如：2019 年德国的销售额是多少？')
    group.add_argument('--check-model',action='store_true',help='仅检查本机服务及模型，不执行推理')
    args = parser.parse_args()
    try:
        if args.check_model:
            result = OllamaClient().status()
            code = 0 if result['installed'] else 1
        else:
            result = SQLAnalysisAgent().ask(args.question)
            code = 1 if result['status'] == 'error' else 0 # 澄清是正常业务响应，不当作程序崩溃。
    except ModelError as error:
        result,code = {'status':'error','error':error.code,'answer':str(error)},1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return code

if __name__ == '__main__':
    raise SystemExit(main())
