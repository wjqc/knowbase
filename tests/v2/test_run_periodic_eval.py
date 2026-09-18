"""P5-C4 测试：bin/run_periodic_eval.py CLI 入口。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "bin" / "run_periodic_eval.py"


def _run(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    if check and proc.returncode not in (0, 2):
        raise AssertionError(
            f"CLI exit {proc.returncode}\nSTDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )
    return proc


# ---- subcommands exist ------------------------------------------------------

class TestCLIStructure:
    def test_help_exits_zero(self):
        proc = _run(["--help"])
        assert proc.returncode == 0
        assert "P5-C" in proc.stdout or "period" in proc.stdout.lower()

    def test_subcommands_listed(self):
        proc = _run(["--help"])
        for sub in ("run", "diff", "versions"):
            assert sub in proc.stdout

    def test_run_help(self):
        proc = _run(["run", "--help"])
        assert proc.returncode == 0
        assert "--output" in proc.stdout
        assert "--parser-version" in proc.stdout

    def test_diff_help(self):
        proc = _run(["diff", "--help"])
        assert proc.returncode == 0
        assert "--baseline" in proc.stdout
        assert "--candidate" in proc.stdout


# ---- versions subcommand ----------------------------------------------------

class TestVersionsSubcommand:
    def test_returns_json_with_all_builtins(self):
        proc = _run(["versions"])
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        for name in ("markdown", "code", "html", "txt", "log", "pdf",
                     "docx", "xlsx", "pptx", "ocr"):
            assert name in payload, f"missing parser: {name}"

    def test_each_parser_has_impl_sha256(self):
        proc = _run(["versions"])
        payload = json.loads(proc.stdout)
        for name, info in payload.items():
            assert info["impl_sha256"] is not None
            assert len(info["impl_sha256"]) == 64
            assert info["impl_source"].endswith(".py")


# ---- run subcommand ---------------------------------------------------------

class TestRunSubcommand:
    def test_default_run_exits_zero(self):
        proc = _run(["run"])
        assert proc.returncode == 0
        assert "总用例" in proc.stdout or "总用" in proc.stdout
        assert "pass_rate" in proc.stdout

    def test_run_with_json_output(self, tmp_path):
        out = tmp_path / "eval.json"
        proc = _run(["run", "-o", str(out)])
        assert proc.returncode == 0
        assert out.exists()
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["kind"] == "parser_eval"
        assert "report" in payload
        assert "case_results" in payload
        assert "parser_versions" in payload
        assert payload["report"]["total"] >= 7
        assert payload["report"]["passed"] == payload["report"]["total"]

    def test_parser_version_override_takes_effect(self):
        proc = _run(["run", "--parser-version", "markdown=9.9.9,code=8.8.8"])
        assert proc.returncode == 0
        # Look for the version tag in the parser_versions section
        assert "v9.9.9" in proc.stdout
        assert "v8.8.8" in proc.stdout

    def test_invalid_version_format_exits_nonzero(self):
        proc = _run(["run", "--parser-version", "badformat-no-equals"])
        assert proc.returncode != 0


# ---- diff subcommand --------------------------------------------------------

class TestDiffSubcommand:
    def _write_eval(self, tmp_path: Path, *, name: str = "v1") -> Path:
        """Generate a JSON eval report by invoking `run`."""
        out = tmp_path / f"{name}.json"
        proc = _run(["run", "-o", str(out)])
        assert proc.returncode == 0
        return out

    def test_diff_two_identical_reports(self, tmp_path):
        base = self._write_eval(tmp_path, name="b")
        cand = self._write_eval(tmp_path, name="c")
        proc = _run(["diff", "--baseline", str(base), "--candidate", str(cand)])
        assert proc.returncode == 0
        assert "regressions=0" in proc.stdout
        assert "gate: PASS" in proc.stdout

    def test_diff_missing_baseline(self, tmp_path):
        cand = self._write_eval(tmp_path, name="c")
        proc = _run([
            "diff",
            "--baseline", str(tmp_path / "nope.json"),
            "--candidate", str(cand),
        ])
        assert proc.returncode == 1
        assert "baseline not found" in proc.stderr

    def test_diff_missing_candidate(self, tmp_path):
        base = self._write_eval(tmp_path, name="b")
        proc = _run([
            "diff",
            "--baseline", str(base),
            "--candidate", str(tmp_path / "nope.json"),
        ])
        assert proc.returncode == 1
        assert "candidate not found" in proc.stderr

    def test_diff_with_json_output(self, tmp_path):
        base = self._write_eval(tmp_path, name="b")
        cand = self._write_eval(tmp_path, name="c")
        diff_out = tmp_path / "diff.json"
        proc = _run([
            "diff",
            "--baseline", str(base),
            "--candidate", str(cand),
            "-o", str(diff_out),
        ])
        assert proc.returncode == 0
        assert diff_out.exists()
        payload = json.loads(diff_out.read_text(encoding="utf-8"))
        assert "had_regression" in payload
        assert payload["had_regression"] is False
        assert payload["regressions"] == 0
