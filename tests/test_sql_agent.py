"""验证自由 SQL 的执行边界、跨期查询、修正、会话和回答事实约束。"""
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from agent.sql_agent import SQLAnalysisAgent, SQLModelClient, SQLArguments, query_database
from agent.graph import ConversationAgent
from agent.answer_style import check_sql_answer, AnswerWriter
from security.sql_validator import validate_sql, SQLValidationError
from tools.sql_tool import QueryError

COMPARE = """SELECT ROUND(SUM(CASE WHEN YEAR(s.order_date)=2018 THEN s.quantity*p.unit_price_usd END),2) AS old_amount,
ROUND(SUM(CASE WHEN YEAR(s.order_date)=2019 THEN s.quantity*p.unit_price_usd END),2) AS new_amount,
ROUND(SUM(CASE WHEN YEAR(s.order_date)=2019 THEN s.quantity*p.unit_price_usd ELSE -s.quantity*p.unit_price_usd END),2) AS difference
FROM sales s JOIN products p ON s.product_key=p.product_key JOIN customers c ON s.customer_key=c.customer_key
WHERE c.country='Germany' AND s.order_date >= '2018-01-01' AND s.order_date < '2020-01-01'"""


def reply(sql):
    return ({'name': 'query_database', 'args': {'sql': sql}}, {})


def raw_result(sql, columns=None, rows=None):
    return dict(trace_id='test-sql', sql=sql, columns=columns or ['数量'], rows=rows if rows is not None else [[12]],
                row_count=1, row_limit=2000, limit_reached=False, limit_applied=True, elapsed_ms=1)


def make_agent(tmp_path, replies, runner):
    client = SimpleNamespace(settings=SimpleNamespace(model='test-model'), propose=Mock(side_effect=replies))
    agent = SQLAnalysisAgent(client=client, runner=runner, context_loader=lambda: {}, audit_path=tmp_path / 'audit.jsonl')
    return agent, client


def test_compare_reaches_executor_without_single_period_rules(tmp_path, schema):
    executed = []
    def runner(args):
        checked = validate_sql(args['sql'], schema)
        executed.append(checked.sql)
        return raw_result(checked.sql, ['2018年估算销售额', '2019年估算销售额', '差额'], [['100.00', '130.00', '30.00']])
    agent, client = make_agent(tmp_path, [reply(COMPARE)], runner)
    result = agent.ask('2019年德国销售额比2018年增加多少？')
    assert result['status'] == 'ok' and len(executed) == 1
    assert result['plan']['mode'] == 'sql' and '30.00' in result['answer']
    assert client.propose.call_count == 1


@pytest.mark.parametrize('sql', [
    "SELECT c.country, p.brand, SUM(s.quantity) AS qty FROM sales s JOIN customers c ON s.customer_key=c.customer_key JOIN products p ON s.product_key=p.product_key WHERE c.country IN ('Germany','France') AND p.brand <> 'Contoso' GROUP BY c.country,p.brand HAVING SUM(s.quantity)>10 ORDER BY qty DESC LIMIT 5",
    "WITH totals AS (SELECT YEAR(order_date) AS y,SUM(quantity) AS qty FROM sales GROUP BY YEAR(order_date)) SELECT y,qty,LAG(qty) OVER (ORDER BY y) AS previous_qty FROM totals",
    "SELECT order_number, ROW_NUMBER() OVER (PARTITION BY customer_key ORDER BY order_date) AS n FROM sales LIMIT 5",
])
def test_select_filters_comparison_and_windows_allowed(schema, sql):
    assert validate_sql(sql, schema).sql


@pytest.mark.parametrize('sql', [
    'DELETE FROM sales', 'UPDATE sales SET quantity=0', 'DROP TABLE sales',
    'SELECT quantity FROM sales; DELETE FROM sales',
    "SELECT quantity INTO OUTFILE 'D:/leak.txt' FROM sales",
    'SELECT quantity FROM sales FOR UPDATE', 'SELECT SLEEP(5) FROM sales',
    'SELECT LOAD_FILE(\'D:/secret\') FROM sales', 'SELECT name FROM customers',
    'SELECT password FROM mysql.user', 'SELECT @x := quantity FROM sales',
    'WITH x AS (SELECT quantity FROM sales) DELETE FROM sales',
])
def test_non_select_and_sensitive_access_never_reaches_database(monkeypatch, schema, sql):
    import agent.sql_agent as module
    executed = []
    def guarded_run(text):
        checked = validate_sql(text, schema)
        executed.append(checked.sql)
    monkeypatch.setattr(module, 'run_sql', guarded_run)
    with pytest.raises(SQLValidationError):
        query_database.invoke({'sql': sql})
    assert not executed


