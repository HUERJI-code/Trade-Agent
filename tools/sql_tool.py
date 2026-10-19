# 文件作用：阶段 1：安全 SQL 执行器。先取结构和校验 SQL，之后才发送规范 SQL；限制返回量，统一结果和错误格式，写入脱敏审计日志。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Validated SQL execution with bounded rows, sanitized errors and audit traces."""  # 说明本工具负责有界返回、安全错误和查询追踪。
import argparse  # 导入：argparse 用于解析终端中的 --参数。
from dataclasses import asdict, dataclass  # 导入：asdict 用于将 dataclass 结果转成可序列化字典；dataclass 用于自动生成数据容器的初始化等方法。
from datetime import date, datetime, timezone  # 导入：date 用于表示日期并校验 ISO 日期；datetime 用于解析或表示日期时间；timezone 用于指定 UTC 时区。
from decimal import Decimal  # 导入：Decimal 用于精确表示十进制金额，避免普通浮点误差。
import hashlib  # 导入：hashlib 用于计算 SHA-256 文件或查询指纹。
import json  # 导入：json 用于在 Python 对象与 JSON 文本之间转换。
from pathlib import Path  # 导入：Path 用于以对象方式拼接、读取和写入文件路径。
import time  # 导入：time 用于用 perf_counter 测量查询耗时。
from uuid import uuid4  # 导入：uuid4 用于生成每次查询独立的追踪编号。

from sqlalchemy.exc import DBAPIError, SQLAlchemyError  # 导入：DBAPIError 用于捕获底层数据库驱动错误；SQLAlchemyError 用于捕获数据库引擎和连接等异常。

from database.connection import ROOT, readonly_engine  # 导入：ROOT 用于取得项目根目录常量；readonly_engine 用于创建带只读身份、权限和会话检查的数据库引擎。
from security.permissions import DEFAULT_POLICY  # 导入：DEFAULT_POLICY 用于取得统一的默认查询资源限制。
from security.sql_validator import SQLValidationError, validate_sql  # 导入：SQLValidationError 用于处理 SQL 规则校验失败；validate_sql 用于调用 SQL 白名单与资源限制校验。
from tools.schema_tool import get_schema  # 导入：get_schema 用于读取并过滤真实数据库结构。

class QueryError(Exception):  # 定义面向调用方的查询错误类型。
    def __init__(self,code,message,trace_id):  # 错误携带错误码、可展示说明和本次追踪编号。
        self.code,self.trace_id = code,trace_id  # 分别保存 code 和 trace_id。
        super().__init__(message)  # 将说明交给异常父类。

@dataclass  # dataclass 为结果类自动生成初始化方法，方便整体转字典。
class QueryResult:  # 定义统一查询结果结构。
    trace_id: str  # trace_id 是这次查询的 UUID，可用于查日志。
    columns: list  # columns 按结果顺序记录列名。
    rows: list  # rows 是二维列表，每个内部列表是一行。
    row_count: int  # row_count 是实际返回的行数。
    row_limit: int  # row_limit 是本次允许的最大返回行数。
    limit_applied: bool  # limit_applied 表示限制是否由工具补上或缩小。
    limit_reached: bool  # limit_reached 表示返回量碰到了上限，不保证数据库中一定还有下一行。
    elapsed_ms: float  # elapsed_ms 保存本次执行耗时，单位毫秒。
    sql: str  # sql 保存实际执行的规范 SQL；它包含筛选值，不能当成脱敏日志随意公开。

def json_value(value):  # 定义把数据库值转换为 JSON 可表示值的函数。
    if isinstance(value,Decimal):  # 发现 Decimal 金额或聚合小数时进入此分支。
        return str(value)  # Preserve decimal accuracy for downstream arithmetic.  # 转成字符串保持精度；因此金额 JSON 可能是 "123.45"，后续计算应转回 Decimal。
    if isinstance(value,(date,datetime)):  # 日期或日期时间对象进入此分支。
        return value.isoformat()  # 转成 ISO 格式字符串，便于 JSON 输出。
    return value  # 整数、文本、None 等直接返回；None 序列化为 JSON null。

