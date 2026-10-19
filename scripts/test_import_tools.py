# 文件作用：阶段 0 的 unittest 检查。它不由默认 pytest.ini 自动发现；单独运行验证 CSV 转换、SQL 文本编码、指纹拦截及只检查模式。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Offline checks for import boundaries; these do not prove MySQL execution."""  # 说明是离线检查，不等于 MySQL 已实际执行导入。
import contextlib  # 导入：contextlib 用于把测试中的终端输出重定向到内存。
import hashlib  # 导入：hashlib 用于计算 SHA-256 文件或查询指纹。
import io  # 导入：io 用于在内存中模拟文件，供 CSV 解析或输出捕获。
import json  # 导入：json 用于在 Python 对象与 JSON 文本之间转换。
import tempfile  # 导入：tempfile 用于创建测试结束后可清理的临时目录。
import unittest  # 导入：unittest 用于运行阶段 0 的标准库测试。
from decimal import Decimal  # 导入：Decimal 用于精确表示十进制金额，避免普通浮点误差。
from pathlib import Path  # 导入：Path 用于以对象方式拼接、读取和写入文件路径。
from unittest.mock import patch  # 导入：patch 用于在测试期间临时替换函数、路径或命令行参数。

import import_mysql  # 导入：import_mysql 用于调用并模拟数据导入入口。
from prepare_data import convert, literal  # 导入：convert 用于验证 CSV 数据类型转换；literal 用于验证 Python 值安全编码为 SQL 字面量。


class ImportChecks(unittest.TestCase):  # 继承 unittest.TestCase，使 test_ 方法被标准库测试框架执行。
    def test_preserve_business_values(self):  # 验证转换不损坏原始业务值。
        self.assertEqual(convert('00123', 'str'), '00123')  # 邮编按 str 转换时应保留开头的 0。
        self.assertIsNone(convert(' ', 'date'))  # 空白日期应保持缺失 None。
        self.assertEqual(convert('2/20/2021', 'date'), '2021-02-20')  # 美式日期应转为 ISO 日期。
        self.assertEqual(convert('$1,234.50 ', 'decimal'), Decimal('1234.50'))  # 带美元符号、逗号和空格的金额应精确转成 Decimal。
        with self.assertRaises(ValueError):  # 声明接下来必须抛出 ValueError。
            convert('2/30/2021', 'date')  # 尝试不存在的 2 月 30 日，不能静默纠正为其他日期。

    def test_text_is_data_even_with_sql_punctuation(self):  # 验证含 SQL 符号的源数据仍当作普通文本。
        text = "O'Brien\\name; DROP DATABASE example; -- café"  # 构造带单引号、反斜杠、分号、SQL 字样及重音字母的测试文本。
        encoded = literal(text)  # 将其转换成导入 SQL 的文本字面量。
        self.assertNotIn('DROP', encoded)  # 十六进制编码后的 SQL 不应包含可执行的 DROP 字样。
        self.assertEqual(bytes.fromhex(encoded.split("'")[1]).decode('utf-8'), text)  # 提取编码字节并解码，必须完整还原原文，证明编码没有损坏数据。
        self.assertEqual(literal(None), 'NULL')  # None 应生成为 SQL NULL。

    def test_modified_sql_never_runs(self):  # 验证被修改的 SQL 不会触发 mysql 导入。
        with tempfile.TemporaryDirectory() as folder:  # 创建自动清理的临时目录。
            root = Path(folder)  # 用 Path 包装临时根目录。
            prepared = root/'Data'/'prepared'  # 按项目结构构造临时 Data/prepared。
            prepared.mkdir(parents=True)  # 创建所需目录。
            (prepared/'import_global_electronics.sql').write_text('modified',encoding='utf-8')  # 写入内容为 modified 的测试 SQL 文件，不是真实导入文件。
            (prepared/'data_profile.json').write_text(json.dumps({'sql_sha256':hashlib.sha256(b'original').hexdigest()}))  # 写一个只匹配 original 内容的指纹，故意制造不一致。
            with patch.object(import_mysql,'ROOT',root), patch('sys.argv',['import_mysql.py']), patch.object(import_mysql.subprocess,'run') as run:  # 临时替换导入模块 ROOT、命令行参数及 subprocess.run，避免真实进程启动。
                with self.assertRaises(SystemExit):  # 期望导入入口主动退出。
                    import_mysql.main()  # 调用 import_mysql.main 触发指纹检查。
                run.assert_not_called()  # 证明没有启动任何外部导入进程。

    def test_check_only_never_connects(self):  # 验证 --check-only 模式不启动 mysql 子进程；它仍依赖本地生成文件及 mysql 在 PATH 中。
        with patch('sys.argv',['import_mysql.py','--check-only']), patch.object(import_mysql.subprocess,'run') as run, contextlib.redirect_stdout(io.StringIO()):  # 模拟只检查参数和进程调用，同时将终端输出重定向到内存。
            self.assertEqual(import_mysql.main(),0)  # 只检查流程应返回成功码 0。
            run.assert_not_called()  # 证明没有调用 subprocess.run，不建立导入连接。


if __name__ == '__main__':  # 作为脚本运行时才启动测试。
    unittest.main()  # 交给 unittest 自动发现并运行本文件的四个测试方法。
