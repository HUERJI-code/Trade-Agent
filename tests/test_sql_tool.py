# 文件作用：前半部分用 Mock 测执行顺序与错误处理；标有 integration 的后半部分连接真实 MySQL，验证聚合、限制、日期、文本字面量和服务器超时。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
from decimal import Decimal  # 导入：Decimal 用于精确表示十进制金额，避免普通浮点误差。
import json  # 导入：json 用于在 Python 对象与 JSON 文本之间转换。
from unittest.mock import MagicMock  # 导入：MagicMock 用于模拟包括 with 上下文协议在内的数据库对象。

import pymysql  # 导入：pymysql 用于与 MySQL 通信，也提供驱动异常类型。
import pytest  # 导入：pytest 用于使用 fixture、参数化样例、异常断言与测试标记。
from sqlalchemy.exc import OperationalError  # 导入：OperationalError 用于构造测试中的连接或执行错误。

from security.permissions import QueryPolicy  # 导入：QueryPolicy 用于创建具有硬上限的查询策略。
from tools.sql_tool import QueryError, SQLTool  # 导入：QueryError 用于处理统一查询错误；SQLTool 用于使用受校验、受限制并记日志的 SQL 执行器。

def tool_with_schema(schema,tmp_path):  # 定义创建带模拟 schema 的 SQLTool 辅助函数。
    engine = MagicMock()  # 用 MagicMock 模拟 Engine，包括 with 所需的特殊方法。
    tool = SQLTool(engine,audit_path=tmp_path/'audit.jsonl')  # 日志写 pytest 提供的临时目录，避免污染日常日志。
    tool._schema = schema  # 预填 schema，让本组测试不触发元数据连接。
    return tool,engine  # 同时返回执行器和模拟引擎，供测试观察是否调用了连接。

def test_rejection_never_reaches_query_connection(schema,tmp_path):  # 验证被拒绝的 SQL 不进入业务查询连接。
    tool,engine = tool_with_schema(schema,tmp_path)  # 取得带缓存结构的假引擎工具。
    with pytest.raises(QueryError):  # 要求执行请求抛出统一 QueryError。
        tool.run_sql('DELETE FROM sales')  # 提交 DELETE 测试文本；应在校验阶段拒绝。
    engine.connect.assert_not_called()  # 证明 engine.connect 没被调用；此断言是在预填 schema 的前提下成立。
    audit = json.loads((tmp_path/'audit.jsonl').read_text())  # 读取临时审计日志的 JSON。
    assert audit['status']=='rejected' and 'DELETE' not in json.dumps(audit)  # 状态应是 rejected，日志不能包含原始 DELETE 文本。

def test_only_regenerated_sql_executes(schema,tmp_path):  # 验证真正发出的 SQL 是重新生成、限制后的 SQL。
    tool,engine = tool_with_schema(schema,tmp_path)  # 创建模拟执行器与引擎。
    connection = engine.connect.return_value.__enter__.return_value  # 取得模拟 with engine.connect() as connection 的返回对象。
    result = connection.execution_options.return_value.exec_driver_sql.return_value.__enter__.return_value  # 取得模拟流式执行结果的上下文对象。
    result.keys.return_value = ['value']  # 模拟结果列名为 value。
    result.fetchmany.return_value = [(Decimal('123.45'),)]  # 模拟数据库返回一个精确小数值。
    response = tool.run_sql('SELECT unit_price_usd AS value FROM products LIMIT 999999')  # 提交超大 LIMIT 查询，促使校验器将其缩小。
    sent = connection.execution_options.return_value.exec_driver_sql.call_args.args[0]  # 从 Mock 的调用记录中取出实际传给驱动的 SQL。
    assert sent.endswith('LIMIT 2000')  # 要求实际执行 SQL 已改为 LIMIT 2000。
    assert response.rows == [['123.45']]  # Decimal 必须序列化为字符串，保持精度。
    assert response.limit_applied  # 结果应明确标记上限被工具应用。

@pytest.mark.parametrize('mysql_code,expected',[(3024,'query_timeout'),(1142,'database_error')])  # 分别模拟服务器超时编号和权限错误编号，检查分类。
def test_database_errors_do_not_leak_raw_details(mysql_code,expected,schema,tmp_path):  # 定义异常脱敏测试。
    tool,engine = tool_with_schema(schema,tmp_path)  # 创建模拟执行器。
    connection = engine.connect.return_value.__enter__.return_value  # 取得模拟连接。
    connection.execution_options.return_value.exec_driver_sql.side_effect = OperationalError(  # 让真正执行 SQL 的模拟方法抛出 SQLAlchemy OperationalError。
        'secret SQL',{},pymysql.err.OperationalError(mysql_code,'password=private_value'))  # 模拟异常故意包含敏感 SQL 和假密码标记，观察是否泄漏；它们不是实际凭据。
    with pytest.raises(QueryError) as caught:  # 要求被转换为统一 QueryError。
        tool.run_sql('SELECT COUNT(*) FROM sales')  # 触发一条本应允许的查询，使流程抵达驱动错误分支。
    assert caught.value.code == expected  # 检查错误分类与编号对应。
    assert 'private_value' not in str(caught.value)  # 用户可见错误不能包含模拟敏感值。
    assert 'private_value' not in (tmp_path/'audit.jsonl').read_text()  # 审计日志也不能包含模拟敏感值。

