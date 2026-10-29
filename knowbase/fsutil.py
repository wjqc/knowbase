"""跨平台文件替换与原子写辅助。"""
import os
import time


def replace_with_retry(src, dst, *, attempts: int = 5, delay: float = 0.1) -> None:
    """os.replace 包装：Windows 上目标文件被杀毒/编辑器/宿主进程打开且无共享删除
    权限时抛 PermissionError；先退避重试再判定失败。POSIX 上该错误即真实权限问题，
    立刻抛出不重试。"""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))
