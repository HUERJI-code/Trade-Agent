"""离线验证会话继承、隔离、重启恢复和工具提议校验；不占用训练显存。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import pytest
from agent.conversation import complete_question
from agent.graph import ConversationAgent
from agent.planner import PlanError, PLAN_SCHEMA, QueryPlan, ground_plan
from agent.langchain_model import LangChainOllamaClient
from agent.local_model import ModelSettings

SCOPE = dict(action='query', metric='revenue', start='2019-01-01', end='2020-01-01', country='Germany', dimension=None, reason=None)


def test_native_tool_prompt_has_no_conflicting_json_output_contract():
    from agent.planner import build_prompt
    context = {'metrics': {'revenue': {'label': '估算销售额', 'description': '按标价估算'}}}
    prompt = build_prompt(context, tool_call=True)
    assert '只输出指定 JSON' not in prompt and '输出 schema' not in prompt
    assert '指标参数叫 name' in prompt and '2019年表示 start=2019-01-01' in prompt
    assert '只输出指定 JSON' in build_prompt(context)  # CLI 的原协议继续有效。


def test_followup_inherits_only_missing_conditions():
    effective, inherited = complete_question('那法国呢？', SCOPE)
    assert inherited == ['time', 'metric'] and 'Germany' not in effective
    plan = QueryPlan(**{**SCOPE, 'country': 'France'})
    assert ground_plan(plan, effective) == plan
    effective, inherited = complete_question('订单数呢？', SCOPE)
    assert inherited == ['time', 'country']
    assert ground_plan(QueryPlan(**{**SCOPE, 'metric': 'order_count'}), effective).metric == 'order_count'


def test_full_question_resets_scope_and_ambiguous_dates_stay_ambiguous():
    question = '2020年全部国家销售额是多少？'
    assert complete_question(question, SCOPE) == (question, [])
    effective, _ = complete_question('2018年比2019年德国销售额增加多少？', SCOPE)
    assert ground_plan(QueryPlan(**SCOPE), effective).reason == 'ambiguous_scope'
    assert complete_question('你好', SCOPE) == ('你好', [])
    assert complete_question('那法国呢？', None) == ('那法国呢？', [])


def test_persisted_sessions_are_isolated_and_failed_turn_preserves_scope(tmp_path):
    analysis = Mock()
    analysis.ask.side_effect = [dict(status='ok', plan=SCOPE, answer='结果'), dict(status='unsupported', answer='不支持')]
    path = tmp_path / 'sessions.sqlite3'
    first = ConversationAgent(analysis=analysis, path=path)
    a, b = first.create_conversation()['id'], first.create_conversation()['id']
    first.ask('2019年德国销售额', a)
    first.ask('预测明年', a)
    assert len(first.get_conversation(a)['history']) == 2
    assert first.get_conversation(b)['history'] == []
    first.close()
    resumed_analysis = Mock(ask=Mock(return_value=dict(status='clarify', answer='补充条件')))
    resumed = ConversationAgent(analysis=resumed_analysis, path=path)
    resumed.ask('那法国呢？', a)
    assert '2019-01-01' in resumed_analysis.ask.call_args.args[0]
    resumed.ask('那法国呢？', b)
    assert resumed_analysis.ask.call_args.args[0] == '那法国呢？'
    with pytest.raises(ValueError):
        resumed.get_conversation('00000000-0000-0000-0000-000000000000')
    resumed.close()


def test_langchain_sends_tool_directory_but_does_not_execute_model_proposal(monkeypatch):
    import agent.langchain_model as module
    llm = Mock()
    llm.bind_tools.return_value.invoke.return_value = SimpleNamespace(
        tool_calls=[{'name': 'run_metric', 'args': dict(name='revenue', start='2019-01-01', end='2020-01-01', country='Germany')}],
        invalid_tool_calls=[], response_metadata={})
    monkeypatch.setattr(module, 'ChatOllama', Mock(return_value=llm))
    client = LangChainOllamaClient()
    reply = client.generate('规则', '2019年德国销售额', PLAN_SCHEMA)
    assert json.loads(reply.content) == SCOPE
    assert {item.name for item in llm.bind_tools.call_args.args[0]} == {'run_metric', 'clarify_query', 'unsupported_query'}
    llm.bind_tools.return_value.invoke.return_value.tool_calls[0]['name'] = 'execute_sql'
    with pytest.raises(PlanError):
        client.generate('规则', '删除数据库', PLAN_SCHEMA)


def test_literal_null_normalization_does_not_remove_explicit_country(monkeypatch):
    import agent.langchain_model as module
    llm = Mock()
    llm.bind_tools.return_value.invoke.return_value = SimpleNamespace(
        tool_calls=[{'name': 'run_metric', 'args': dict(name='revenue', start='2019-01-01', end='2020-01-01', country='null', dimension='null')}],
        invalid_tool_calls=[], response_metadata={})
    monkeypatch.setattr(module, 'ChatOllama', Mock(return_value=llm))
    reply = LangChainOllamaClient().generate('规则', '2019年销售额', PLAN_SCHEMA)
    plan = QueryPlan.parse(reply.content)
    assert plan.country is None and plan.dimension is None
    assert ground_plan(plan, '2019年销售额') == plan
    with pytest.raises(PlanError):
        ground_plan(plan, '2019年德国销售额')  # 即使字符串 null 被纠正，明确国家筛选也不能丢掉。


def test_missing_date_can_be_supplied_on_next_turn(tmp_path):
    analysis = Mock()
    analysis.ask.side_effect = [dict(status='clarify', plan={'reason': 'missing_time'}, answer='请补充时间'),
                                dict(status='ok', plan=SCOPE, answer='结果')]
    agent = ConversationAgent(analysis=analysis, path=tmp_path / 'pending.sqlite3')
    identifier = agent.create_conversation()['id']
    agent.ask('德国销售额是多少？', identifier)
    agent.ask('2019年', identifier)
    assert analysis.ask.call_args.args[0] == '德国销售额是多少？；2019年'
    agent.close()


def test_real_langchain_http_transport_includes_tools_and_parses_ollama_call():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(payload)
            reply = {'model': 'qwen3:8b', 'created_at': '2026-09-10T00:00:00Z', 'done': True, 'done_reason': 'stop',
                'message': {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'run_metric',
                    'arguments': dict(name='revenue', start='2019-01-01', end='2020-01-01', country='Germany', dimension=None)}}]},
                'prompt_eval_count': 100, 'eval_count': 30, 'total_duration': 1000000}
            body = (json.dumps(reply) + '\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = LangChainOllamaClient(ModelSettings(base_url=f'http://127.0.0.1:{server.server_port}'))
        reply = client.generate('只读业务参数规则', '2019年德国销售额是多少？', PLAN_SCHEMA)
        assert json.loads(reply.content) == SCOPE
        assert {item['function']['name'] for item in requests[0]['tools']} == {'run_metric', 'clarify_query', 'unsupported_query'}
        assert requests[0]['think'] is False
        assert reply.prompt_tokens == 100 and reply.output_tokens == 30
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
