# 文件作用：阶段 2：加载已经确认的指标字典，校验指标，组合时间/国家/分组条件，调用安全 SQL 工具。它是确定性的业务查询程序，目前没有大模型或中文问题理解。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Approved metric definitions and deterministic calculations; no natural-language model."""  # 模块说明：使用已确认指标进行确定性计算，尚未接入自然语言模型。
import argparse  # 导入：argparse 用于解析终端中的 --参数。
from copy import deepcopy  # 导入：deepcopy 用于复制嵌套配置，避免测试或调用者误改原对象。
from datetime import date, timedelta  # 导入：date 用于表示日期并校验 ISO 日期；timedelta 用于做日期增减，例如将最晚日加一天。
import json  # 导入：json 用于在 Python 对象与 JSON 文本之间转换。
from pathlib import Path  # 导入：Path 用于以对象方式拼接、读取和写入文件路径。
import re  # 导入：re 用于用正则检查指标名称与日期文本格式。

import sqlglot  # 导入：sqlglot 用于解析、检查和重新生成 SQL 语法树。
from sqlglot import exp  # 导入：exp 用于使用 sqlglot 的各类语法树节点。
import yaml  # 导入：yaml 用于安全解析业务 YAML 配置。
from sqlalchemy.exc import SQLAlchemyError  # 导入：SQLAlchemyError 用于捕获数据库引擎和连接等异常。

from database.connection import ROOT, readonly_engine  # 导入：ROOT 用于取得项目根目录常量；readonly_engine 用于创建带只读身份、权限和会话检查的数据库引擎。
from security.sql_validator import SQLValidationError, validate_sql  # 导入：SQLValidationError 用于处理 SQL 规则校验失败；validate_sql 用于调用 SQL 白名单与资源限制校验。
from tools.schema_tool import get_schema  # 导入：get_schema 用于读取并过滤真实数据库结构。
from tools.sql_tool import SQLTool, QueryError  # 导入：SQLTool 用于使用受校验、受限制并记日志的 SQL 执行器；QueryError 用于处理统一查询错误。

DEFAULT_PATH = ROOT/'config'/'business_metrics.yaml'  # 默认加载项目 config/business_metrics.yaml，而不是 draft 模板。

class BusinessError(ValueError):  # 定义业务层异常，区分“指标不存在”“缺时间”等业务问题。
    def __init__(self,code,message):  # 构造异常时传入错误码与说明。
        self.code = code  # 保存供调用者识别的错误码。
        super().__init__(message)  # 将说明保存到异常父类。

class UniqueSafeLoader(yaml.SafeLoader):  # 继承 PyYAML SafeLoader，创建本项目专用加载器；SafeLoader 不执行任意 Python 对象构造。
    pass  # 类体暂不新增方法，pass 是合法占位；后面的 add_constructor 会注册自定义字典解析逻辑。

def unique_mapping(loader,node,deep=False):  # 定义 YAML 映射解析方法，deep 控制子对象构造时的深度处理。
    result = {}  # 准备保存解析后的键值对。
    for key_node,value_node in node.value:  # 逐一取 YAML 原始节点中的键节点和值节点。
        key = loader.construct_object(key_node,deep=deep)  # 将键节点构造成 Python 对象。
        if not isinstance(key,str) or key in result:  # 键必须为字符串且不能重复，避免重复配置静默覆盖前面的值。
            raise BusinessError('invalid_dictionary','YAML keys must be unique strings.')  # 发现非字符串键或重复键时，报告字典无效。
        result[key] = loader.construct_object(value_node,deep=deep)  # 构造值并存入结果字典。
    return result  # 返回本层映射。

UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,unique_mapping)  # 让此加载器在解析 YAML 普通映射时调用 unique_mapping；不修改全局 SafeLoader。

def fail(message):  # 定义配置失败的快捷函数。
    raise BusinessError('invalid_dictionary',message)  # 统一抛出 invalid_dictionary 错误。

