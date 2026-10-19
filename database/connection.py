# 文件作用：阶段 1：读取应用连接配置、建立小型连接池，并在每次取出连接时核验账号、库名、权限以及查询会话设置。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Read-only credentials, verified grants, and server/client timeouts."""  # 模块说明：集中管理只读凭据、权限核验、服务器和客户端超时。
from pathlib import Path  # 导入：Path 用于以对象方式拼接、读取和写入文件路径。

from dotenv import dotenv_values  # 导入：dotenv_values 用于把 .env 文件读取为配置字典。
from sqlalchemy import create_engine, event  # 导入：create_engine 用于建立 SQLAlchemy 数据库引擎和连接池；event 用于注册数据库连接池取出连接时的监听函数。
from sqlalchemy.engine import URL  # 导入：URL 用于结构化组装数据库地址，正确处理密码特殊字符。

from security.permissions import ALLOWED_COLUMNS, DATABASE, READONLY_USER, DEFAULT_POLICY  # 导入：ALLOWED_COLUMNS 用于取得固定表/列白名单；DATABASE 用于取得固定教学数据库名；READONLY_USER 用于取得固定只读账号名；DEFAULT_POLICY 用于取得统一的默认查询资源限制。

ROOT = Path(__file__).resolve().parents[1]  # 得到项目根目录；配置从这里的 .env 读取。

def database_url(path=None):  # 定义生成连接 URL 的函数；path 可供受信任调用方指定另一个配置文件。
    values = dotenv_values(path or ROOT/'.env')  # dotenv_values 将配置读成字典，不把所有内容打印出来；path 未传则读取根目录 .env。
    required = ('DB_HOST','DB_PORT','DB_USER','DB_PASSWORD','DB_NAME')  # 列出五项必填设置。
    if any(not values.get(key) for key in required):  # 任何必填项缺失或为空都视为配置错误，因此运行期只读密码不能是空字符串。
        raise ValueError('Missing database settings in .env. No connection attempted.')  # 尚未连接数据库就报错，避免带着缺失设置尝试登录。
    if values['DB_USER'] != READONLY_USER or values['DB_NAME'] != DATABASE:  # 要求使用固定只读账号和教学数据库，拒绝用 root 作为应用账号。
        raise ValueError('Use the dedicated readonly user and demo database.')  # 身份或库名不合要求时停止。
    return URL.create('mysql+pymysql',username=values['DB_USER'],password=values['DB_PASSWORD'],  # URL.create 结构化组装 MySQL/PyMySQL URL，安全处理密码中的特殊字符，不需要手工拼接 URL。
                      host=values['DB_HOST'],port=int(values['DB_PORT']),database=values['DB_NAME'])  # 补充主机、整数端口和数据库名，返回 URL 对象。

def readonly_engine(policy=DEFAULT_POLICY):  # 定义创建只读引擎的函数；默认采用统一 QueryPolicy。
    engine = create_engine(database_url(),pool_pre_ping=True,echo=False,hide_parameters=True,  # 创建 SQLAlchemy Engine；pre_ping 在复用时探活，echo=False 不打印 SQL，hide_parameters 避免异常日志展示参数。
                           pool_size=2,max_overflow=0,pool_timeout=5,  # 连接池最多 2 个连接，不额外扩容；获取连接最多等 5 秒。
                           connect_args={'connect_timeout':5,'read_timeout':policy.timeout_ms/1000+2,  # 建立 TCP 连接限 5 秒；客户端读取超时比服务器策略多 2 秒，为服务器返回超时错误留出余量。
                                         'write_timeout':5,'charset':'utf8mb4','local_infile':False})  # 写网络请求限 5 秒；用 utf8mb4；关闭本地文件导入能力 local_infile。
    @event.listens_for(engine,'checkout')  # 注册 checkout 监听器：每次从连接池取连接都会运行下面函数，包含首次获取。
    def configure(dbapi_connection, _record, _proxy):  # 接收底层 DBAPI 连接和连接池内部对象；后两个参数本函数不使用。
        dbapi_connection.rollback()  # 回滚可能残留的事务状态，避免复用前一次调用的未结束事务。
        with dbapi_connection.cursor() as cursor:  # 使用底层游标设置和检查当前连接。
            cursor.execute('SELECT CURRENT_USER(), DATABASE()')  # 读取数据库实际认证身份和当前数据库，而不是只相信 .env 中的声明。
            if cursor.fetchone() != (f'{READONLY_USER}@127.0.0.1', DATABASE):  # 要求实际身份是固定用户@127.0.0.1，数据库也是教学库。
                raise ValueError('Unexpected database identity.')  # 不匹配即拒绝该连接。
            cursor.execute('SHOW GRANTS')  # 查询当前身份的实际授权。
            account = f'`{READONLY_USER}`@`127.0.0.1`'  # 组装 SHOW GRANTS 中的账号文本，用于严格比较。
            expected = {f'GRANT USAGE ON *.* TO {account}'} | {  # 预期权限集合包含 USAGE；USAGE 表示账号存在，不代表可以读写所有表；竖线表示集合并集。
                f'GRANT SELECT ON `{DATABASE}`.`{table}` TO {account}' for table in ALLOWED_COLUMNS}  # 再加入五张表各自的 SELECT 授权。
            if {row[0] for row in cursor.fetchall()} != expected:  # 将实际授权文本组成集合，要求与预期完全相同；额外权限也会导致失败。
                raise ValueError('Database privileges differ from the approved read-only grants.')  # 发现权限被扩大、缩小或显示格式不符时，停止使用连接；这种精确匹配针对当前本机 MySQL 环境。
            dbapi_connection.rollback()  # 结束刚才检查可能产生的事务状态。
            cursor.execute('SET SESSION TRANSACTION READ ONLY')  # 将会话默认事务设为只读，是表级 SELECT 授权以外的另一层限制。
            cursor.execute(f'SET SESSION max_execution_time={policy.timeout_ms}')  # 设置服务器查询执行时间上限，单位毫秒，默认 5000。
            cursor.execute("SET SESSION sql_mode='ONLY_FULL_GROUP_BY,STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")  # 固定 SQL 模式：严格分组、严格数据检查、拒绝无效日期等，避免连接受其他会话设置影响。
    return engine  # 返回配置好的引擎；调用者实际 connect 时才会取得连接并执行监听器。
