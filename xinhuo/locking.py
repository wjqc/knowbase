"""repo 级跨进程互斥锁（flock 锁文件）。

stdio MCP 意味着 8 个 Agent 各自拉起独立服务进程，写并发是常态。
所有改变仓库状态的操作（分配 id → 写文件 → git → 重建索引）必须在
同一个锁事务内完成，事务边界 = 锁边界。
"""

import errno
import fcntl
import time
from pathlib import Path


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
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    self._fh.close()
                    raise LockTimeout(f"repo lock 超时({self._timeout}s): {self._path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
