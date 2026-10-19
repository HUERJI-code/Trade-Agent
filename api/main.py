"""只供本机使用的 FastAPI 服务；网页不能提交原始 SQL 或修改模型配置。"""
import threading # 用非阻塞锁限制同时进行的模型分析数量。
from dataclasses import replace # 创建短超时健康检查配置，不改变正式推理参数。
from pathlib import Path # 按代码位置定位前端构建目录。
from contextlib import asynccontextmanager # 在服务停止时关闭会话数据库。
from fastapi import FastAPI, Request # 应用对象和当前 HTTP 请求。
from fastapi.exceptions import RequestValidationError # 捕获请求字段校验错误。
from fastapi.responses import FileResponse, JSONResponse # 分别返回网页和 JSON。
from fastapi.staticfiles import StaticFiles # 只公开前端构建后的资源文件。
from pydantic import BaseModel, ConfigDict, Field # 声明允许的请求结构。
from starlette.concurrency import run_in_threadpool # 避免同步模型调用阻塞异步事件循环。
from agent.service import load_business_context # 健康检查继续验证数据库和业务字典。
from agent.local_model import OllamaClient, ModelSettings # 读取本机模型状态。
from agent.graph import ConversationAgent # LangGraph 会话与 LangChain 工具调用入口。

ROOT = Path(__file__).resolve().parents[1] # 项目根目录，与启动终端的位置无关。
DIST = ROOT / 'frontend' / 'dist' # 只挂载这个目录，不公开项目文件。

class AskRequest(BaseModel): # 接口只接受一个完整的自然语言问题。
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True) # 禁止额外 SQL 字段及隐式类型转换。
    question: str = Field(min_length=1, max_length=1000) # 去空格后不能是空问题。
    conversation_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') # 由服务端创建的会话 ID。

def create_app(analysis=None, health_check=None, static_dir=DIST): # 可注入模拟服务，便于离线测试接口边界。
    @asynccontextmanager
    async def lifespan(app):
        yield  # 服务处理请求期间保持数据库连接。
        if analysis is None:
            agent.close()
    app = FastAPI(title='Trade Agent 本机分析接口', docs_url=None, redoc_url=None, lifespan=lifespan) # 文档不依赖外部 CDN。
    agent = analysis or ConversationAgent() # 网页通过图恢复历史，再复用既有分析器。
    gate = threading.Lock() # 单进程一次只做一个分析，避免 GPU 请求排队堆积。

    @app.middleware('http')
    async def check_request(request: Request, call_next): # 在模型调用前限制网页来源与请求体大小。
        origin = request.headers.get('origin') # 浏览器 POST 自带 Origin；本机命令行可不带。
        if origin and origin != f'http://{request.headers.get("host", "")}':
            return JSONResponse({'detail': '仅接受本机页面的同源请求。'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '不接受跨站请求。'}, status_code=403)
        if request.method == 'POST':
            if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                return JSONResponse({'detail': '请使用 JSON 请求。'}, status_code=415)
            body = bytearray() # 同时检查实际流大小，不能只信任 Content-Length。
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192: # 1000 个中文字符的 JSON 可以容纳，超大输入提前拒绝。
                    return JSONResponse({'detail': '请求内容过长。'}, status_code=413)
            request._body = bytes(body) # 缓存已读取的数据，供下游 JSON 解析继续使用。
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store' # 查询结果不进入浏览器 HTTP 缓存。
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'" # Plotly 使用行内样式；脚本均为本地构建文件。
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error): # 不把校验异常中的原始输入回显到错误响应。
        return JSONResponse({'detail': '请输入 1 至 1000 字符的问题，可附带有效的 conversation_id。'}, status_code=422)

    @app.get('/api/conversations')
    def conversations():
        return agent.list_conversations()  # 本机单用户的聊天目录。

    @app.post('/api/conversations')
    def new_conversation():
        return agent.create_conversation()  # 新建会话不会调用模型。

    @app.get('/api/conversations/{conversation_id}')
    def conversation(conversation_id: str):
        try:
            return agent.get_conversation(conversation_id)
        except ValueError:
            return JSONResponse({'detail': '会话不存在。'}, status_code=404)

    @app.get('/api/health')
    def health(): # 检查可用性不做模型推理，也不计算业务指标。
        if health_check:
            return health_check()
        result = {'status': 'degraded', 'model': 'unavailable', 'database': 'unavailable'}
        try:
            client = OllamaClient(replace(ModelSettings.load(), timeout_seconds=3)) # 只缩短状态查询的等待时间。
            status = client.status()
            if status['installed']:
                result['model'] = status['model']
        except Exception: # 健康检查只给可用状态，不泄露连接错误或配置内容。
            pass
        try:
            load_business_context() # 实际验证只读账户、Schema 和业务字典。
            result['database'] = 'ready'
        except Exception:
            pass
        if result['model'] != 'unavailable' and result['database'] == 'ready':
            result['status'] = 'ready'
        return result

    @app.post('/api/ask')
    async def ask(payload: AskRequest):
        if not gate.acquire(blocking=False): # 忙碌时明确拒绝，不在内存中积压请求。
            return JSONResponse({'detail': '正在处理另一个问题，请稍后重试。'}, status_code=429, headers={'Retry-After': '3'})
        try:
            if payload.conversation_id:
                result = await run_in_threadpool(agent.ask, payload.question, conversation_id=payload.conversation_id)
            else:
                result = await run_in_threadpool(agent.ask, payload.question) # 不带 ID 的旧调用仍然有效。
            return JSONResponse(result, status_code=503 if result['status'] == 'error' else 200)
        except ValueError:
            return JSONResponse({'detail': '会话不存在，请新建对话。'}, status_code=404)
        except Exception:
            return JSONResponse({'detail': '分析服务暂不可用，请检查本机服务。'}, status_code=503)
        finally:
            gate.release() # 成功、失败后都允许处理下一次请求。

    @app.get('/')
    def homepage():
        entry = Path(static_dir) / 'index.html'
        if not entry.is_file():
            return JSONResponse({'detail': '页面尚未构建，请在 frontend 目录运行 npm run build。'}, status_code=503)
        return FileResponse(entry) # 文件路径固定，用户不能指定服务器文件。

    assets = Path(static_dir) / 'assets'
    if assets.is_dir():
        app.mount('/assets', StaticFiles(directory=assets), name='assets') # 仅发布编译资源，不公开 .env、数据或模型。
    return app

app = create_app() # Uvicorn 通过 api.main:app 找到服务入口。