class SQLTool:  # 定义可复用的 SQLTool 类。
    def __init__(self,engine,*,policy=DEFAULT_POLICY,audit_path=None):  # 接收数据库引擎、仅限关键字的策略参数及可选日志位置。
        self.engine,self.policy = engine,policy  # 保存引擎和策略。
        self.audit_path = Path(audit_path) if audit_path is not None else ROOT/'logs'/'sql_queries.jsonl'  # 没传日志路径时写入项目 logs/sql_queries.jsonl；测试可用临时路径。
        self._schema = None  # 结构缓存初始为空，首次查询会从数据库加载。

    def run_sql(self,sql):  # 定义执行一条用户/上层工具 SQL 的方法。
        trace_id,start = str(uuid4()),time.perf_counter()  # 生成 UUID，使用单调高精度计时器记录开始时刻。
        audit = {'trace_id':trace_id,'at':datetime.now(timezone.utc).isoformat(),  # 建立日志对象，记录追踪号和 UTC 时间。
                 'sql_sha256':hashlib.sha256(sql.encode('utf-8')).hexdigest() if isinstance(sql,str) else None,  # 字符串输入只记录其 SHA-256 指纹；不是字符串则记录 None。哈希不是对低熵输入的加密保证。
                 'status':'failed','row_count':0}  # 默认状态为失败、行数为 0，只有成功后才改成 ok。
        try:  # 开始处理校验、执行和异常。
            if self._schema is None:  # 第一次使用工具时缓存为空。
                self._schema = get_schema(self.engine)  # 加载真实且过滤后的表结构；这里可能产生元数据连接。
            checked = validate_sql(sql,self._schema,self.policy)  # 先检查 SQL；校验失败时不会进入后面的业务查询执行连接。
            audit['sql_template'] = checked.audit_template  # 把脱敏后的 SQL 模板放入日志。
            with self.engine.connect() as connection:  # 取出数据库连接；正常使用 readonly_engine 时会触发身份和权限核验。
                # 说明即使受信任的内部调用者传入其他引擎，也会设置本工具的查询超时；这不等于会替该引擎补齐所有权限核验。
                connection.exec_driver_sql(f'SET SESSION max_execution_time={self.policy.timeout_ms}')  # 执行固定会话语句，设置当前策略的服务器超时。
                # 注释说明待执行 SQL 已经由 AST 正确编码了字面量。
                # 注释说明避免空 DBAPI 参数让 PyMySQL 误解释 DATE_FORMAT 或 LIKE 的百分号。
                with connection.execution_options(stream_results=True,no_parameters=True).exec_driver_sql(checked.sql) as result:  # 采用流式结果和 no_parameters=True，只执行 checked.sql；with 负责关闭结果对象。
                    columns = list(result.keys())  # 读取结果列名。
                    fetched = result.fetchmany(checked.row_limit+1)  # 最多尝试取最终上限加 1 行，用于发现 LIMIT 没被执行的异常情况；正常数据库 LIMIT 不会多返回。
                    if len(fetched) > checked.row_limit:  # 如果实际取到的行超过已校验上限，说明发生异常。
                        raise QueryError('row_limit_failed','Database returned more rows than the validated limit.',trace_id)  # 抛出行数限制失效错误，不将过量结果交给调用方。
                    rows = [[json_value(value) for value in row] for row in fetched]  # 逐行逐值做 JSON 兼容转换。
            elapsed = round((time.perf_counter()-start)*1000,2)  # 计算从开始到执行完成的耗时，保留两位小数。
            audit.update(status='ok',row_count=len(rows))  # 成功日志改为 ok，并记录实际返回量。
            return QueryResult(trace_id,columns,rows,len(rows),checked.row_limit,checked.limit_applied,  # 构造 QueryResult，填入追踪号、列、行和限制信息。
                               checked.row_limit > 0 and len(rows)==checked.row_limit,elapsed,checked.sql)  # 补充是否碰到非零上限、耗时、实际 SQL；LIMIT 0 不会标为达到上限。
        except SQLValidationError as error:  # 捕获 SQL 规则拒绝错误。
            audit.update(status='rejected',error_code=error.code)  # 日志标为 rejected，并保留稳定错误码。
            raise QueryError(error.code,str(error),trace_id) from None  # 转换成外部统一 QueryError，隐藏底层异常链。
        except QueryError as error:  # 捕获执行器自己已经构造的 QueryError。
            audit['error_code'] = error.code  # 记下该错误码。
            raise  # 原样继续抛出，不重复包装。
        except DBAPIError as error:  # 捕获底层数据库驱动错误。
            number = error.orig.args[0] if getattr(error.orig,'args',None) else None  # 安全取得 MySQL 错误编号，无法取得则用 None。
            code = 'query_timeout' if number in (3024,1205,1317,2013) else 'database_error'  # 将超时、锁等待、查询中断、连接中断等指定编号归为 query_timeout；其余为 database_error。
            audit['error_code'] = code  # 日志保存分类后的错误码。
            message = 'Query exceeded a server/client time limit or the connection was interrupted.' if code=='query_timeout' else 'Database query failed; inspect configuration or refresh the schema.'  # 使用预设的安全提示；原始数据库错误里可能带查询或连接详情，所以不直接返回。
            raise QueryError(code,message,trace_id) from None  # 抛出包含追踪号的统一错误，隐藏原始异常链。
        except (SQLAlchemyError,ValueError) as error:  # 处理连接引擎或结构验证产生的其他已知错误。
            audit['error_code'] = 'connection_or_schema_error'  # 在日志中标记连接或结构错误。
            raise QueryError('connection_or_schema_error','Database connection or schema check failed.',trace_id) from None  # 返回统一的连接/结构检查失败说明。
        finally:  # 不论查询成功、被拒绝还是失败，最后都执行日志写入。
            audit['elapsed_ms'] = round((time.perf_counter()-start)*1000,2)  # 记录日志写入前的总耗时，与返回结果的 elapsed_ms 可能略有差别。
            self.audit_path.parent.mkdir(parents=True,exist_ok=True)  # 确保日志父目录存在。
            with self.audit_path.open('a',encoding='utf-8') as output:  # 用追加模式打开 UTF-8 日志，不覆盖以前记录。
                output.write(json.dumps(audit,ensure_ascii=False)+'\n')  # 写入一条 JSON 加换行，这就是 JSONL。若日志目录不可写，异常会继续传播，当前没有静默忽略日志错误的分支。

