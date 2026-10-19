"""只访问本机 Ollama；不上传问题、表结构或结果至云端。"""
from dataclasses import dataclass # 保存模型配置和推理统计。
import ipaddress # 验证目标为回环 IP，不通过域名跳转访问外部服务。
import json # 编码 HTTP 请求并解析结构化响应。
from pathlib import Path # 定位项目内配置文件。
import socket # 识别网络超时。
from urllib.error import HTTPError, URLError # 将底层网络错误转换为可展示的错误码。
from urllib.parse import urlsplit # 检查 URL 的协议、主机、端口和路径。
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler # 调用本机 HTTP，不使用系统代理。
import yaml # 读取不含密码的本地模型配置。

ROOT = Path(__file__).resolve().parents[1] # agent 目录上一级是项目根目录。
MAX_RESPONSE_BYTES = 262144 # 限制 HTTP 响应为 256 KiB，防止异常响应无限占用内存。

class ModelError(ValueError): # 对外仅返回受控提示，不暴露原始响应或内部请求。
    def __init__(self, code, message):
        self.code = code # 稳定错误码供终端、未来 API 和测试识别。
        super().__init__(message) # 保留安全说明。

class NoRedirect(HTTPRedirectHandler): # 本机服务不能把请求转发到其他站点。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None # urllib 将重定向作为 HTTPError 处理。

@dataclass(frozen=True)
class ModelSettings: # 配置创建后不可随意修改。
    base_url: str = 'http://127.0.0.1:11434'
    model: str = 'qwen3:8b'
    timeout_seconds: int = 120
    context_tokens: int = 4096
    output_tokens: int = 384
    temperature: float = 0.1
    seed: int = 42

    def __post_init__(self): # 配置由文件加载后仍要验证，不能将本地模式改成云端请求。
        try:
            url = urlsplit(self.base_url) # 结构化拆分 URL，避免简单前缀匹配被欺骗。
            local = url.hostname == 'localhost' or ipaddress.ip_address(url.hostname).is_loopback
            if not local or url.scheme != 'http' or not url.port or url.username or url.password or url.path or url.query or url.fragment:
                raise ValueError('Invalid local endpoint')
            if self.model not in ('qwen3:8b', 'qwen3:4b', 'trade-agent-qwen3:8b-lora-v1'):
                raise ValueError('Only reviewed local model tags are allowed')
            for name, low, high in [('timeout_seconds', 1, 300), ('context_tokens', 2048, 8192), ('output_tokens', 128, 1024), ('seed', 0, 2147483647)]:
                value = getattr(self, name) # 分项检查资源设置。
                if type(value) is not int or not low <= value <= high:
                    raise ValueError('Model option out of bounds')
            if type(self.temperature) not in (int, float) or not 0 <= self.temperature <= 1:
                raise ValueError('Temperature out of bounds')
        except (ValueError, TypeError, AttributeError):
            raise ModelError('invalid_model_config', '本地模型配置无效；只允许本机 Ollama 与受限推理参数。') from None

    @classmethod
    def load(cls, path=ROOT/'config'/'local_model.yaml'):
        try:
            text = Path(path).read_text(encoding='utf-8') # 只读取模型配置，不读取 .env。
            if len(text) > 8192:
                raise ValueError('Configuration too large')
            from tools.business_tool import UniqueSafeLoader # 复用拒绝重复 YAML 键的安全加载器。
            if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for token in yaml.scan(text)):
                raise ValueError('No YAML aliases')
            values = yaml.load(text, Loader=UniqueSafeLoader)
            if not isinstance(values, dict) or values.pop('version', None) != 1:
                raise ValueError('Unsupported version')
            return cls(**values) # 未知参数也会被 dataclass 构造函数拒绝。
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            raise ModelError('invalid_model_config', '无法加载 config/local_model.yaml；请检查模型配置。') from None

@dataclass(frozen=True)
class ModelReply: # 只保留模型内容与可度量的运行统计，不保存隐藏思考。
    content: str
    prompt_tokens: int
    output_tokens: int
    duration_ms: float

