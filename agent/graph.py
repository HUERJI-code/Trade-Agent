"""LangGraph：恢复会话 → 补齐条件 → 分析查询 → 保存回答。"""
import sqlite3  # 在 D 盘保存会话，不向只读业务库写入数据。
import threading  # 序列化本机图执行，避免同会话发生交错更新。
import re  # 判断本轮是否仅补充时间或指标。
from pathlib import Path  # 确保会话目录存在。
from typing import TypedDict  # 声明节点之间传递的状态字段。
from uuid import UUID, uuid4  # 用独立 ID 区分多个聊天窗口。
from datetime import datetime, timezone  # 会话列表按更新时间排列。
from langgraph.graph import StateGraph, START, END  # 定义节点与执行方向。
from langgraph.checkpoint.sqlite import SqliteSaver  # 将每个会话的检查点持久化到 SQLite。
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # 不从检查点任意加载 Python 类。
from agent.local_model import ROOT  # 统一定位项目根目录。
from agent.sql_agent import SQLAnalysisAgent  # 模型生成完整 SELECT，复用底层只读执行与审计。
from agent.conversation import complete_question  # 补齐追问省略的条件。
from agent.planner import explicit_range, mentioned_countries  # 复用明确条件识别。
import yaml  # 读取回答模型是否已经完成部署。
from agent.answer_style import AnswerWriter  # 使用微调模型组织回答并核对关键事实。


class ConversationState(TypedDict, total=False):
    question: str  # 用户本轮原始问题。
    effective_question: str  # 补齐条件后的可独立查询问题。
    inherited: list[str]  # 告知前端哪些条件来自上轮。
    confirmed_scope: dict  # 只有成功执行后才更新的查询条件。
    response: dict  # 本轮答案、结果表、SQL 和追踪信息。
    history: list[dict]  # 给页面恢复显示的最近消息。
    pending_question: str | None  # 缺时间或指标时保存待补充的问题。


