"""抽取 web/index.html 里的内联 JS，落到临时文件后交给 `node --check` 做语法校验。

为什么需要它：前端是**单文件、无构建步骤**的（JS 全内联在 index.html 里），
所以"直接 node --check app.js"这种检查在这里根本无从下手 ——
之前是靠"肉眼看过了"糊过去的，等于没检查。

这个脚本用 UTF-8 读写（避免 Windows 默认代码页把中文与 `…` 弄成乱码，
那种乱码会伪装成 JS 语法错误，反而盖住真问题）。

用法：
    python scripts/check_js.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web" / "index.html"

# 只取**内联** script 块（有 src 的是外链，不在这里校验）
SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("跳过：本机没有 node，无法做 JS 语法校验")
        return 0
    if not WEB.exists():
        print(f"找不到 {WEB}")
        return 1

    html = WEB.read_text(encoding="utf-8")
    blocks = SCRIPT_RE.findall(html)
    if not blocks:
        print("web/index.html 里没有内联 <script> 块")
        return 1

    print(f"内联 script 块：{len(blocks)} 个（来源 {WEB.name}）")
    bad = 0
    for i, code in enumerate(blocks, 1):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(code)
            tmp = fh.name
        try:
            proc = subprocess.run([node, "--check", tmp],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        finally:
            Path(tmp).unlink(missing_ok=True)

        first = code.strip().splitlines()[0] if code.strip() else ""
        if proc.returncode == 0:
            print(f"  ✓ 块 {i}｜{len(code)} 字符｜首行：{first[:60]}")
        else:
            bad += 1
            print(f"  ✗ 块 {i}｜{len(code)} 字符｜首行：{first[:60]}")
            for line in (proc.stderr or "").splitlines()[:12]:
                print("      " + line)

    if bad:
        print(f"\n✗ {bad} 个内联脚本块语法不通过")
        return 1
    print("\n✓ 前端内联 JS 语法全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
