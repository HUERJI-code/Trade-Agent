# 文件作用：阶段 1：SQL 的准入检查。把 SQL 解析成语法树，检查语句、函数、表、列、资源限制，最后重新生成允许执行的 SQL。此文件不连接数据库。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Fail-closed MySQL SELECT validation. Only the regenerated AST SQL may execute."""  # 说明采用默认拒绝策略，只允许重新生成的语法树 SQL 被执行。
from dataclasses import dataclass  # 导入：dataclass 用于自动生成数据容器的初始化等方法。

import sqlglot  # 导入：sqlglot 用于解析、检查和重新生成 SQL 语法树。
import re  # 仅回传受限格式的别名，帮助模型修正漏写 JOIN，不回显完整 SQL。
from sqlglot import exp  # 导入：exp 用于使用 sqlglot 的各类语法树节点。
from sqlglot.errors import ErrorLevel, SqlglotError  # 导入：ErrorLevel 用于指定 SQL 解析/生成遇到问题时的严格程度；SqlglotError 用于捕获 SQL 解析器异常。
from sqlglot.optimizer.qualify import qualify  # 导入：qualify 用于确定字段属于哪张表，并校验未知、歧义或受限列。
from sqlglot.optimizer.scope import traverse_scope  # 导入：traverse_scope 用于区分实体表、CTE 和子查询的作用域。
from sqlglot.tokens import TokenType  # 导入：TokenType 用于辨别 SELECT/WITH 等 SQL 词法类型。

from security.permissions import ALLOWED_COLUMNS, DATABASE, DEFAULT_POLICY  # 导入：ALLOWED_COLUMNS 用于取得固定表/列白名单；DATABASE 用于取得固定教学数据库名；DEFAULT_POLICY 用于取得统一的默认查询资源限制。

class SQLValidationError(ValueError):  # 定义专用校验异常，继承 ValueError，便于与执行错误区分。
    def __init__(self, code, message):  # 初始化时传入稳定错误码和可展示说明。
        self.code = code  # 保存 code，供调用方分辨是越权、语法错误还是复杂度超限。
        super().__init__(message)  # 把说明传给父异常类，使 str(error) 返回该说明。

@dataclass(frozen=True)  # 定义不可变结果对象的装饰器。
class ValidatedQuery:  # 校验成功后的返回类型。
    sql: str  # sql 保存重新生成并加限制后的 SQL 文本。
    row_limit: int  # row_limit 保存最终行数上限。
    limit_applied: bool  # limit_applied 表示工具是否补上或缩小了用户的 LIMIT。
    audit_template: str  # audit_template 保存删除具体字面量和自定义别名的日志模板。

# 下方多行字符串第 1 行：允许的 AST 节点名称集合开始：SELECT、UNION、子查询、CTE、标识符和表节点。
# 下方多行字符串第 2 行：继续允许列、FROM、JOIN、筛选、分组、排序、限制及常量节点。
# 下方多行字符串第 3 行：继续允许括号、去重、通配符节点以及四则运算、比较和布尔连接；星号稍后还会额外限制。
# 下方多行字符串第 4 行：继续列出 IN、LIKE、区间、类型等结构；split 按空白分词，frozenset 创建不可变集合。
STRUCTURES = frozenset('''select union subquery with cte alias identifier table tablealias
column from join where group having order ordered limit offset literal null boolean
paren distinct star add sub mul div intdiv mod neg eq neq gt gte lt lte and or not
is between in like escape tuple interval var datatype datatypeparam window windowspec'''.split())
# 下方多行字符串第 1 行：允许的函数名称集合，包含常见计数、求和、均值、极值和空值处理。
# 下方多行字符串第 2 行：继续允许条件表达式、类型转换和日期处理；这里使用 sqlglot 的内部标准函数名。
# 下方多行字符串第 3 行：继续允许日期加减、差值和当前时间函数。
# 下方多行字符串第 4 行：继续允许有限的字符串处理和 EXISTS 等函数，然后结束集合。
FUNCTIONS = frozenset('''COUNT SUM AVG MIN MAX COALESCE NULLIF ROUND ABS FLOOR CEIL
CASE IF CAST DATE YEAR MONTH DAY DAY_OF_MONTH EXTRACT TIME_TO_STR STR_TO_DATE
DATE_ADD DATE_SUB DATE_DIFF TIMESTAMP_DIFF CURRENT_DATE CURRENT_TIMESTAMP
LOWER UPPER TRIM LENGTH CONCAT SUBSTRING EXISTS TS_OR_DS_TO_TIMESTAMP TS_OR_DS_TO_DATE
ROW_NUMBER RANK DENSE_RANK LAG LEAD FIRST_VALUE LAST_VALUE NTILE'''.split())
UNITS = frozenset('YEAR QUARTER MONTH WEEK DAY HOUR MINUTE SECOND'.split())  # 允许作为日期运算单位的固定词，避免任意变量通过。

