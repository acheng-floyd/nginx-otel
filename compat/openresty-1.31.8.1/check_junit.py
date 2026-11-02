#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""精确统计 pytest JUnit XML，不允许用通过数量掩盖跳过数量。"""
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def totals_from_xml(filename):
    root = ET.parse(filename).getroot()
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.findall("testsuite"))
    else:
        raise ValueError("JUnit XML 的根节点必须是 testsuite 或 testsuites")
    if not suites:
        raise ValueError("JUnit XML 不含有效测试套件")
    return {
        key: sum(int(s.get(key, 0)) for s in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def main():
    if len(sys.argv) != 3:
        raise SystemExit("用法：check_junit.py <JUnit XML 路径> <期望测试数量>")
    expected_count = int(sys.argv[2])
    if not Path(sys.argv[1]).is_file():
        raise SystemExit("失败：本项回归没有生成 JUnit XML，请检查 pytest 运行步骤；不允许记作通过")
    actual = totals_from_xml(sys.argv[1])
    expected = dict(tests=expected_count, failures=0, errors=0, skipped=0)
    print(f"测试统计：{actual}；期望：{expected}")
    if actual != expected:
        raise SystemExit("失败：测试数量或零失败/零错误/零跳过门禁不符合要求")
    print("通过：精确数量及零失败/错误/跳过门禁")


if __name__ == "__main__":
    main()