def test_validation_error_repaired_once_and_failure_is_not_success(tmp_path):
    runner = Mock(side_effect=[QueryError('invalid_sql_or_column', 'Unknown column', '1'), raw_result('SELECT quantity FROM sales')])
    agent, client = make_agent(tmp_path, [reply('SELECT bad FROM sales'), reply('SELECT quantity FROM sales')], runner)
    assert agent.ask('销量')['status'] == 'ok'
    feedback = client.propose.call_args.args[-1]
    assert feedback['error'] == 'invalid_sql_or_column' and feedback['previous_sql'] == 'SELECT bad FROM sales'
    runner = Mock(side_effect=QueryError('readonly_only', 'Only SELECT', '2'))
    agent, client = make_agent(tmp_path, [reply('DELETE FROM sales')] * 2, runner)
    result = agent.ask('删除销售记录')
    assert result['status'] == 'error' and result['result'] is None and runner.call_count == 2


def test_clarification_and_unknown_tool_do_not_execute(tmp_path):
    runner = Mock()
    agent, _ = make_agent(tmp_path, [({'name': 'ask_for_details', 'args': {'message': '请选择期间'}}, {})], runner)
    assert agent.ask('那个时期呢')['status'] == 'clarify'
    runner.assert_not_called()
    agent, _ = make_agent(tmp_path, [({'name': 'shell', 'args': {}}, {})] * 2, runner)
    assert agent.ask('执行命令')['status'] == 'error'
    runner.assert_not_called()


def test_tool_schema_and_proposal_do_not_execute(monkeypatch):
    import agent.sql_agent as module
    response = SimpleNamespace(tool_calls=[{'name': 'query_database', 'args': {'sql': COMPARE}}], invalid_tool_calls=[], response_metadata={})
    llm = Mock()
    llm.bind_tools.return_value.invoke.return_value = response
    monkeypatch.setattr(module, 'ChatOllama', Mock(return_value=llm))
    execution = Mock()
    monkeypatch.setattr(module, 'run_sql', execution)
    call, _ = SQLModelClient().propose('两年比较', {}, [])
    assert call['args']['sql'] == COMPARE
    assert {item.name for item in llm.bind_tools.call_args.args[0]} == {'query_database', 'ask_for_details'}
    execution.assert_not_called()
    response.tool_calls[0]['args']['extra'] = 'bad'
    with pytest.raises(ValueError):
        SQLModelClient().propose('两年比较', {}, [])


def test_sql_conversation_preserves_raw_question_and_isolates_history(tmp_path):
    analysis = SimpleNamespace(query_mode='sql', ask=Mock(return_value={'status': 'ok', 'answer': '结果', 'plan': {'action': 'query', 'mode': 'sql', 'sql': COMPARE}}))
    graph = ConversationAgent(analysis=analysis, path=tmp_path / 'sessions.sqlite3')
    a, b = graph.create_conversation()['id'], graph.create_conversation()['id']
    graph.ask('2019年德国销售额比2018年增加多少', a)
    graph.ask('那法国呢？', a)
    assert analysis.ask.call_args.args == ('那法国呢？',)
    assert analysis.ask.call_args.kwargs['history'][0]['question'].startswith('2019年德国')
    graph.ask('订单数呢？', b)
    assert analysis.ask.call_args.kwargs['history'] == []
    graph.close()


def test_sql_answer_checks_changed_sign_and_invented_comparison():
    context = {'columns': ['估算差额_美元'], 'rows': [['-100.00']]}
    assert check_sql_answer('估算差额为 **-100.00 美元**。', context, '') == []
    assert 'unexpected_number' in check_sql_answer('估算差额为100美元。', context, '')
    assert 'unexpected_number' in check_sql_answer('估算差额为-100美元，增长率为10%。', context, '')
    assert 'missing_estimate' in check_sql_answer('销售额为100美元。',
        {'columns': ['revenue_usd'], 'rows': [['100']], 'requires_estimate': True}, '')


def test_unknown_alias_feedback_and_valid_correlated_select(schema):
    with pytest.raises(SQLValidationError, match='alias c is not declared'):
        validate_sql("SELECT SUM(s.quantity) FROM sales s WHERE c.country='Germany'", schema)
    assert validate_sql('SELECT c.country FROM customers c WHERE EXISTS (SELECT s.order_number FROM sales s WHERE s.customer_key=c.customer_key)', schema).sql


def test_generic_answer_falls_back_on_missing_values_and_empty_result():
    client = Mock()
    client.settings = SimpleNamespace(model='test', seed=42)
    client._request.return_value = {'message': {'content': '估算销售额为100美元。'}, 'done': True}
    writer = AnswerWriter(client)
    result = {**raw_result('SELECT ...', ['估算销售额_美元', '差额_美元'], [['100', '20']]), 'kind': 'sql', 'limitations': []}
    response = {'status': 'ok', 'result': result, 'answer': '原始可核对文案'}
    assert writer.rewrite('比较', response)['answer'] == '原始可核对文案'
    client._request.reset_mock()
    result['rows'] = []
    writer.rewrite('无匹配', response)
    client._request.assert_not_called()
