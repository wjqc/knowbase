"""多格式解析错误。"""
from __future__ import annotations


class ParseError(Exception):
    def __init__(self, parser: str, reason: str, path: str = ""):
        self.parser = parser
        self.reason = reason
        self.path = path
        super().__init__(f"{parser}: {reason}" + (f" ({path})" if path else ""))
