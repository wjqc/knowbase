"""repo 级跨进程互斥锁：POSIX 用 flock，Windows 用 msvcrt 字节范围锁。

stdio MCP 意味着多个 Agent 各自拉起独立服务进程，写并发是常态。
所有改变仓库状态的操作（分配 id → 写文件 → git → 重建索引）必须在
同一个锁事务内完成，事务边界 = 锁边界。
"""

import errno
import time
from pathlib import Path

try:  # POSIX (macOS / Linux)
    import fcntl

    def _try_lock(fh) -> bool:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            return False

    def _unlock(fh):
        fcntl.flock(fh, fcntl.LOCK_UN)

except ImportError:  # Windows
    import msvcrt

    def _try_lock(fh) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


class LockTimeout(RuntimeError):
    pass


class RepoLock:
    def __init__(self, repo: Path, timeout: float = 10.0):
        self._path = Path(repo) / ".lock"
        self._timeout = timeout
        self._fh = None

    def __enter__(self):
        self._fh = open(self._path, "a+")
        deadline = time.monotonic() + self._timeout
        while True:
            if _try_lock(self._fh):
                return self
            if time.monotonic() >= deadline:
                self._fh.close()
                raise LockTimeout(f"repo lock 超时({self._timeout}s): {self._path}")
            time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            _unlock(self._fh)
        finally:
            self._fh.close()
