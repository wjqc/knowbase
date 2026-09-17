"""git 操作：审计提交 + 受白名单约束的推送。

推送失败永不阻塞保存（离线可用是本地化工具的底线）；
远端白名单校验把"永不推公网"从纪律变成代码。
"""

import subprocess
from pathlib import Path


def _run(repo: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()


def is_repo(repo: Path) -> bool:
    return (Path(repo) / ".git").exists()


def commit_all(repo: Path, message: str) -> str | None:
    """add -A + commit。返回告警信息（None=成功），绝不抛出。"""
    try:
        _run(repo, "add", "-A")
        r = subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", message],
            capture_output=True, text=True, encoding="utf-8",
        )
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 and "nothing to commit" not in out:
            return f"git commit 失败: {out.strip()}"
        return None
    except Exception as e:  # git 缺失、无身份配置等：保存已落盘，只告警
        return f"git 操作告警(不影响保存): {e}"


def push(repo: Path, remote_url: str, allowed_prefixes: list[str]) -> str | None:
    if not remote_url:
        return None
    if not any(remote_url.startswith(p) for p in allowed_prefixes):
        return f"已拒绝推送: remote {remote_url} 不在白名单 {allowed_prefixes} 内"
    try:
        _run(repo, "push", "-u", "origin", "HEAD")
        return None
    except Exception as e:
        return f"push 失败(已忽略，本地已提交): {e}"
