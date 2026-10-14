"""git 超时必须强杀整棵进程树（Windows memory_save 无限卡死的根因回归测试）。

背景：subprocess.run(timeout=N) 超时只杀 git.exe 本身，孙进程
（git-remote-http / 凭据管理器）继承管道句柄不死，communicate() 等
EOF 永久阻塞。这里用假 git 模拟同构场景：父进程 sleep、孙进程持有
stdout 管道 sleep，验证 _run 在超时后快速失败且孙进程被清掉。

假 git 用 Python 而非 sh 脚本：少一层进程 spawn，且不受沙箱对 shell
脚本的启动检查影响（本机曾偶发在 timeout 内没写完 pid 文件）。
"""

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from knowbase import gitops


def test_run_git_returns_output_normally():
    code, out, err = gitops._run_git([sys.executable, "-c", "print('ok')"], timeout=10)
    assert code == 0
    assert out.strip() == "ok"
    assert err == ""


@pytest.mark.skipif(os.name != "posix", reason="假 git 依赖进程组语义")
def test_run_kills_process_tree_when_grandchild_holds_pipe(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "grandchild.pid"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "git").write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"with open({str(pid_file)!r}, 'w') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "    fh.flush()\n"
        "    os.fsync(fh.fileno())\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    (fake_bin / "git").chmod((fake_bin / "git").stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    started = time.monotonic()
    # 孙进程 sleep 60 持有管道：若未杀树，这里会阻塞到 ~60s 才返回
    with pytest.raises(RuntimeError, match="超时"):
        gitops._run(tmp_path, "fetch", "origin", timeout=6)
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"超时后应快速失败，实际耗时 {elapsed:.1f}s"

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.05)
    assert pid_file.exists(), "假 git 未写出孙进程 pid，无法验证进程树击杀"

    grandchild = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return  # 孙进程已被整组击杀
        time.sleep(0.1)
    pytest.fail(f"孙进程 {grandchild} 在进程树击杀后仍存活")