def reject(code, message):  # 定义统一拒绝函数。
    raise SQLValidationError(code,message)  # 抛出带 code 的 SQLValidationError。

def integer(node, code):  # 解析 LIMIT/OFFSET 等需要整数常量的位置；code 由调用处指定。
    if not isinstance(node,exp.Literal) or node.is_string or not node.this.isdigit():  # 要求 AST 是非字符串数字字面量，文本只含十进制数字；排除负数、占位符和计算式。
        reject(code,'LIMIT and OFFSET must be nonnegative integer constants.')  # 不符合要求就拒绝，说明限制必须为非负整数常量。
    return int(node.this)  # 返回真正的 Python 整数。

def validate_sql(sql, schema, policy=DEFAULT_POLICY):  # 定义 SQL 校验入口；schema 是结构快照，policy 是受限查询策略。
    if not isinstance(sql,str) or not sql.strip() or len(sql) > policy.max_sql_chars:  # 要求输入为非空字符串，且字符数未超过上限。
        reject('invalid_input','SQL is empty or exceeds the size limit.')  # 无效输入立即拒绝。
    try:  # 开始捕获解析、遍历和列解析错误。
        tokens = sqlglot.Dialect.get_or_raise('mysql').tokenize(sql)  # 按 MySQL 方言将文本分解为 token；这比搜索 SELECT 字符串更准确。
        if not tokens or tokens[0].token_type not in (TokenType.SELECT,TokenType.WITH):  # 第一个 token 必须为 SELECT 或 WITH。
            reject('readonly_only','Only SELECT and nonrecursive read-only WITH queries are allowed.')  # 否则拒绝写入、授权、控制等语句。
        if any(token.comments for token in tokens):  # 扫描所有 token 的注释信息。
            reject('comments_forbidden','SQL comments and optimizer hints are not accepted.')  # 拒绝 SQL 注释和优化器 hint，防止夹带修改执行行为的指令。
        statements = [q for q in sqlglot.parse(sql,read='mysql',error_level=ErrorLevel.RAISE) if q is not None]  # 严格解析为语法树列表，忽略解析器返回的空项。
        if len(statements) != 1:  # 只允许一条实际 SQL 语句。
            reject('multiple_statements','Submit exactly one SQL query.')  # 多语句拒绝，防止 SELECT 后面追加其他命令。
        query = statements[0]  # 取唯一的查询语法树。
        if not isinstance(query,(exp.Select,exp.Union)):  # 顶层结构必须是 SELECT 或 UNION；WITH 本身作为查询附属结构处理。
            reject('readonly_only','Only SELECT and nonrecursive read-only WITH queries are allowed.')  # 外表看似 WITH 但最终是写语句的请求也拒绝。
        nodes = list(query.walk())  # 遍历整棵 AST 并收集节点，包括子查询内部。
        if len(nodes) > policy.max_ast_nodes:  # 节点数量超过限制就认为过于复杂。
            reject('query_too_complex','The query exceeds the supported complexity.')  # 返回复杂度错误。
        for node in nodes:  # 逐个检查 AST 节点。
            if node.comments:  # 再次检查节点上保存的注释。
                reject('comments_forbidden','SQL comments and optimizer hints are not accepted.')  # 任何位置的注释都会被拒绝。
            if isinstance(node,exp.Func):  # 当节点被 sqlglot 识别为函数类型，执行函数白名单检查。
                if isinstance(node,exp.Anonymous) or (node.key not in STRUCTURES and node.sql_name() not in FUNCTIONS):  # 拒绝未识别的 Anonymous 函数；其他函数若既不是允许结构、也不在函数集合中同样拒绝。AND/OR 在此版本也属于 Func，需允许其结构名称。
                    reject('unsupported_function','This function is not in the approved analytics function list.')  # 未知或危险函数不放行，例如 SLEEP。
            elif node.key not in STRUCTURES:  # 非函数节点也必须在 STRUCTURES 中。
                reject('unsupported_syntax','This SQL feature is not supported by the read-only tool.')  # 未实现支持的语法默认拒绝，而不是默认执行。
            if isinstance(node,exp.Star) and not isinstance(node.parent,exp.Count):  # 星号只有直接作为 COUNT 的子节点才允许。
                reject('wildcard_forbidden','List output columns explicitly; only COUNT(*) accepts a wildcard.')  # 拒绝 SELECT * 等无差别返回所有字段的方式，要求显式列名。
            if isinstance(node,exp.With) and node.args.get('recursive'):  # 检测 WITH RECURSIVE。
                reject('recursive_cte','Recursive CTEs are not supported.')  # 拒绝递归 CTE，避免无限或高成本递归查询。
            if isinstance(node,exp.Join):  # 对每个 JOIN 施加额外要求。
                if node.args.get('method') or node.args.get('kind') == 'CROSS' or not (node.args.get('on') or node.args.get('using')):  # 拒绝自然连接方式、CROSS JOIN，以及没有 ON/USING 的连接；ON 1=1 仍可能很贵，所以还需要超时限制。
                    reject('join_condition_required','JOIN requires an explicit ON or USING condition; no cross/natural joins.')  # 给出需要明确关联条件的错误。
            if isinstance(node,exp.Var) and node.name.upper() not in UNITS:  # 变量节点只接受固定日期单位。
                reject('unsupported_syntax','This expression is not supported.')  # 拒绝其他变量表达式。
            if isinstance(node,exp.Limit):  # 发现 LIMIT 节点就检查其中的值。
                integer(node.expression,'invalid_limit')  # 验证其是非负整数常量，子查询的 LIMIT 也经过这里。
            if isinstance(node,exp.Offset) and integer(node.expression,'invalid_offset') > policy.max_offset:  # 验证 OFFSET 的整数格式，并检查是否超过上限。
                reject('offset_too_large','OFFSET exceeds the supported range.')  # 偏移量过大时拒绝。
            if isinstance(node,exp.DataTypeParam):  # 对 CAST 类型参数，例如 CHAR(长度)，继续检查。
                if integer(node.this,'invalid_cast_size') > 512:  # 要求类型尺寸为受限整数，最大 512。
                    reject('invalid_cast_size','CAST type sizes greater than 512 are not supported.')  # 拒绝巨大的类型尺寸，减少资源滥用风险。
            if isinstance(node,exp.Table):  # 检查每个表节点的结构。
                if not isinstance(node.this,exp.Identifier) or node.catalog or (node.db and node.db != DATABASE):  # 表名必须为标识符，不允许 catalog 前缀；显式库名只能是教学库。
                    reject('table_forbidden','Only approved tables in the teaching database are accessible.')  # 跨库或异常表表达式拒绝。
        physical = []  # 准备记录真正访问的实体表；CTE 名称不直接算实体表。
        for scope in traverse_scope(query):  # 按查询作用域分析，区分表、子查询和 CTE 别名。
            visible = set(scope.sources)  # 本作用域及外层的别名，支持相关子查询。
            ancestor = scope.parent
            while ancestor is not None:
                visible.update(ancestor.sources)
                ancestor = ancestor.parent
            for column in scope.columns:
                if column.table and column.table not in visible:
                    alias = column.table if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,31}', column.table) else '(未声明别名)'
                    reject('invalid_sql_or_column', f'Table alias {alias} is not declared in FROM/JOIN; add its table and join condition or correct the alias.')
            for _, source in scope.selected_sources.values():  # 遍历当前作用域实际选用的数据源。
                if isinstance(source,exp.Table):  # 仅实体 Table 节点进入表白名单检查。
                    if source.name not in ALLOWED_COLUMNS:  # 实体表名必须存在于 ALLOWED_COLUMNS。
                        reject('table_forbidden','The query references a table outside the approved business schema.')  # 禁止读系统表、未知表和其他不允许的业务表。
                    physical.append(source.name)  # 记录一次实体表引用；同表自连接会记录多次。
        if not physical:  # 整条查询至少需要读一张允许的业务表。
            reject('business_table_required','A query must read at least one approved business table.')  # 拒绝 SELECT 1 等纯常量查询；本工具定位为业务数据查询。
        if len(physical) > policy.max_table_references:  # 检查实体表引用总次数。
            reject('query_too_complex','Too many table references.')  # 超过策略上限则拒绝。
        # 注释：调用者传入的 schema 不能突破写死在代码里的列权限。
        mapping = {DATABASE:{}}  # 按数据库名组织 sqlglot 所需的字段类型映射。
        for table, meta in schema['tables'].items():  # 遍历提供的 schema 中的表。
            if table in ALLOWED_COLUMNS:  # 只有允许的表才加入映射。
                mapping[DATABASE][table] = {c['name']:c['type'] for c in meta['columns'] if c['name'] in ALLOWED_COLUMNS[table]}  # 对列再次与白名单取交集，阻止伪造 schema 加入客户姓名等字段。
        query = qualify(query,dialect='mysql',db=DATABASE,schema=mapping,infer_schema=False,  # qualify 解析列归属、补齐库/表信息；infer_schema=False 禁止猜测不存在的字段。
                        expand_stars=False,validate_qualify_columns=True,allow_partial_qualification=False)  # 不自动扩展星号，强制检查列是否合法、是否歧义，不接受未完全确认的列引用。
        limit = query.args.get('limit')  # 取得最外层查询的 LIMIT。
        requested = integer(limit.expression,'invalid_limit') if limit else None  # 有 LIMIT 就解析请求值，没有则设为 None；0 不会被当成没有 LIMIT。
        row_limit = min(requested,policy.max_rows) if requested is not None else policy.max_rows  # 用户请求与策略上限取较小值；未指定则使用默认上限。
        applied = requested is None or requested > policy.max_rows  # 记录是否补充或缩小了限制，用户原本较小的限制不会标记为被修改。
        query = query.limit(row_limit,copy=False)  # 在最外层 AST 设置最终 LIMIT；copy=False 表示直接更新这棵树。
        canonical = query.sql(dialect='mysql',comments=False,unsupported_level=ErrorLevel.RAISE)  # 按 MySQL 方言重新生成规范 SQL；无法可靠生成的特性要求抛错。
        audit = query.copy().transform(lambda n: exp.Placeholder() if isinstance(n,exp.Literal) else n)  # 复制语法树，把其中的字符串和数字字面量换成占位符，避免日志保存筛选值。
        safe_names = {DATABASE,*ALLOWED_COLUMNS} | {c for names in ALLOWED_COLUMNS.values() for c in names}  # 准备允许保留在日志中的库名、表名和列名集合。
        for identifier in audit.find_all(exp.Identifier):  # 检查日志副本中的所有标识符。
            if identifier.name not in safe_names:  # 不属于固定业务名称的标识符，例如自定义别名，进入脱敏分支。
                identifier.set('this','_alias')  # 将它改为统一的 _alias；这只改日志副本，不改实际执行 SQL。
        return ValidatedQuery(canonical,row_limit,applied,audit.sql(dialect='mysql',comments=False))  # 返回可执行规范 SQL、限制元数据，以及脱敏日志模板。
    except SQLValidationError:  # 捕获本模块主动产生的校验异常。
        raise  # 原样继续抛出，保留其准确错误码。
    except (SqlglotError,RecursionError,ValueError,TypeError,KeyError):  # 捕获解析器、递归和结构访问等异常。
        # 注释：解析器原始错误可能带完整用户 SQL，不应直接回传。
        reject('invalid_sql_or_column','SQL could not be validated, or references an unknown, ambiguous or restricted column.')  # 统一返回安全说明，表示语法、未知列、歧义列或限制列校验未通过。
