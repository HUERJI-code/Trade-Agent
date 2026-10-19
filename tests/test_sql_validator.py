# 文件作用：SQL 校验器离线测试。列表中的危险 SQL 只是字符串，测试只将它们交给解析器，不发送给数据库。parametrize 将每条样例分别运行，所以测试数多于函数数。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
import pytest  # 导入：pytest 用于使用 fixture、参数化样例、异常断言与测试标记。

from security.permissions import QueryPolicy  # 导入：QueryPolicy 用于创建具有硬上限的查询策略。
from security.sql_validator import SQLValidationError, validate_sql  # 导入：SQLValidationError 用于处理 SQL 规则校验失败；validate_sql 用于调用 SQL 白名单与资源限制校验。

@pytest.mark.parametrize('sql',[  # 将下列每条 SQL 分别作为 sql 参数运行同一个允许查询测试。
    'SELECT COUNT(*) AS n FROM sales',  # 允许对销售明细 COUNT(*) 聚合。
    'SELECT COUNT(DISTINCT order_number) FROM sales',  # 允许对订单号 COUNT DISTINCT。
    'SELECT country, COUNT(*) AS n FROM customers GROUP BY country ORDER BY n DESC',  # 允许按国家分组、计数并按别名排序。
    'SELECT s.product_key, SUM(s.quantity) AS n FROM sales s JOIN products p ON p.product_key=s.product_key GROUP BY s.product_key HAVING n>10',  # 允许有 ON 条件的关联、按产品分组及 HAVING 聚合后筛选。
    'WITH totals AS (SELECT customer_key,SUM(quantity) AS n FROM sales GROUP BY customer_key) SELECT customer_key,n FROM totals ORDER BY n DESC',  # 允许非递归 WITH 临时结果，再在外层选择和排序。
    'SELECT a.country FROM (SELECT country FROM customers) a LIMIT 10',  # 允许 FROM 中的子查询，以及较小 LIMIT。
    'SELECT country FROM customers UNION ALL SELECT country FROM stores',  # 允许 UNION ALL 合并客户国家和门店国家。
    "SELECT DATE_FORMAT(order_date,'%Y-%m') AS month, SUM(quantity) FROM sales GROUP BY month",  # 允许 DATE_FORMAT 月度聚合，验证百分号不会被解析器误处理。
    'SELECT SUM(quantity) / NULLIF(COUNT(*),0) AS avg_qty FROM sales',  # 允许聚合除以 NULLIF 保护的分母。
    'SELECT CASE WHEN quantity>2 THEN 1 ELSE 0 END AS large_order FROM sales',  # 允许 CASE WHEN 条件表达式。
    "SELECT country FROM customers WHERE country IN ('Germany','United States')",  # 允许使用字符串列表 IN 筛选国家。
    'SELECT country FROM customers WHERE customer_key IN (SELECT customer_key FROM sales)',  # 允许 IN 内部的业务表子查询。
    'SELECT c.country FROM customers c WHERE EXISTS (SELECT 1 FROM sales s WHERE s.customer_key=c.customer_key)',  # 允许引用外层客户别名的 EXISTS 相关子查询。
    'SELECT COUNT(*) FROM trade_agent_demo.sales',  # 允许显式指定教学库名称。
    "SELECT country FROM customers WHERE country='DROP TABLE; is just text'",  # 字面量中的 DROP TABLE 和分号只是数据，不能因为关键词出现就误拒绝。
    'SELECT CAST(unit_price_usd AS DECIMAL(12,2)) FROM products LIMIT 0',  # 允许受限 DECIMAL 转换及 LIMIT 0。
    'SELECT order_number FROM sales WHERE quantity > 1 AND (quantity < 10 OR quantity = 20)',  # 回归测试：允许 AND/OR 组合；它们在当前 sqlglot 中也被归类为 Func。
])  # 结束允许 SQL 样例列表与参数化装饰器。
def test_allowed_queries(sql,schema):  # 定义允许查询测试，schema 由 conftest 自动注入。
    checked = validate_sql(sql,schema)  # 执行真实校验函数，不连接数据库。
    assert checked.sql and checked.row_limit <= 2000  # 要求返回规范 SQL，且最终行数上限不超过 2000。

