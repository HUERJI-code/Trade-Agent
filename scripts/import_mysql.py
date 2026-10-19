# 文件作用：阶段 0：先校验生成 SQL 的指纹，再调用本机 mysql 客户端导入新数据库。这是初始化写入步骤，日常查询不走这里。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Import the prepared sample into a NEW database; password stays in mysql's prompt."""  # 模块说明：只向新数据库导入；管理员密码由 mysql 客户端提示输入。
import argparse  # 导入：argparse 用于解析终端中的 --参数。
import hashlib  # 导入：hashlib 用于计算 SHA-256 文件或查询指纹。
import json  # 导入：json 用于在 Python 对象与 JSON 文本之间转换。
import shutil  # 导入：shutil 用于查找 PATH 中的 mysql 可执行程序。
import subprocess  # 导入：subprocess 用于启动 mysql 外部进程并传递 SQL 输入。
import sys  # 导入：sys 用于访问模块搜索路径、退出码或标准错误流。
from pathlib import Path  # 导入：Path 用于以对象方式拼接、读取和写入文件路径。

ROOT = Path(__file__).resolve().parents[1]  # 定位项目根目录，保证可以稳定找到 Data/prepared。

def main():  # 定义命令行程序入口函数。
    parser = argparse.ArgumentParser(description=__doc__)  # 创建参数解析器；description 使用文件顶部说明。
    parser.add_argument('--user', default='root')  # 允许通过 --user 指定导入账号，默认 root，因为建库需要初始化权限。
    parser.add_argument('--host', default='127.0.0.1')  # 允许指定主机，默认连接本机 IPv4 回环地址。
    parser.add_argument('--port', type=int, default=3306)  # 端口参数转成整数，默认 MySQL 的 3306。
    parser.add_argument('--check-only', action='store_true', help='Validate local import files without connecting')  # --check-only 是开关：出现时为 True，仅检查本地导入条件，不执行导入。
    args = parser.parse_args()  # 读取并解析终端参数。
    folder = ROOT/'Data'/'prepared'  # 定位准备好的导入文件所在目录。
    sql_file = folder/'import_global_electronics.sql'  # 定位导入 SQL 文件。
    profile = json.loads((folder/'data_profile.json').read_text(encoding='utf-8'))  # 读取准备阶段产生的 JSON 报告，取得预期 SQL 指纹。
    if hashlib.sha256(sql_file.read_bytes()).hexdigest() != profile['sql_sha256']:  # 重新计算 SQL 文件 SHA-256 并比较；这里只防止意外变更，不是数字签名认证。
        raise SystemExit('Import file changed. Run scripts/prepare_data.py again before importing.')  # 不一致时退出，要求重新准备，避免执行与画像不一致的 SQL。
    mysql = shutil.which('mysql')  # 在系统 PATH 中查找 mysql 可执行程序。
    if not mysql:  # 若没有找到客户端，进入报错分支。
        raise SystemExit('mysql executable not found on PATH.')  # 退出并提示缺少 mysql 客户端；即使 --check-only 也先经过此检查。
    if args.check_only:  # 检查是否只做本地验证。
        print('Local checks passed. No database connection or writes performed.')  # 打印未连接数据库、未进行写入的结果。
        return 0  # 返回成功状态码 0，跳过后面的导入流程。
    print('Creating a NEW database: trade_agent_demo. Existing database -> stop with error.',flush=True)  # 提醒目标库必须是新库；flush=True 立即显示消息。
    print('Enter your MySQL password at the next prompt. It is not stored by this script.',flush=True)  # 提示在 mysql 的控制台输入密码；本脚本不把该密码写入配置。
    # 注释解释两个输入通道：SQL 从 stdin 读，密码从交互控制台读。
    # 注释解释不使用 --force：批量执行遇到首个错误即停止。
    command = [mysql,'--no-defaults','--batch','--raw','--binary-mode',  # 以参数列表调用 mysql；不加载默认配置，批处理输出、原始输出，binary-mode 限制非交互客户端命令处理。
               '--default-character-set=utf8mb4','--protocol=TCP',  # 明确 UTF-8 字符集和 TCP 连接协议。
               f'--host={args.host}',f'--port={args.port}',f'--user={args.user}',  # 将解析出的主机、端口、账号分别作为命令参数，未通过 shell 拼接执行。
               '--connect-timeout=5','--password']  # 连接最多等待 5 秒；无密码值的 --password 会提示交互输入，空管理员密码时直接回车。
    with sql_file.open('rb') as source:  # 以二进制读取 SQL 文件，保证作为 stdin 传递时不被文本换行转换。
        result = subprocess.run(command,stdin=source,check=False)  # 启动 mysql，SQL 文件传给标准输入；check=False 表示由下面代码自己检查退出码。
    if result.returncode:  # 非零退出码代表导入失败。
        print('Import failed. Do not delete/retry automatically; report the error for diagnosis.',file=sys.stderr)  # 向标准错误流提示失败，不自动删库或重试。
        print('MySQL DDL is not transactional; a failed import can leave an empty/partial schema.',file=sys.stderr)  # 说明 MySQL 建库建表语句有隐式提交，失败可能留下部分结构。
    else:  # 退出码为 0 时进入成功分支。
        print('Import completed. Each table should show passed=1 in the results above.')  # 提示查看前面表行数检查的 passed 值；它们应全部为 1。
    return result.returncode  # 返回 mysql 的退出码，保留真实执行结果。

if __name__ == '__main__':  # 仅直接运行脚本时调用入口。
    sys.exit(main())  # 运行 main，并让其返回值成为整个程序的退出码。
