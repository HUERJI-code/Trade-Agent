"""使用 LangChain ChatOllama 提出工具调用，再交给既有计划校验器。"""
import json  # 将工具参数转换为既有 QueryPlan 的 JSON 格式。
import os  # 关闭云端追踪，问答和数据保留在本机。
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
from langchain_ollama import ChatOllama  # Ollama 的标准 LangChain 模型接口。
from agent.local_model import OllamaClient, ModelError, ModelReply  # 复用本机地址与配置校验。
from agent.langchain_tools import QUERY_TOOLS  # 工具定义自动转换为名称、描述与参数 schema。


class LangChainOllamaClient(OllamaClient):
    planner_source = 'langchain_tool_call'  # 审计中区分原来的 JSON 参数提取与工具调用。
    def generate(self, system, question, schema, repair=False):
        settings = self.settings  # ModelSettings 已检查目标是本机回环地址。
        model = ChatOllama(model=settings.model, base_url=settings.base_url, temperature=settings.temperature,
            num_ctx=settings.context_tokens, num_predict=settings.output_tokens, seed=settings.seed,
            reasoning=False, keep_alive='5m',
            client_kwargs={'timeout': settings.timeout_seconds, 'trust_env': False, 'follow_redirects': False})
        bound = model.bind_tools(QUERY_TOOLS)  # 每次请求都把可用工具目录发给模型，不扫描 Python 文件。
        instruction = system + '\n本轮输出方式：调用且仅调用一个工具，严格使用该工具定义中的参数名。'
        if repair:
            instruction += '\n上次工具调用无效；核对日期、国家和工具参数，重新提交一次。'
        try:
            reply = bound.invoke([('system', instruction), ('human', question)])  # 模型只提出调用，还没有执行工具。
            calls = reply.tool_calls
            if len(calls) != 1 or reply.invalid_tool_calls or reply.response_metadata.get('done_reason') == 'length':
                raise ValueError('Expected one complete tool call')
            call = calls[0]
            registered = next((item for item in QUERY_TOOLS if item.name == call['name']), None)
            if registered is None:
                raise ValueError('Unknown tool')
            arguments = {key: (None if key in ('country', 'dimension') and value == 'null' else value)
                         for key, value in call['args'].items()}  # 只纠正可空字段的字符串 null；日期、国家等仍交给原校验。
            parameters = registered.args_schema.model_validate(arguments).model_dump()
            if set(call['args']) - set(registered.args):
                raise ValueError('Unexpected tool argument')
            plan = dict(action='query', metric=None, start=None, end=None, country=None, dimension=None, reason=None)
            if call['name'] == 'run_metric':
                plan.update(metric=parameters.pop('name'), **parameters)
            else:
                plan.update(action='clarify' if call['name'] == 'clarify_query' else 'unsupported', reason=parameters['reason'])
            meta = reply.response_metadata
            return ModelReply(json.dumps(plan, ensure_ascii=False), int(meta.get('prompt_eval_count', 0)),
                              int(meta.get('eval_count', 0)), round(meta.get('total_duration', 0) / 1_000_000, 2))
        except ModelError:
            raise
        except (ValueError, TypeError, KeyError):
            from agent.planner import PlanError  # 让原服务按约定只重试一次格式错误。
            raise PlanError('模型没有提交有效的工具调用。') from None
        except Exception:
            raise ModelError('model_unavailable', '本机模型工具调用失败，请检查 Ollama 状态。') from None