@pytest.mark.parametrize('sql',[  # 将下面每条禁止 SQL 分别用于拒绝测试。
    '', None, 'UPDATE sales SET quantity=1', 'DELETE FROM sales',  # 空串、None、UPDATE、DELETE 都应被拒绝。
    'INSERT INTO sales SELECT * FROM sales', 'DROP TABLE sales', 'ALTER TABLE sales ADD x INT',  # INSERT、DROP、ALTER 都不是本工具允许的只读分析。
    'TRUNCATE TABLE sales','CREATE TABLE test (id INT)','CALL sys.version()',  # 拒绝 TRUNCATE、CREATE 和存储过程 CALL。
    'SET SESSION max_execution_time=0', 'SHOW TABLES','EXPLAIN SELECT COUNT(*) FROM sales',  # 拒绝改会话设置、SHOW 和 EXPLAIN；固定内部诊断查询不通过此用户接口。
    'SELECT COUNT(*) FROM sales; DROP TABLE sales', 'SELECT 1; SELECT 2',  # 拒绝一段输入携带多条实际 SQL。
    'WITH x AS (SELECT order_number FROM sales) DELETE FROM sales',  # 拒绝用 WITH 前缀包装 DELETE。
    'SELECT * FROM sales','SELECT s.* FROM sales s','SELECT a.order_number FROM (SELECT * FROM sales) a',  # 拒绝顶层、带表别名及子查询里的 SELECT *。
    'SELECT customer_name FROM customers','SELECT birthday FROM customers',  # 拒绝直接读取客户姓名和生日。
    'SELECT COUNT(customer_name) FROM customers','SELECT country FROM customers WHERE birthday IS NOT NULL',  # 受限列即使用于聚合或 WHERE，也必须被拒绝。
    'SELECT country FROM customers ORDER BY customer_name',  # ORDER BY 中引用客户姓名也被拒绝。
    'SELECT x.n FROM (SELECT customer_name AS n FROM customers) x',  # 不能通过子查询给姓名起别名绕过限制。
    'WITH x AS (SELECT birthday AS d FROM customers) SELECT d FROM x',  # 不能通过 CTE 暴露生日。
    'SELECT s.unknown_column FROM sales s','SELECT nonexistent FROM sales',  # 拒绝虚构的列名。
    'SELECT customer_key FROM sales s JOIN customers c ON s.customer_key=c.customer_key',  # 两张表都有 customer_key 而未限定归属时拒绝，避免歧义。
    'SELECT User FROM mysql.user','SELECT table_name FROM information_schema.tables',  # 拒绝 mysql.user 和 information_schema 等系统库。
    'SELECT country FROM otherdb.customers','SELECT value FROM secret_table',  # 拒绝其他数据库和未知表。
    'WITH x AS (SELECT User FROM mysql.user) SELECT User FROM x',  # 系统表藏在 WITH 中也应被识别并拒绝。
    "SELECT LOAD_FILE('/etc/passwd') FROM sales LIMIT 1",  # 拒绝 LOAD_FILE 文件读取函数。
    'SELECT SLEEP(1) FROM sales','SELECT BENCHMARK(1000000,1+1) FROM sales',  # 拒绝 SLEEP 和 BENCHMARK 等消耗资源的函数。
    "SELECT GET_LOCK('lock',1) FROM sales", 'SELECT @@version FROM sales','SELECT @x:=quantity FROM sales',  # 拒绝获取锁、读取系统变量、给用户变量赋值。
    'SELECT @x FROM sales', 'SELECT CURRENT_USER() FROM sales',  # 拒绝用户变量和读取当前账号函数。
    "SELECT order_number INTO OUTFILE '/tmp/example' FROM sales",  # 拒绝 SELECT INTO OUTFILE 导出服务器文件。
    'SELECT order_number FROM sales FOR UPDATE','SELECT order_number FROM sales LOCK IN SHARE MODE',  # 拒绝 SELECT FOR UPDATE 和共享锁查询。
    'SELECT /*+ MAX_EXECUTION_TIME(0) */ COUNT(*) FROM sales',  # 拒绝通过优化器 hint 关闭执行时间限制。
    'SELECT COUNT(*) FROM sales -- comment', 'SELECT COUNT(*) FROM sales # comment',  # 拒绝双短横线和井号 SQL 注释。
    'SELECT COUNT(*) FROM sales /*!50000 FOR UPDATE */',  # 拒绝 MySQL 可执行版本注释。
    'WITH RECURSIVE x AS (SELECT order_number FROM sales) SELECT order_number FROM x',  # 拒绝递归 WITH。
    'SELECT COUNT(*) FROM sales CROSS JOIN customers',  # 拒绝显式 CROSS JOIN。
    'SELECT COUNT(*) FROM sales, customers','SELECT COUNT(*) FROM sales NATURAL JOIN customers',  # 拒绝逗号隐式交叉连接和 NATURAL JOIN。
    'SELECT order_number FROM sales LIMIT -1','SELECT order_number FROM sales LIMIT ? ',  # LIMIT 不能为负数或占位符。
    'SELECT order_number FROM sales LIMIT 10 OFFSET 10001','SELECT 1',  # 拒绝过大 OFFSET，也拒绝不读取任何业务表的 SELECT 1。
    'SELECT CAST(country AS CHAR(1000000000)) FROM customers',  # 拒绝极大的 CAST 字符长度。
    'WITH customers AS (SELECT birthday FROM trade_agent_demo.customers) SELECT birthday FROM customers',  # 即使把 CTE 命名为 customers，也不能遮蔽实际受限生日列的检查。
])  # 结束禁止样例列表。
def test_rejected_queries(sql,schema):  # 定义拒绝测试。
    with pytest.raises(SQLValidationError):  # 要求抛出 SQLValidationError；未抛出则测试失败。
        validate_sql(sql,schema)  # 将测试字符串交给校验器，绝不执行它。

