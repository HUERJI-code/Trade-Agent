"""本机 HTTP 客户端测试：检查请求参数、网络边界和响应验证。"""
import json # 构造模拟服务响应。
from unittest.mock import Mock, MagicMock # 不依赖真实模型的传输层测试。
from urllib.error import HTTPError, URLError # 模拟网络故障。
import pytest
from agent.local_model import ModelSettings, ModelError, OllamaClient, NoRedirect, MAX_RESPONSE_BYTES

@pytest.mark.parametrize('url',[
    'https://api.deepseek.com:443','http://example.com:11434','http://127.0.0.1:11434/other',
    'http://user:secret@127.0.0.1:11434','http://127.0.0.1:11434?x=1','http://127.0.0.1:11434#x',
    'http://127.0.0.1','http://127.0.0.1.evil.test:11434',
])
def test_remote_and_ambiguous_endpoints_rejected(url):
    with pytest.raises(ModelError):
        ModelSettings(base_url=url)

@pytest.mark.parametrize('options',[{'model':'qwen3:cloud'},{'context_tokens':99999},{'timeout_seconds':True},{'temperature':float('nan')}])
def test_resource_and_model_limits(options):
    with pytest.raises(ModelError):
        ModelSettings(**options)

def fake_client(body):
    client = OllamaClient(ModelSettings())
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body
    client.opener = Mock()
    client.opener.open.return_value = response
    return client

def test_request_uses_local_json_schema_and_no_thinking():
    body = {'done':True,'message':{'role':'assistant','content':'{}'},'prompt_eval_count':10,'eval_count':2,'total_duration':1000000}
    client = fake_client(json.dumps(body).encode())
    result = client.generate('safe system','问题',{'type':'object'})
    request = client.opener.open.call_args.args[0]
    sent = json.loads(request.data)
    assert request.full_url=='http://127.0.0.1:11434/api/chat'
    assert sent['stream'] is False and sent['think'] is False and sent['format']=={'type':'object'}
    assert sent['options']['num_ctx']==4096 and result.duration_ms==1.0
    assert NoRedirect().redirect_request(None,None,302,'',{},'https://example.com') is None

@pytest.mark.parametrize('body',[b'not JSON',b'[]',b' '* (MAX_RESPONSE_BYTES+1)],ids=['invalid-json','wrong-shape','oversized'])
def test_invalid_http_responses(body):
    with pytest.raises(ModelError):
        fake_client(body)._request('/api/chat',{})

@pytest.mark.parametrize('body',[
    {'done':False,'message':{'content':'{}'}}, {'done':True,'done_reason':'length','message':{'content':'{}'}},
    {'done':True,'message':{'content':''}}, {'done':True,'message':{'content':'{}'},'eval_count':-1},
])
def test_truncated_or_invalid_model_output(body):
    with pytest.raises(ModelError):
        fake_client(json.dumps(body).encode()).generate('system','question',{})

@pytest.mark.parametrize('error,code',[
    (HTTPError('url',404,'secret',{},None),'model_not_found'),
    (HTTPError('url',302,'secret',{},None),'model_http_error'),
    (URLError('secret connection detail'),'model_unavailable'),(TimeoutError('secret'),'model_timeout'),
])
def test_errors_are_sanitized(error,code):
    client = fake_client(b'{}')
    client.opener.open.side_effect = error
    with pytest.raises(ModelError) as caught:
        client._request('/api/chat',{})
    assert caught.value.code==code and 'secret' not in str(caught.value)

def test_config_file_is_strict(tmp_path):
    path = tmp_path/'config.yaml'
    path.write_text('version: 1\nmodel: qwen3:8b\nmodel: qwen3:4b',encoding='utf-8')
    with pytest.raises(ModelError):
        ModelSettings.load(path)