@pytest.mark.integration  # 标记下面测试访问真实教学数据库。
def test_live_queries_schema_and_limits(tmp_path):  # 定义真实查询、结构和限制的综合验证。
    from database.connection import readonly_engine  # 导入：readonly_engine 用于创建带只读身份、权限和会话检查的数据库引擎。
    from tools.schema_tool import get_schema  # 导入：get_schema 用于读取并过滤真实数据库结构。
    engine = readonly_engine()  # 创建真实只读引擎。
    try:  # 保证最终清理。
        safe_schema = get_schema(engine)  # 读取真实且受限的结构。
        assert len(safe_schema['tables']['sales']['foreign_keys'])==4  # 真实销售表应有客户、产品、门店、汇率四条外键。
        tool = SQLTool(engine,audit_path=tmp_path/'live.jsonl')  # 创建真实执行器，日志仍写临时目录。
        count = tool.run_sql('SELECT COUNT(DISTINCT order_number) AS n FROM sales')  # 查询去重订单数。
        assert count.rows == [[26326]]  # 结果必须为此数据集的 26326 单。
        grouped = tool.run_sql('WITH x AS (SELECT customer_key, SUM(quantity) AS n FROM sales GROUP BY customer_key) SELECT c.country,SUM(x.n) AS n FROM x JOIN customers c ON c.customer_key=x.customer_key GROUP BY c.country ORDER BY n DESC')  # 通过 WITH 先按客户汇总数量，再 JOIN 客户按国家合计，测试多层查询。
        assert len(grouped.rows)==8  # 应返回 8 个国家。
        assert sum(int(row[1]) for row in grouped.rows)==197757  # 各国家数量相加应为 197757 件，关联未丢失或重复数量。
        limited = tool.run_sql('SELECT order_number,line_item FROM sales ORDER BY order_number,line_item LIMIT 999999')  # 提交很大的 LIMIT，并固定排序确保结果稳定。
        assert limited.row_count==2000 and limited.limit_applied and limited.limit_reached  # 实际只返回 2000 行，标明限制被修改且已触及上限。
        empty = tool.run_sql('SELECT order_number FROM sales LIMIT 0')  # 真实执行 LIMIT 0。
        assert empty.rows==[] and empty.row_limit==0  # 应返回空列表且保留 0 上限。
        monthly = tool.run_sql("SELECT DATE_FORMAT(order_date,'%Y-%m') AS month,SUM(quantity) AS units FROM sales GROUP BY month ORDER BY month")  # 按 YYYY-MM 格式聚合月份，检查真实驱动对百分号的处理。
        assert monthly.rows[0][0]=='2016-01' and monthly.rows[-1][0]=='2021-02'  # 第一月和最后月应为 2016-01 与 2021-02。
        assert sum(int(row[1]) for row in monthly.rows)==197757  # 月度数量相加也必须为 197757。
        from sqlglot import exp  # 导入：exp 用于使用 sqlglot 的各类语法树节点。
        for value in ["O'Brien \\ literal", "'; DROP TABLE sales; --"]:  # 轮流测试含引号、反斜杠，以及看似 SQL 命令的普通字符串。
            sql = f'SELECT {exp.Literal.string(value).sql(dialect="mysql")} AS value FROM stores LIMIT 1'  # 用 sqlglot 将测试文本编码为字面量，只从 stores 取一行用于承载值。
            assert tool.run_sql(sql).rows==[[value]]  # 返回值应完整等于原文本，证明特殊符号没有变成执行指令。
        with engine.connect() as connection:  # 直接打开受信任的内部诊断连接。
            assert connection.exec_driver_sql('SELECT @@session.max_execution_time, @@session.transaction_read_only').one()==(5000,1)  # 核对服务器会话超时 5000 毫秒与默认只读事务标志 1。
    finally:  # 保证真实资源清理。
        engine.dispose()  # 释放引擎。

@pytest.mark.integration  # 标记服务器超时测试需要真实数据库。
def test_live_server_timeout(tmp_path):  # 定义实际服务器中断测试。
    from database.connection import readonly_engine  # 导入：readonly_engine 用于创建带只读身份、权限和会话检查的数据库引擎。
    policy = QueryPolicy(timeout_ms=20)  # 将本次超时缩短为 20 毫秒，避免高成本查询运行太久。
    engine = readonly_engine(policy)  # 用对应策略创建只读引擎。
    try:  # 保证最终释放连接池。
        tool = SQLTool(engine,policy=policy,audit_path=tmp_path/'timeout.jsonl')  # 创建执行器并使用同一超时策略。
        with pytest.raises(QueryError) as caught:  # 期望发生 QueryError。
            tool.run_sql('SELECT SUM(a.quantity*b.quantity*c.quantity) FROM sales a JOIN sales b ON a.product_key=b.product_key JOIN sales c ON b.product_key=c.product_key')  # 三次关联 sales 并聚合，故意制造足够计算量触发服务器超时；只读且有时间限制。
        assert caught.value.code=='query_timeout'  # 错误应被归类为 query_timeout。
        # 注释：中断后还需验证连接池可继续使用。
        assert tool.run_sql('SELECT COUNT(*) FROM stores').rows==[[67]]  # 再查询门店总数应得到 67，证明后续正常查询可恢复。
    finally:  # 不论断言成功失败都清理。
        engine.dispose()  # 释放引擎。
