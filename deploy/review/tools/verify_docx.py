"""校验下载的批注文档是合法 docx 且包含 Word 批注（comments.xml）。

用法：python verify_docx.py <file.docx>
退出码 0 = 合法且含批注；1 = 校验失败（打印原因）。
"""

import sys
import zipfile
from pathlib import Path


def verify(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"文件不存在: {path}"
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "word/document.xml" not in names:
                return False, "缺少 word/document.xml（不是合法 docx）"
            if "word/comments.xml" not in names:
                return False, "缺少 word/comments.xml（批注未写入）"
            # 批注内容应非空
            comments = zf.read("word/comments.xml")
            if b"<w:comment " not in comments:
                return False, "comments.xml 中无 w:comment 元素"
    except zipfile.BadZipFile:
        return False, "不是有效的 zip/docx 文件"
    return True, f"合法 docx，批注已写入（{path.stat().st_size} bytes）"


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python verify_docx.py <file.docx>", file=sys.stderr)
        return 2
    ok, msg = verify(Path(sys.argv[1]))
    print(f"[verify-docx] {'PASS' if ok else 'FAIL'}: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
