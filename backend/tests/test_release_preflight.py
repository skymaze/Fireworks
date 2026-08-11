"""发布元数据校验脚本的 GitHub ref 行为回归。"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_preflight.py"
VERSION = ROOT.joinpath("VERSION").read_text(encoding="utf-8").strip()
WRONG_VERSION = "9.9.9" if VERSION != "9.9.9" else "9.9.8"
GITHUB_REF_ENV = (
    "GITHUB_EVENT_NAME",
    "GITHUB_REF",
    "GITHUB_REF_NAME",
    "GITHUB_REF_TYPE",
)


def _run_preflight(**values: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in GITHUB_REF_ENV:
        env.pop(key, None)
    env.update(values)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_preflight_accepts_main_branch_push():
    result = _run_preflight(
        GITHUB_EVENT_NAME="push",
        GITHUB_REF="refs/heads/main",
        GITHUB_REF_NAME="main",
        GITHUB_REF_TYPE="branch",
    )
    assert result.returncode == 0, result.stderr


def test_preflight_accepts_matching_release_tag():
    result = _run_preflight(
        GITHUB_EVENT_NAME="push",
        GITHUB_REF=f"refs/tags/v{VERSION}",
        GITHUB_REF_NAME=f"v{VERSION}",
        GITHUB_REF_TYPE="tag",
    )
    assert result.returncode == 0, result.stderr


def test_preflight_rejects_mismatched_release_tag():
    result = _run_preflight(
        GITHUB_EVENT_NAME="push",
        GITHUB_REF=f"refs/tags/v{WRONG_VERSION}",
        GITHUB_REF_NAME=f"v{WRONG_VERSION}",
        GITHUB_REF_TYPE="tag",
    )
    assert result.returncode != 0
    assert f"与 VERSION v{VERSION} 不一致" in result.stderr