@pytest.mark.parametrize('suffix,expected,applied',[  # 为 LIMIT 测试分别传入 SQL 后缀、预期上限、是否被工具修改。
    ('',2000,True),(' LIMIT 999999',2000,True),(' LIMIT 7',7,False),  # 无 LIMIT 默认 2000；超大 LIMIT 缩到 2000；LIMIT 7 原样保留。
    (' LIMIT 0',0,False),(' LIMIT 2,7',7,False),(' LIMIT 7 OFFSET 2',7,False)])  # LIMIT 0 保留；同时验证 MySQL 两种偏移量语法。
def test_row_limits(suffix,expected,applied,schema):  # 定义行数限制测试。
    result = validate_sql('SELECT order_number FROM sales'+suffix,schema)  # 构造普通查询加后缀，取得校验结果。
    assert (result.row_limit,result.limit_applied)==(expected,applied)  # 同时检查最终上限和 limit_applied 标记。

def test_union_gets_outer_limit(schema):  # 验证 UNION 限制加在整个组合查询外层。
    checked = validate_sql('SELECT country FROM customers UNION ALL SELECT country FROM stores',schema,QueryPolicy(max_rows=11))  # 使用更严格的 11 行策略验证合并查询。
    assert checked.sql.endswith('LIMIT 11')  # 要求生成 SQL 最后以 LIMIT 11 结束。

def test_snapshot_cannot_expand_permissions(schema):  # 验证调用方伪造结构快照不能扩大字段权限。
    schema['tables']['customers']['columns'].append({'name':'customer_name','type':'VARCHAR(255)'})  # 故意把 customer_name 加进测试 schema。
    with pytest.raises(SQLValidationError):  # 即使 schema 声称有这个列，也期望拒绝。
        validate_sql('SELECT customer_name FROM customers',schema)  # 验证固定代码白名单仍然有效。

def test_literals_redacted_in_audit(schema):  # 验证查询字面量不会出现在日志模板中。
    query = validate_sql("SELECT country FROM customers WHERE country='secret literal 123'",schema)  # 使用一段可辨认的筛选字符串作为测试值。
    assert 'secret literal' not in query.audit_template  # 要求脱敏模板不包含该值。
    assert 'secret literal' in query.sql  # 实际执行 SQL 则应保留该筛选值，否则业务含义会改变。

def test_aliases_redacted_in_audit(schema):  # 验证用户自定义别名也会从日志模板中去除。
    query = validate_sql('SELECT country AS private_alias FROM customers',schema)  # 生成含 private_alias 的允许查询。
    assert 'private_alias' not in query.audit_template  # 模板中不应保留该别名。
    assert 'private_alias' in query.sql  # 真正执行 SQL 仍应保留结果别名。

def test_policy_cannot_raise_hard_ceiling():  # 验证自定义策略不能突破硬上限。
    with pytest.raises(ValueError):  # 期望超限配置抛出 ValueError。
        QueryPolicy(max_rows=2001)  # 尝试 2001 行，必须在策略初始化时被拒绝。