class ConversationAgent:
    def __init__(self, analysis=None, path=None, answer_writer=None):
        location = path or ROOT / '.runtime/conversations.sqlite3'
        Path(location).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(location), check_same_thread=False)
        self.connection.execute('CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT, updated TEXT)')
        self.connection.commit()
        self.lock = threading.RLock()  # API 之外直接调用时也保护会话状态。
        self.analysis = analysis or SQLAnalysisAgent()
        self.sql_mode = getattr(self.analysis, 'query_mode', None) == 'sql'  # 注入旧分析器的测试仍可验证历史行为。
        enabled = yaml.safe_load((ROOT / 'config/answer_model.yaml').read_text(encoding='utf-8'))['enabled']
        self.answer_writer = answer_writer or (AnswerWriter() if analysis is None and enabled else None)
        builder = StateGraph(ConversationState)  # 节点返回字典，用于更新本轮状态。
        builder.add_node('resolve_context', self.resolve_context)
        builder.add_node('query', self.query)
        builder.add_node('write_answer', self.write_answer)
        builder.add_node('remember', self.remember)
        builder.add_edge(START, 'resolve_context')
        builder.add_edge('resolve_context', 'query')
        builder.add_edge('query', 'write_answer')
        builder.add_edge('write_answer', 'remember')
        builder.add_edge('remember', END)
        serializer = JsonPlusSerializer(allowed_msgpack_modules=[])  # 状态只需字典、列表、文本和数字。
        self.graph = builder.compile(checkpointer=SqliteSaver(self.connection, serde=serializer))

    def resolve_context(self, state):
        if self.sql_mode:
            return {'effective_question': state['question'], 'inherited': []}  # 原问题直接交给模型，不再套单时间/国家补齐规则。
        pending = state.get('pending_question')
        question = state['question']
        if pending:
            new_period, _ = explicit_range(question)
            old_period, old_reason = explicit_range(pending)
            new_metric = bool(re.search(r'销售|毛利|订单|客户数|销量|件数', question))
            old_metric = bool(re.search(r'销售|毛利|订单|客户数|销量|件数', pending))
            # 仅补充所缺字段时接续澄清；完整的新问题不拼上旧问题。
            date_only = old_reason == 'missing_time' and new_period and not new_metric and not mentioned_countries(question)
            metric_only = old_period and not old_metric and new_metric and not new_period and not mentioned_countries(question)
            if date_only or metric_only:
                return {'effective_question': pending + '；' + question, 'inherited': ['pending_question']}
        effective, inherited = complete_question(state['question'], state.get('confirmed_scope'))
        return {'effective_question': effective, 'inherited': inherited}

    def query(self, state):
        if self.sql_mode:
            previous = []  # 只取本会话最近三轮，不共享别的会话，不复制大结果表。
            for turn in state.get('history', [])[-3:]:
                reply = turn.get('response', {})
                plan = reply.get('plan') or {}
                previous.append({'question': turn['question'], 'status': reply.get('status'),
                                 'plan': {key: (value[:3500] if isinstance(value, str) else value) for key, value in plan.items()},
                                 'clarification': reply.get('answer', '')[:500] if reply.get('status') == 'clarify' else None})
            response = self.analysis.ask(state['effective_question'], history=previous)
        else:
            response = self.analysis.ask(state['effective_question'])
        response.update(effective_question=state['effective_question'], inherited_conditions=state['inherited'])
        return {'response': response}

    def remember(self, state):
        response = state['response']
        history = (state.get('history', []) + [{'question': state['question'], 'response': response}])[-30:]
        update = {'history': history, 'pending_question': None}
        if response.get('status') == 'clarify' and (response.get('plan') or {}).get('reason') in ('missing_time', 'missing_metric'):
            update['pending_question'] = state['effective_question']
        if response.get('status') == 'ok' and response.get('plan', {}).get('action') == 'query':
            update['confirmed_scope'] = response['plan']  # 失败或不支持的请求不能污染已确认条件。
        return update

    def write_answer(self, state):
        if self.answer_writer:
            return {'response': self.answer_writer.rewrite(state['effective_question'], state['response'])}
        return {}  # 部署前继续显示原有可核对文案。

    def create_conversation(self):
        identifier = str(uuid4())
        with self.lock:
            self.connection.execute('INSERT INTO conversations VALUES (?, ?, ?)',
                (identifier, '新对话', datetime.now(timezone.utc).isoformat()))
            self.connection.commit()
        return {'id': identifier, 'title': '新对话'}

    def list_conversations(self):
        with self.lock:
            rows = self.connection.execute('SELECT id, title, updated FROM conversations ORDER BY updated DESC LIMIT 100').fetchall()
        return [dict(zip(('id', 'title', 'updated'), row)) for row in rows]

    def checked_id(self, identifier):
        identifier = str(UUID(identifier))  # 不把用户给的字符串当文件路径或 SQL。
        if not self.connection.execute('SELECT 1 FROM conversations WHERE id=?', (identifier,)).fetchone():
            raise ValueError('会话不存在。')
        return identifier

    def get_conversation(self, identifier):
        with self.lock:
            identifier = self.checked_id(identifier)
            state = self.graph.get_state({'configurable': {'thread_id': identifier}})
            return {'id': identifier, 'history': state.values.get('history', [])}

    def ask(self, question, conversation_id=None):
        with self.lock:
            identifier = self.checked_id(conversation_id) if conversation_id else self.create_conversation()['id']
            state = self.graph.invoke({'question': question}, {'configurable': {'thread_id': identifier}})
            self.connection.execute('UPDATE conversations SET title=CASE WHEN title=? THEN ? ELSE title END, updated=? WHERE id=?',
                ('新对话', question[:32], datetime.now(timezone.utc).isoformat(), identifier))
            self.connection.commit()
            return {**state['response'], 'conversation_id': identifier}

    def close(self):
        self.connection.close()  # 服务生命周期结束时释放会话数据库。
