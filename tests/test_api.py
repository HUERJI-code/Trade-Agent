"""验证网页接口边界；模拟分析器不代表真实模型效果。"""
from concurrent.futures import ThreadPoolExecutor # 验证并发请求不会同时进入模型。
from threading import Event # 精确控制测试中的慢查询，无需任意等待。
from types import SimpleNamespace # 构造只有 ask 方法的模拟分析器。
from unittest.mock import Mock # 检查被拒绝请求是否调用了分析器。
import pytest # 使用参数化验证多种无效输入。
from fastapi.testclient import TestClient # 在进程内发送 HTTP 请求。
from api.main import create_app # 创建独立应用，不使用已运行的实际服务。

def client_for(analysis, tmp_path):
    return TestClient(create_app(analysis=analysis, health_check=lambda: {'status': 'ready'}, static_dir=tmp_path), base_url='http://127.0.0.1:8000')

@pytest.mark.parametrize('payload', [{}, {'question': ''}, {'question': '  '}, {'question': 1}, {'question': '问' * 1001}, {'question': '销量', 'sql': 'DROP TABLE sales'}])
def test_invalid_body_never_calls_agent(tmp_path, payload):
    analysis = Mock()
    with client_for(analysis, tmp_path) as client:
        response = client.post('/api/ask', json=payload)
    assert response.status_code == 422
    analysis.ask.assert_not_called() # 无效输入不能触发模型或数据库。
    assert 'DROP TABLE' not in response.text

def test_size_type_and_json_boundaries(tmp_path):
    analysis = Mock()
    with client_for(analysis, tmp_path) as client:
        assert client.post('/api/ask', content='x' * 8193, headers={'Content-Type': 'application/json'}).status_code == 413
        assert client.post('/api/ask', content='broken-json', headers={'Content-Type': 'application/json'}).status_code == 422
        assert client.post('/api/ask', data={'question': '销量'}).status_code == 415
    analysis.ask.assert_not_called()

@pytest.mark.parametrize('headers,expected', [({'Origin': 'https://example.com'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403)])
def test_foreign_browser_requests_rejected(tmp_path, headers, expected):
    analysis = Mock()
    with client_for(analysis, tmp_path) as client:
        assert client.post('/api/ask', json={'question': '销量'}, headers=headers).status_code == expected
    analysis.ask.assert_not_called()


def test_custom_host_and_matching_origin_are_accepted(tmp_path):
    analysis = Mock(ask=Mock(return_value={'status': 'ok', 'answer': '完成'}))
    with client_for(analysis, tmp_path) as client:
        result = client.post('/api/ask', json={'question': '2019年订单数'},
                             headers={'Host': 'trade.example:8000', 'Origin': 'http://trade.example:8000'})
    assert result.status_code == 200
    analysis.ask.assert_called_once_with('2019年订单数')

def test_success_retains_decimal_text_and_trims_question(tmp_path):
    data = {'status': 'ok', 'answer': '估算销售额', 'result': {'rows': [['1874921.45', 2208]]}}
    analysis = Mock(ask=Mock(return_value=data))
    with client_for(analysis, tmp_path) as client:
        response = client.post('/api/ask', json={'question': '  2019年德国销售额  '}, headers={'Origin': 'http://127.0.0.1:8000'})
    assert response.status_code == 200 and response.json() == data
    assert response.headers['cache-control'] == 'no-store'
    analysis.ask.assert_called_once_with('2019年德国销售额')

def test_failures_release_busy_lock_and_hide_raw_error(tmp_path):
    analysis = Mock(ask=Mock(side_effect=[RuntimeError('secret-connection-string'), {'status': 'error', 'answer': '模型离线'}, {'status': 'clarify', 'answer': '请提供时间'}]))
    with client_for(analysis, tmp_path) as client:
        first = client.post('/api/ask', json={'question': '销量'})
        second = client.post('/api/ask', json={'question': '销量'})
        third = client.post('/api/ask', json={'question': '销量'})
    assert first.status_code == second.status_code == 503
    assert 'secret-connection' not in first.text
    assert third.status_code == 200 # 前次异常不能导致锁一直占用。

def test_concurrent_question_returns_busy(tmp_path):
    entered, release = Event(), Event()
    def slow_question(question):
        entered.set()
        assert release.wait(timeout=5)
        return {'status': 'clarify', 'answer': '请提供时间'}
    with client_for(SimpleNamespace(ask=slow_question), tmp_path) as client, ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, '/api/ask', json={'question': '销量'})
        try:
            assert entered.wait(timeout=5)
            busy = client.post('/api/ask', json={'question': '销量'})
            assert busy.status_code == 429 and busy.headers['retry-after'] == '3'
            assert client.get('/api/health').status_code == 200 # 正在分析时仍能处理轻量请求。
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200

def test_static_files_never_expose_project(tmp_path):
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets' / 'app.js').write_text('console.log("fixture")', encoding='utf-8')
    (tmp_path / 'index.html').write_text('<html>fixture</html>', encoding='utf-8')
    with client_for(Mock(), tmp_path) as client:
        assert client.get('/').status_code == 200
        assert client.get('/assets/app.js').status_code == 200
        for path in ('/.env', '/config/local_model.yaml', '/models/ollama', '/assets/%2e%2e/%2e%2e/.env', '/api/sql'):
            assert client.get(path).status_code == 404 # 未挂载的路径不允许落回项目目录。

def test_missing_build_reports_actionable_message(tmp_path):
    with client_for(Mock(), tmp_path) as client:
        response = client.get('/')
    assert response.status_code == 503 and 'npm run build' in response.text
