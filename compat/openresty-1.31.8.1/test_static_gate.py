#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""本地可重复的版本门禁预处理测试；不需要下载 NGINX 源码。"""

from pathlib import Path
import subprocess
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix="sg51-version-gate-") as directory:
        temporary = Path(directory)
        for name in ("ngx_config.h", "ngx_core.h", "ngx_http.h", "ngx_http_proxy_module.h"):
            (temporary / name).write_text("// 实验桩头文件；不参与产品构建。\n", encoding="utf-8")
        source = temporary / "check.cpp"
        source.write_text('#include "ngx.hpp"\n', encoding="utf-8")
        cases = [
            ("稳定版默认允许", 1031001, None, True),
            ("新版默认禁止", 1031008, None, False),
            ("新版实验开关开启允许", 1031008, 1, True),
            ("新版实验开关为零禁止", 1031008, 0, False),
            ("旧版错误启用实验开关禁止", 1031001, 1, False),
            ("未验证版本默认禁止", 1031007, None, False),
            ("未验证版本即使有实验开关也禁止", 1031009, 1, False),
        ]
        for label, version, flag, expected in cases:
            command = ["g++", "-E", "-x", "c++", "-I", str(temporary),
                       "-I", str(REPO_ROOT / "src"), f"-Dnginx_version={version}"]
            if flag is not None:
                command.append(f"-DNGX_OTEL_OPENRESTY_13181_LAB={flag}")
            command.append(str(source))
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            passed = result.returncode == 0
            if passed != expected:
                raise AssertionError(
                    f"门禁检查失败：{label}；实际={passed}，应为={expected}；"
                    f"stderr={result.stderr[-800:]}"
                )
            print(f"通过：{label} (nginx_version={version})")
    print("通过：稳定默认门禁与实验放行条件的全部 7 项静态检查")


if __name__ == "__main__":
    main()
