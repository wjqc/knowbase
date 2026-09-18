"""OCR 图片解析器（P5-A3）。

基于 pytesseract + PIL：
- 支持格式：PNG / JPG / JPEG / GIF / BMP / WEBP / TIFF / TIF
- text：OCR 抽取的纯文本
- meta：format / width / height / mode / language（默认 eng）

依赖（三层检查）：
1. PIL：解析图片字节（必需）
2. pytesseract：Python 包装（可选，缺失抛 ParseError 提示 pip install）
3. tesseract 二进制：实际 OCR 引擎（可选，缺失抛 ParseError 提示
   `brew install tesseract` / `apt install tesseract-ocr`）

设计选择：始终注册 OcrParser，让 ingestion.service 决定重试 / tombstone。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .base import DocumentParser, ParsedDocument


IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif",
    ".bmp", ".webp", ".tif", ".tiff",
}


def _check_tesseract_binary() -> str | None:
    """返回 tesseract 可执行文件路径；找不到返回 None。"""
    return shutil.which("tesseract")


class OcrParser(DocumentParser):
    name = "ocr"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in IMAGE_EXTENSIONS

    def parse(self, path: Path) -> ParsedDocument:
        from ...observability.errors import ParseError

        # Layer 1: PIL（必须）
        try:
            from PIL import Image
        except ImportError as e:
            raise ParseError(parser=self.name,
                             reason="Pillow not installed; uv add pillow",
                             path=str(path)) from e

        # Layer 2: pytesseract（可选，但必需做 OCR）
        try:
            import pytesseract  # type: ignore
        except ImportError as e:
            raise ParseError(parser=self.name,
                             reason="pytesseract not installed; pip install pytesseract",
                             path=str(path)) from e

        # Layer 3: tesseract 二进制（可选，但必需做 OCR）
        if _check_tesseract_binary() is None:
            raise ParseError(
                parser=self.name,
                reason=(
                    "tesseract binary not found in PATH; "
                    "macOS: brew install tesseract; "
                    "Linux: apt install tesseract-ocr; "
                    "Windows: download from https://github.com/UB-Mannheim/tesseract/wiki"
                ),
                path=str(path),
            )

        # 打开图片
        try:
            img = Image.open(str(path))
            img.load()  # 触发完整解码（提前暴露损坏文件）
        except Exception as e:
            raise ParseError(parser=self.name,
                             reason=f"cannot open image: {e}",
                             path=str(path)) from e

        width, height = img.size
        mode = img.mode

        # OCR
        try:
            text = pytesseract.image_to_string(img, lang="eng")
        except Exception as e:
            raise ParseError(parser=self.name,
                             reason=f"OCR failed: {e}",
                             path=str(path)) from e

        text = text.strip()
        return ParsedDocument(
            text=text,
            meta={
                "format": "ocr",
                "language": "eng",
                "width": width,
                "height": height,
                "mode": mode,
                "text_length": len(text),
                "title": path.stem,
            },
        )