def run_sql(sql,*,policy=DEFAULT_POLICY):  # 提供无需手动管理 SQLTool 的便利函数。
    engine = readonly_engine(policy)  # 使用统一只读连接工厂创建引擎。
    try:  # 开始保证资源清理的 try/finally。
        return SQLTool(engine,policy=policy).run_sql(sql)  # 创建执行器并运行 SQL，直接返回结果对象。
    finally:  # 无论查询是否成功都做下面的清理。
        engine.dispose()  # 释放引擎连接池资源。

def main():  # 定义终端入口。
    parser = argparse.ArgumentParser(description=__doc__)  # 建立参数解析器。
    parser.add_argument('--sql',required=True)  # 要求提供 --sql 参数。
    args = parser.parse_args()  # 解析用户输入。
    try:  # 捕获统一查询错误。
        print(json.dumps(asdict(run_sql(args.sql)),ensure_ascii=False,indent=2))  # 运行 SQL，将 dataclass 转字典，再打印保留中文、缩进两格的 JSON。
        return 0  # 成功退出码为 0。
    except QueryError as error:  # 失败时处理 QueryError。
        print(json.dumps({'error':error.code,'message':str(error),'trace_id':error.trace_id},ensure_ascii=False,indent=2))  # 输出错误码、说明、追踪号的 JSON，便于定位日志。
        return 1  # 失败退出码为 1。

if __name__ == '__main__':  # 仅模块被作为入口执行时调用 main。
    raise SystemExit(main())  # 运行 main，并将返回值作为退出码。
