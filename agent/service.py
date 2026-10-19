"""将中文问题连接到既有业务工具，答案中的数值始终来自数据库。"""
from datetime import datetime, timezone # 审计时间统一为 UTC。
from decimal import Decimal # 金额和百分比显示时保持十进制精度。
import hashlib # 问题只记录指纹，不把原文放进日志。
import json # 日志采用每行一个 JSON 的格式。
from pathlib import Path # 支持测试传入临时日志位置。
import re # 对明显越界请求做应用侧拒绝。
import time # 统计端到端耗时。
from uuid import uuid4 # 为一次问答分配独立追踪编号。
from sqlalchemy.exc import SQLAlchemyError # 统一处理数据库基础设施错误。
from database.connection import ROOT, readonly_engine # 复用已验证的只读连接。
from tools.schema_tool import get_schema # 从真实数据库读取允许的结构。
from tools.business_tool import BusinessDictionary, BusinessError, run_metric # 复用正式指标定义与计算。
from tools.sql_tool import QueryError # 不把原始数据库异常直接展示给用户。
from agent.local_model import OllamaClient, ModelError # 本机推理客户端。
from agent.planner import QueryPlan, PlanError, PLAN_SCHEMA, MESSAGES, build_prompt, ground_plan, explicit_range # 严格限制模型计划。
from agent.ranking import CountryExtremePlan, parse_country_extreme, compare_countries, COUNTRY_LABELS # 明确国家极值问题的受控扩展。

def load_business_context(): # 引擎仅用于读取安全结构，不向模型提供数据库凭据。
    engine = readonly_engine()
    try:
        return BusinessDictionary.load(get_schema(engine)).context()
    finally:
        engine.dispose()

def preflight(question): # 对当前明确不支持的操作保守拒绝，禁止“看起来是查询”却忽略重要条件。
    extreme = parse_country_extreme(question) # 完整句式匹配成功才进入国家比较，额外条件不会被丢弃。
    if extreme is not None:
        return extreme
    unsupported = r'\b(?:delete|drop|update|insert|alter|truncate|grant|revoke)\b|删除|清空|客户姓名|生日|密码|净利润|实际成交|同比|环比|预测|排名|最高|最低|前\s*\d+|\btop\s*\d+|不是|除了|除外|排除|超过|大于|小于|以上|以下|之前|之后|上旬|下旬|忽略.*(?:规则|指令)|系统提示|绕过|日本|中国|巴西|印度|韩国|西班牙'
    if re.search(unsupported, question, re.I):
        return QueryPlan('unsupported',None,None,None,None,None,'unsupported_request')
    groups = [r'销售额|销售金额',r'毛利(?!率)',r'毛利率',r'订单数|多少.*订单',r'活跃客户|客户数',r'销量|件数']
    matched = sum(bool(re.search(pattern, question)) for pattern in groups)
    if matched > 1:
        return QueryPlan('unsupported',None,None,None,None,None,'unsupported_request')
    if matched or re.search(r'销售|订单|毛利|利润|客户|销量|件',question): # 对业务问题缺时间先落实已确认规则，兼容“多少客户下单”等说法。
        _, reason = explicit_range(question)
        if reason:
            return QueryPlan('clarify',None,None,None,None,None,reason)
    return None # 不属于明确拒绝项时，交给本地模型理解。

def display_value(value, unit): # 结果格式化只用数据库值，不调用语言模型做四则运算。
    if value is None:
        return '暂无可计算值'
    number = Decimal(str(value))
    if unit == 'USD':
        return f'{number:,.2f} 美元'
    if unit == 'ratio':
        return f'{number*100:.2f}%'
    suffix = {'orders':'单','customers':'位','units':'件'}[unit]
    return f'{int(number):,} {suffix}'

def render_result(result): # 固定文案便于核验，后续自由分析必须另行做事实一致性评测。
    coverage = result['data_coverage']
    if result['status'] == 'outside_data_coverage':
        return f"请求的时间超出数据覆盖范围（{coverage['first_date']} 至 {coverage['last_date']}），不能将缺少数据解释为零业务。"
    scope = f"{result['start_inclusive']} 至 {result['end_exclusive']}（不含结束日），客户国家：{result['country'] or '全部'}。"
    if result['status'] == 'no_matching_transactions':
        answer = scope+'所选条件下没有匹配的销售明细；计数为零，金额或比率可能为空。'
    elif result.get('ranking'):
        ranking = result['ranking']
        winners = ranking['winners']
        label = '最高' if ranking['sort_order'] == 'desc' else '最低'
        if winners:
            countries = '、'.join(COUNTRY_LABELS.get(row[0],str(row[0])) for row in winners)
            tied = '并列' if len(winners)>1 else ''
            answer = scope+f"在所选期间有交易且指标可计算的 {ranking['compared_groups']} 个客户国家中，{result['label']}{tied}{label}的是{countries}，{'均为' if tied else '为'} {display_value(winners[0][1],result['unit'])}。"
        else:
            answer = scope+'没有可比较的指标值，无法判断最高或最低国家。'
        if ranking['null_groups']:
            answer += f"另有 {ranking['null_groups']} 个国家的指标为空，未参与比较。"
    elif result['dimension'] is None:
        value = result['rows'][0][0] # 未分组的指标是第一列，第二列为匹配销售明细数。
        answer = scope+f"{result['label']}为 {display_value(value,result['unit'])}。"
    else:
        answer = scope+f"已按所选维度返回 {len(result['rows'])} 组{result['label']}，请查看明细表。"
    if coverage['partial']:
        answer += '请求区间只有部分落在样例数据日期范围内。'
    if result['limit_reached']:
        answer += '结果已触及返回行数上限，不能保证是完整分组结果。'
    if result['unit'] in ('USD','ratio'):
        answer += '金额与毛利率按产品标价及单位成本估算，不代表实际成交收入或净利润。'
    return answer