def parse_expression(value):  # 定义单个指标或维度 SQL 表达式的解析器。
    if not isinstance(value,str) or len(value)>4000:  # 要求公式是字符串且不超过 4000 字符。
        fail('A metric or dimension must be a bounded SQL expression.')  # 超限或类型错误时拒绝。
    queries = sqlglot.parse('SELECT '+value,read='mysql')  # 在表达式前临时加 SELECT，借助 sqlglot 按 MySQL 语法解析。
    if len(queries)!=1 or not isinstance(queries[0],exp.Select):  # 必须只得到一个 SELECT 查询。
        fail('Expected a single expression, not a SQL statement.')  # 拒绝包含多条语句或其他类型语句的配置。
    query = queries[0]  # 取出刚才解析出的唯一查询。
    if len(query.expressions)!=1 or any(v for k,v in query.args.items() if k!='expressions'):  # SELECT 必须只含一个投影表达式，而且不能附带 FROM、WHERE 等其他查询子句。
        fail('Expected a single expression without query clauses.')  # 否则拒绝，配置应该提供公式，不应夹带完整 SQL。
    expression = query.expressions[0]  # 取出这个独立表达式的 AST。
    if isinstance(expression,exp.Alias) or list(expression.find_all(exp.Select)):  # 拒绝公式自己加 AS 别名，也拒绝公式内部嵌套 SELECT。
        fail('Expression aliases and subqueries are not supported.')  # 报告表达式结构不支持。
    return expression  # 返回解析后的表达式树，供进一步检查聚合形式。