class OllamaClient:
    def __init__(self, settings=None):
        self.settings = settings or ModelSettings.load() # 测试可传入受控的本机测试服务地址。
        self.opener = build_opener(ProxyHandler({}), NoRedirect()) # 明确绕过代理，拒绝 HTTP 跳转。

    def _request(self, route, payload=None, timeout=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = Request(self.settings.base_url+route, data=data, headers={'Content-Type':'application/json'})
        try:
            with self.opener.open(request, timeout=timeout or self.settings.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES+1) # 多读一字节以检查上限。
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ModelError('invalid_model_response', '本地模型响应超过大小限制。')
            result = json.loads(raw) # 不执行响应中的任何代码。
            if not isinstance(result, dict):
                raise ValueError('Expected object')
            return result
        except HTTPError as error:
            code = 'model_not_found' if error.code == 404 else 'model_http_error'
            message = '本机尚未安装所选模型，请运行模型下载脚本。' if error.code == 404 else '本机 Ollama 返回错误，请检查项目中的服务日志。'
            raise ModelError(code, message) from None
        except (socket.timeout, TimeoutError):
            raise ModelError('model_timeout', '本地模型响应超时；请检查模型加载状态或缩小上下文。') from None
        except URLError as error:
            code = 'model_timeout' if isinstance(error.reason, (socket.timeout, TimeoutError)) else 'model_unavailable'
            raise ModelError(code, '无法连接本机 Ollama；请先启动 scripts/local_model.ps1 start。') from None
        except (ValueError, UnicodeError, TypeError) as error:
            if isinstance(error, ModelError):
                raise
            raise ModelError('invalid_model_response', '本地模型返回的内容不是有效 JSON。') from None

    def status(self): # 健康检查不执行推理，也不自动下载任何模型。
        version = self._request('/api/version', timeout=5)
        models = self._request('/api/tags', timeout=5)
        entries = models.get('models', []) # 查询真实已安装模型，不根据目录存在就猜测下载完成。
        if not isinstance(entries,list):
            raise ModelError('invalid_model_response','本机模型列表格式无效。')
        selected = next((entry for entry in entries if isinstance(entry,dict) and entry.get('name')==self.settings.model),None)
        return {'version':version.get('version'), 'model':self.settings.model, 'installed':selected is not None,
                'model_digest':selected.get('digest') if selected else None} # 模型指纹供复核版本，不能只记录可变标签。

    def generate(self, system, question, schema, repair=False): # 一次请求只提取参数；不让模型接触数据库密码。
        messages = [{'role':'system', 'content':system}, {'role':'user', 'content':question}]
        if repair:
            messages.append({'role':'user', 'content':'上次输出不符合约定。只输出指定 JSON，不加字段、不猜测日期、不输出 SQL。'})
        settings = self.settings
        payload = {'model':settings.model, 'messages':messages, 'stream':False, 'think':False, 'format':schema,
                   'keep_alive':'5m', 'options':{'temperature':settings.temperature, 'seed':settings.seed,
                   'num_ctx':settings.context_tokens, 'num_predict':settings.output_tokens}}
        result = self._request('/api/chat', payload) # 只调用本机模型，不使用云端兜底。
        message = result.get('message')
        if result.get('done') is not True or result.get('done_reason') == 'length' or not isinstance(message, dict):
            raise ModelError('invalid_model_response', '模型输出不完整，未执行数据库查询。')
        content = message.get('content')
        if not isinstance(content, str) or not content.strip() or len(content) > 16000:
            raise ModelError('invalid_model_response', '模型未返回有效查询参数。')
        values = [result.get(key, 0) for key in ('prompt_eval_count', 'eval_count', 'total_duration')]
        if any(type(value) is not int or value < 0 for value in values):
            raise ModelError('invalid_model_response', '模型运行统计格式错误。')
        return ModelReply(content, values[0], values[1], round(values[2]/1000000, 2))