class AnalysisAgent:
    def __init__(self, client=None, runner=run_metric, context_loader=load_business_context, audit_path=None):
        self.client = client # 延迟创建真实客户端，离线测试可以注入模拟模型。
        self.runner = runner # 只允许受信任的 Python 调用方替换执行器，用户 JSON 不能改变它。
        self.context_loader = context_loader
        self.audit_path = Path(audit_path) if audit_path else ROOT/'logs'/'agent_queries.jsonl'

    def ask(self, question):
        trace_id, start = str(uuid4()), time.perf_counter()
        audit = {'trace_id':trace_id,'at':datetime.now(timezone.utc).isoformat(),'status':'error','attempts':0,
                 'question_sha256':hashlib.sha256(question.encode('utf-8')).hexdigest() if isinstance(question,str) else None}
        response = {'trace_id':trace_id,'status':'error','answer':'','plan':None,'result':None,'model':None,'usage':[]}
        try:
            if not isinstance(question,str) or not question.strip() or len(question)>1000:
                response.update(error='invalid_question',answer='请输入 1 至 1000 字符的业务问题。')
                return response
            question = question.strip()
            plan = preflight(question)
            if plan is None:
                client = self.client or OllamaClient() # 模型未启动时会明确失败，不伪装成本地大模型问答。
                response['model'] = client.settings.model
                system = build_prompt(self.context_loader(), tool_call=getattr(client, 'planner_source', None) == 'langchain_tool_call')
                retry_system = system
                for attempt in range(2): # 仅格式/参数校验错误重试一次；网络故障不重复昂贵推理。
                    audit['attempts'] += 1
                    try:
                        reply = client.generate(retry_system,question,PLAN_SCHEMA,repair=attempt>0)
                        response['usage'].append({'prompt_tokens':reply.prompt_tokens,'output_tokens':reply.output_tokens,'duration_ms':reply.duration_ms})
                        plan = ground_plan(QueryPlan.parse(reply.content),question)
                        break
                    except PlanError as error:
                        retry_system = system + '\n上次参数校验反馈：' + str(error)  # 提供实际失败原因，仍然最多重试一次。
                        if attempt == 1:
                            raise
            response['plan'] = plan.to_dict()
            response['planner_source'] = 'country_ranking_rule' if isinstance(plan,CountryExtremePlan) else (getattr(client, 'planner_source', 'local_model') if response['model'] else 'preflight_rule') # 真实说明计划来自规则、JSON 模型或 LangChain 工具调用。
            if plan.action != 'query':
                response.update(status=plan.action,answer=MESSAGES[plan.reason])
                return response # 澄清与不支持的问题均不会触发业务数据查询。
            result = self.runner(**plan.arguments()) # 指标公式由正式字典决定，模型没有写 SQL 的入口。
            if isinstance(plan,CountryExtremePlan):
                result = compare_countries(result,plan.sort_order) # 完整聚合后精确比较，禁止在截断结果上猜最大值。
            response.update(status='ok',answer=render_result(result),result=result)
            audit['query_trace_id'] = result.get('trace_id') # 可关联既有 SQL 审计记录。
            return response
        except PlanError:
            response.update(error='invalid_plan',answer='模型两次未能给出可靠参数，未执行查询。请明确一个指标、时间范围及国家。')
            return response
        except (ModelError,BusinessError,QueryError) as error:
            response.update(error=error.code,answer=str(error)) # 这些异常已由相应模块清理敏感细节。
            return response
        except (SQLAlchemyError,ValueError,OSError):
            response.update(error='analysis_unavailable',answer='分析服务暂不可用，请检查项目中的数据库、模型配置与日志。')
            return response
        finally:
            response['elapsed_ms'] = round((time.perf_counter()-start)*1000,2)
            audit.update(status=response['status'],error=response.get('error'),model=response['model'],planner_source=response.get('planner_source'),elapsed_ms=response['elapsed_ms'],usage=response['usage'])
            self.audit_path.parent.mkdir(parents=True,exist_ok=True)
            with self.audit_path.open('a',encoding='utf-8') as output:
                output.write(json.dumps(audit,ensure_ascii=False)+'\n') # 不记录原问题、模型原文、结果值或凭据。