class BusinessDictionary:  # 定义 BusinessDictionary 类，将指标配置、校验和查询生成集中起来。
    def __init__(self,data,schema,*,allow_draft=False):  # 构造函数接收配置、数据库结构；星号后的 allow_draft 只能按名字传入。
        self._data,self._schema = deepcopy(data),schema  # 深拷贝配置，避免调用方随后修改原对象影响内部规则；schema 保存引用。
        self._expressions = {}  # 准备已解析公式的缓存。
        self._validate(allow_draft)  # 对象创建时立即验证，防止无效配置被留到执行时才发现。

    @classmethod  # classmethod 使 load 可以通过类调用，cls 指当前类。
    def load(cls,schema,path=DEFAULT_PATH,*,allow_draft=False):  # 定义从文件加载的入口，默认用正式字典，默认不允许草稿。
        try:  # 开始捕获文件读取与 YAML 解析错误。
            path = Path(path)  # 将输入路径规范成 Path 对象。
            if path.stat().st_size > 65536:  # 字典文件大小上限 65536 字节，即 64 KiB。
                fail('Metric dictionary exceeds the size limit.')  # 超过大小上限就拒绝读取解析。
            source = path.read_text(encoding='utf-8')  # 以 UTF-8 读取文件文本。
            if any(isinstance(t,(yaml.tokens.AliasToken,yaml.tokens.AnchorToken)) for t in yaml.scan(source)):  # 先扫描 YAML token；检查是否使用别名或锚点。
                fail('YAML anchors and aliases are not supported.')  # 拒绝锚点/别名，避免复杂引用和膨胀结构；普通 YAML 功能足够表达本项目规则。
            data = yaml.load(source,Loader=UniqueSafeLoader)  # 用专用安全加载器解析，连同重复键规则一起生效。
            return cls(data,schema,allow_draft=allow_draft)  # 创建 BusinessDictionary，构造时还会做业务与 SQL 校验。
        except OSError:  # 捕获文件不存在、无法读取等文件系统错误。
            raise BusinessError('dictionary_missing','Approved metric dictionary is missing or unreadable.') from None  # 返回统一的字典缺失错误，不泄露底层路径异常细节。
        except (yaml.YAMLError,RecursionError):  # 捕获 YAML 语法错误和过深递归。
            fail('Metric dictionary is not valid safe YAML.')  # 转换为安全的字典解析错误。

    @property  # property 使 approved 像属性一样读取，不需要写括号调用。
    def approved(self):  # 定义检查当前字典是否已确认的属性。
        return self._data['status']=='approved'  # 当 status 为 approved 时返回 True。

    def _validate(self,allow_draft):  # 定义配置整体校验逻辑。
        try:  # 将结构错误转成统一业务错误。
            data = self._data  # 用局部变量 data 简化后续访问。
            if not isinstance(data,dict) or data['schema_version'] != 1:  # 配置顶层必须是字典，并且 schema_version 为 1。
                fail('Unsupported metric dictionary version.')  # 拒绝不支持的格式版本。
            if data['status'] not in ('draft','approved'):  # 状态只能为 draft 或 approved。
                fail('Unknown dictionary status.')  # 拒绝拼写错误或其他未知状态。
            if not self.approved and not allow_draft:  # 未确认且调用方没有明确允许查看草稿时拒绝。
                raise BusinessError('approval_required','Business definitions require user confirmation before use.')  # 返回 approval_required；这是配置状态约束，不会自动打开聊天让用户确认。
            if self.approved and (data['approval']['confirmed_by'] != 'user' or not data['approval']['confirmed_on']):  # 已确认配置需要记录 confirmed_by=user 和非空确认日期；这是本地记录校验，并非数字签名认证。
                fail('Approved definitions must record user confirmation.')  # 缺少确认记录就认为配置不完整。
            if data['reporting_currency'] != 'USD' or data['base'] != {'table':'sales','alias':'s'}:  # 固定报告币种 USD，基表必须是 sales 且简称 s。
                fail('This dictionary must use the teaching sales table and USD values.')  # 不同基表或币种不是当前实现支持的业务口径。
            if data['time'] != {'field':'s.order_date','interval':'start_inclusive_end_exclusive',  # 检查日期字段必须为下单日、区间必须含起点不含终点。
                                 'missing_range':'ask','relative_dates':'ask_reference_date'}:  # 继续检查缺日期要询问、相对日期需要参考日期的配置声明。
                fail('Date handling differs from the confirmed business policy.')  # 日期声明与已确认口径不同则拒绝。相对日期的对话询问尚待后续模型层实现。
            if data['market']['default_dimension'] != 'customer_country' or data['market']['field'] != 'c.country':  # 默认市场必须按客户国家，且字段是 c.country。
                fail('Market handling differs from the confirmed customer-country policy.')  # 拒绝悄悄切换到门店国家等口径。
            if data['joins'] != {'p':{'table':'products','on':'s.product_key = p.product_key'},  # 要求产品关系严格为销售产品编号连接产品编号。
                                 'c':{'table':'customers','on':'s.customer_key = c.customer_key'}}:  # 客户关系严格为销售客户编号连接客户编号。
                fail('Unsupported business table relationships.')  # 其他关联定义不支持，避免错误 JOIN 放大金额。
            # 注释：下面会用实际外键与主键确认这些连接是多对一。
            foreign_keys = self._schema['tables']['sales']['foreign_keys']  # 取得销售表的真实外键说明。
            for key,target in [('product_key','products'),('customer_key','customers')]:  # 分别验证产品关联和客户关联。
                if not any(fk['columns']==[key] and fk['target_table']==target and fk['target_columns']==[key] for fk in foreign_keys):  # 至少存在一条来源字段、目标表、目标字段全部匹配的外键。
                    fail('Required foreign key is missing from the schema.')  # 缺失所需外键就拒绝。
                if self._schema['tables'][target]['primary_key'] != [key]:  # 维度表的主键必须恰好是对应的编号列。
                    fail('Dimension join could duplicate sales rows.')  # 没有唯一约束时关联可能复制销售行，因此拒绝生成指标。
            if not {'revenue','gross_profit','gross_margin','order_count','active_customer','quantity'} <= data['metrics'].keys():  # 要求六个核心指标全部存在；集合子集检查并不禁止将来增加其他指标。
                fail('A required core metric is missing.')  # 缺指标时报告配置不完整。
            for name,metric in data['metrics'].items():  # 逐一检查字典中的每个指标。
                if not re.fullmatch('[a-z][a-z0-9_]*',name) or not isinstance(metric['label'],str) or not metric['label']:  # 指标 ID 需小写字母开头、后接字母数字下划线；中文显示名必须非空字符串。
                    fail('Metric IDs and labels are required.')  # 不合规范则拒绝。
                if metric['unit'] not in ('USD','ratio','orders','customers','units'):  # 单位仅接受美元、比率、订单、客户或件数这五类。
                    fail('Unknown metric unit.')  # 拒绝未知单位。
                if metric['unit'] in ('USD','ratio') and '估算' not in metric['label']:  # 金额与比率指标的中文显示名必须包含“估算”。
                    fail('Money and margin labels must disclose that they are estimates.')  # 缺少估算标记就拒绝，防止误当实际成交额或财务结果。
                if not isinstance(metric['description'],str) or not metric['description'] or not isinstance(metric['limitations'],list):  # 要求非空文本 description，以及列表类型 limitations；这里没有逐项验证限制说明文本内容。
                    fail('Metric descriptions and limitations are required.')  # 说明或限制容器类型不对时拒绝。
                expression = self._expression(name)  # 解析该指标的最终公式，比例指标会展开分子分母。
                query = self._compose([f'{expression} AS metric_value'])  # 以该公式构建一条固定 JOIN 的候选查询，并统一结果名 metric_value。
                parsed = sqlglot.parse_one(query,read='mysql')  # 再次解析完整查询，以检查投影中的聚合方式。
                projection = parsed.expressions[0]  # 取 SELECT 的第一个投影，即指标表达式。
                if metric['kind'] != 'ratio' and not list(projection.find_all(exp.AggFunc)):  # 非比例指标至少要包含一个聚合函数。
                    fail('A metric must aggregate rows.')  # 只有单行字段没有聚合的定义被拒绝。
                # 注释：不能在聚合表达式外夹带普通列，也不会猜测 GROUP BY。
                for column in projection.find_all(exp.Column):  # 遍历指标中所有列引用。
                    if column.find_ancestor(exp.AggFunc) is None:  # 每个列节点必须有聚合函数祖先。
                        fail('Metric columns must be inside aggregation functions.')  # 发现聚合外的列就拒绝，减少口径不明的 SQL。
                validate_sql(query,self._schema)  # 将候选查询交给完整 SQL 白名单校验器，检查真实列和允许函数。
            for dimension in data['dimensions'].values():  # 继续验证用于分组的维度定义。
                expression = dimension['expression']  # 取维度表达式，例如 c.country。
                if list(parse_expression(expression).find_all(exp.AggFunc)):  # 维度应表示单行属性，不能包含 SUM 等聚合函数。
                    fail('A dimension cannot aggregate rows.')  # 聚合维度被拒绝。
                validate_sql(self._compose([expression]),self._schema)  # 将维度组成查询，校验它引用的表列是否允许。
        except BusinessError:  # 捕获明确的业务错误。
            raise  # 原样抛出，保留详细业务错误码。
        except (KeyError,TypeError,AttributeError,ValueError,sqlglot.errors.SqlglotError):  # 捕获配置键、类型、属性、SQL 解析等问题；SQLValidationError 也继承 ValueError。
            fail('Metric configuration references invalid fields, expressions or properties.')  # 用统一错误说明替代底层细节。

    def _expression(self,name,trail=()):  # 定义递归解析公式的函数；trail 保存当前依赖路径。
        if name in trail:  # 如果当前指标已在依赖路径里，出现了循环引用。
            fail('Metric dependency cycle detected.')  # 拒绝例如“毛利率的分子还是毛利率”这样的无限递归。
        if name in self._expressions:  # 检查公式是否已经算好并缓存。
            return self._expressions[name]  # 命中缓存时直接返回，避免重复展开。
        metric = self._data['metrics'][name]  # 取得当前指标的配置。
        if metric['kind']=='ratio':  # 比例类型进入分子/分母组合分支。
            numerator,denominator = metric['numerator'],metric['denominator']  # 取出分子与分母的指标 ID。
            if self._data['metrics'][numerator]['unit'] != self._data['metrics'][denominator]['unit'] or metric['unit']!='ratio':  # 两者单位必须一致，且当前指标单位必须是 ratio。
                fail('Ratio numerator and denominator must have matching units.')  # 单位不匹配就拒绝；单位一致只是基本检查，不能替代人判断商业含义。
            value = f'({self._expression(numerator,trail+(name,))}) / NULLIF(({self._expression(denominator,trail+(name,))}),0)'  # 递归展开两个汇总公式，相除；NULLIF(分母,0) 把零变 NULL，避免除零；trail 加当前指标以检查循环。
        elif metric['kind'] in ('additive','distinct_count'):  # 加总类和去重计数类进入直接公式分支。
            value = metric['expression']  # 读取配置中的 expression。
            parsed = parse_expression(value)  # 检查它是一个独立、允许形状的 SQL 表达式。
            if metric['kind']=='distinct_count':  # 去重计数需要额外校验。
                if not isinstance(parsed,exp.Count) or not isinstance(parsed.this,exp.Distinct):  # 要求最外层为 COUNT 且内部为 DISTINCT，不能误用普通 COUNT。
                    fail('Distinct-count metrics must use COUNT(DISTINCT ...).')  # 不满足去重语义就拒绝。
        else:  # 其他类型进入不支持分支。
            fail('Unsupported metric aggregation kind.')  # 报告未知指标聚合类别。
        self._expressions[name] = value  # 把最终公式存入缓存。
        return value  # 返回最终公式文本。

    def _compose(self,projections,where=None,group_by=None):  # 定义组合查询的辅助函数：projections 是 SELECT 列表，where 和 group_by 可选。
        # 固定已校验的多对一关系，让各指标使用一致筛选范围。
        query = ('SELECT '+','.join(projections)+' FROM sales s '  # 拼出 SELECT 列表及销售基表 sales s。
                 'JOIN products p ON s.product_key=p.product_key '  # 按产品编号连接产品表 p，为金额和类别提供字段。
                 'JOIN customers c ON s.customer_key=c.customer_key')  # 按客户编号连接客户表 c，为客户国家筛选提供字段。
        if where:  # 有筛选条件才添加 WHERE。
            query += ' WHERE '+where  # 将已构造的筛选表达式接到查询后。
        if group_by:  # 有分组表达式才添加分组。
            query += ' GROUP BY '+group_by+' ORDER BY '+group_by  # 用同一表达式 GROUP BY 和 ORDER BY；结果按维度排序，而非按金额排名。
        return query  # 返回尚需最终安全校验的 SQL 文本。

    def get_metric(self,name):  # 定义取得单个指标信息的方法。
        if name not in self._data['metrics']:  # 检查指标 ID 是否存在。
            raise BusinessError('unknown_metric','Unknown metric ID; do not infer a formula.')  # 未知指标明确报错，不自行猜测净利润等算法。
        result = deepcopy(self._data['metrics'][name])  # 深拷贝该指标，防止返回对象被修改时影响内部配置。
        result.update(id=name,expression=self._expressions[name],status=self._data['status'])  # 补入 ID、展开后的表达式、当前确认状态。
        return result  # 返回指标信息字典。

    def context(self):  # 定义取得整本字典副本的方法，未来可供模型理解业务规则。
        return deepcopy(self._data)  # 返回深拷贝，调用方不能通过此副本改变内部配置。

    def query(self,name,*,start=None,end=None,country=None,dimension=None):  # 定义查询生成入口，参数是指标、起止日期、可选国家和分组维度。
        if not self.approved:  # 检查字典确认状态。
            raise BusinessError('approval_required','Draft definitions cannot build executable metric queries.')  # 即使允许加载草稿用于阅读，也不能用草稿生成可执行查询。
        self.get_metric(name)  # 调用 get_metric，利用其存在性检查；返回对象此处不使用。
        if start is None or end is None:  # 开始或结束日期缺少任一项都不继续。
            raise BusinessError('time_range_required','请提供时间范围；start 包含当天，end 不包含当天。')  # 返回“请提供时间范围”的业务错误，明确起点包含、终点不包含。
        try:  # 开始验证日期格式、真实性和前后关系。
            if not all(isinstance(v,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',v) for v in (start,end)):  # 两个值都必须是严格 YYYY-MM-DD 字符串；正则中的反斜杠 d 表示数字。
                raise ValueError()  # 格式不符合时主动抛出 ValueError，由下面统一处理。
            first,last = date.fromisoformat(start),date.fromisoformat(end)  # 将文本解析成真实 date，2 月 30 日会失败。
            if first >= last:  # 起点必须早于终点。
                raise ValueError()  # 同一天或倒序日期视为无效范围。
        except ValueError:  # 集中处理日期校验失败。
            raise BusinessError('invalid_time_range','Use valid YYYY-MM-DD dates with start before end.') from None  # 抛出明确的 invalid_time_range，隐藏底层异常链。
        literal = lambda value: exp.Literal.string(value).sql(dialect='mysql')  # 定义小函数，用 sqlglot 把筛选值编码为 MySQL 字符串字面量，避免手工加引号出错。
        where = f's.order_date >= {literal(start)} AND s.order_date < {literal(end)}'  # 生成下单日 >= start 且 < end 的条件；查询全年 2019 的 end 应为 2020-01-01。
        if country is not None:  # 只有传入国家时才增加国家筛选。
            if country not in self._data['market']['countries']:  # 国家必须与配置中英文列表的一项精确匹配，目前不负责把“德国”翻译为 Germany。
                raise BusinessError('unknown_country','Choose an exact country from the business dictionary.')  # 未知国家明确报错。
            where += ' AND c.country = '+literal(country)  # 追加客户国家筛选，值仍通过字面量编码。
        columns = [self._expressions[name]+' AS metric_value','COUNT(*) AS matching_sales_lines']  # SELECT 返回指标值，以及 COUNT(*) 得到的匹配销售明细行数；后者不是去重订单数。
        group_by = None  # 默认不分组。
        if dimension is not None:  # 若指定 dimension，则进入分组处理。
            if dimension not in self._data['dimensions']:  # 维度 ID 必须在配置中存在。
                raise BusinessError('unknown_dimension','Unknown dimension ID.')  # 未知维度报错，不允许临时指定任意个人字段。
            group_by = self._data['dimensions'][dimension]['expression']  # 取得维度对应字段或日期表达式。
            columns.insert(0,group_by+' AS dimension_value')  # 把维度值插到结果第一列，命名 dimension_value。
        return validate_sql(self._compose(columns,where,group_by),self._schema).sql  # 组合 SQL 后再完整校验，只返回重新生成的安全 SQL；此方法本身不执行查询。

def run_metric(name,*,start=None,end=None,country=None,dimension=None):  # 定义“生成并实际执行指标查询”的便利函数。
    engine = readonly_engine()  # 创建带身份/权限检查的只读引擎。
    try:  # 保证最后释放引擎。
        dictionary = BusinessDictionary.load(get_schema(engine))  # 读取真实 schema，再加载并校验正式业务字典。
        sql = dictionary.query(name,start=start,end=end,country=country,dimension=dimension)  # 根据调用参数生成 SQL；缺时间会在这里报错，之前可能已访问数据库元数据。
        tool = SQLTool(engine)  # 创建安全 SQL 执行器。
        tool._schema = dictionary._schema  # 复用刚刚已取得的 schema，避免再加载一次；这里直接访问内部属性，是当前实现的耦合点。
        coverage = tool.run_sql('SELECT MIN(order_date) AS first_date, MAX(order_date) AS last_date FROM sales')  # 通过相同安全工具查询全表最早和最晚下单日期，判断请求是否落在数据覆盖区间。
        first,last = coverage.rows[0]  # 从覆盖查询第一行解包出两个日期值；工具已经把日期转为 ISO 文本。
        result = tool.run_sql(sql)  # 执行实际指标 SQL；即使范围在覆盖区间外，当前顺序仍会先执行这条查询。
        metric = dictionary.get_metric(name)  # 读取标签、单位、限制说明，准备结果。
        no_coverage = first is None or end <= first or start > last  # 空数据表、请求结束不晚于最早日、或请求开始晚于最晚日，都算完全无覆盖。
        partial_coverage = not no_coverage and (start < first or end > str(date.fromisoformat(last)+timedelta(days=1)))  # 有重叠但请求伸出数据起止日就标记部分覆盖；最晚日期加一天是为匹配结束日不包含的规则。
        if no_coverage:  # 完全没有覆盖时进入特殊返回分支。
            status = 'outside_data_coverage'  # 状态设置为 outside_data_coverage，不能把历史样例之外的年份解释成零业务。
            rows = []  # 对外返回空 rows，舍弃可能具有误导性的聚合零值。
        else:  # 范围与现有数据有交集时进入正常分支。
            rows = result.rows  # 保留指标查询返回的行。
            status = 'ok' if any(int(row[-1]) > 0 for row in rows) else 'no_matching_transactions'  # 每行最后一项是匹配明细数；至少一行大于 0 则 ok，否则为 no_matching_transactions。
        return {'metric_id':name,'label':metric['label'],'unit':metric['unit'],'status':status,  # 返回指标 ID、中文标签、单位和状态。
                'start_inclusive':start,'end_exclusive':end,'country':country,'dimension':dimension,  # 返回实际采用的时间、国家、分组条件，方便核对口径。
                'data_coverage':{'first_date':first,'last_date':last,'partial':partial_coverage},  # 返回全数据最早/最晚日期以及是否部分覆盖；日期包络不证明中间每天都有完整数据。
                'columns':result.columns,'rows':rows,'limit_reached':result.limit_reached,  # 返回列名、数据行及是否碰到上限的标记。
                'limitations':metric['limitations'],'trace_id':result.trace_id,  # 附上指标限制说明和指标查询的 trace_id。
                'coverage_trace_id':coverage.trace_id,'sql':result.sql}  # 附上覆盖检查的 trace_id 和实际执行 SQL；调用方若展示原始 SQL，要知道其中包含筛选值。
    finally:  # 无论成功失败都释放资源。
        engine.dispose()  # 关闭引擎连接池。

def main():  # 定义终端入口，处理你输入的命令。
    parser = argparse.ArgumentParser(description=__doc__)  # 创建命令行解析器。
    parser.add_argument('--metric')  # --metric 接收指标 ID，例如 revenue。
    parser.add_argument('--start')  # --start 接收包含当天的起始日期。
    parser.add_argument('--end',help='Exclusive end date, e.g. 2020-01-01 for all of 2019')  # --end 接收不包含当天的结束日期，并提供帮助说明。
    parser.add_argument('--country')  # --country 接收英文国家名称。
    parser.add_argument('--dimension')  # --dimension 接收配置中的分组维度 ID。
    parser.add_argument('--list',action='store_true')  # --list 是布尔开关，列出指标；不执行指标汇总，但仍读取数据库结构来校验配置。
    args = parser.parse_args()  # 解析终端参数到 args 对象。
    try:  # 开始捕获业务和执行错误。
        if args.list:  # 若指定 --list，优先进入列指标模式。
            engine = readonly_engine()  # 创建只读引擎用于读取实际结构。
            try:  # 保证列举模式也会清理连接池。
                book = BusinessDictionary.load(get_schema(engine))  # 读取结构并加载正式字典。
                output = [book.get_metric(name) for name in book.context()['metrics']]  # 逐个取得指标的标签、公式等信息，构成输出列表。
            finally:  # 不论列举成功失败都清理资源。
                engine.dispose()  # 释放引擎。
        elif args.metric:  # 未指定 --list 但指定 --metric 时，进入计算模式。
            output = run_metric(args.metric,start=args.start,end=args.end,country=args.country,dimension=args.dimension)  # 将全部终端参数按名字传入 run_metric，取得完整结果字典。
        else:  # 既不列举也没有指标 ID 时进入错误分支。
            parser.error('Choose --list or --metric.')  # argparse 输出参数使用错误并退出，通常退出码为 2。
        print(json.dumps(output,ensure_ascii=False,indent=2))  # 输出格式化 JSON，ensure_ascii=False 保留中文。
        return 0  # 成功返回退出码 0。
    except (BusinessError,QueryError,SQLValidationError) as error:  # 捕获已定义的业务、查询和校验错误。
        print(json.dumps({'error':error.code,'message':str(error)},ensure_ascii=False,indent=2))  # 输出错误码和安全说明；这里没有把 QueryError 的 trace_id 加入错误 JSON，是当前接口的限制。
        return 1  # 失败返回退出码 1。
    except (SQLAlchemyError,ValueError):  # 处理引擎或结构加载产生的其他 ValueError/SQLAlchemyError。
        print(json.dumps({'error':'connection_or_schema_error','message':'Business schema could not be loaded.'}))  # 输出统一结构加载失败提示。
        return 1  # 返回失败码 1。

if __name__ == '__main__':  # python -m tools.business_tool 或直接作为入口运行时执行下面一行。
    raise SystemExit(main())  # 运行 main 并以其返回值退出程序。
