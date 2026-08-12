#!/usr/bin/env python3
"""验证 Fireworks release candidate 的版本与发布材料一致。"""

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def fail(message: str) -> None:
    raise SystemExit(f"release preflight failed: {message}")


def main() -> None:
    version = ROOT.joinpath("VERSION").read_text(encoding="utf-8").strip()
    if not SEMVER.fullmatch(version):
        fail(f"VERSION 不是有效 SemVer: {version!r}")

    # 控制平面版本已收口到 config.py（main.py 引用之），发布校验以 config.py 为准
    backend = ROOT.joinpath("backend/app/config.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"]+)"$', backend, re.MULTILINE)
    if not match or match.group(1) != version:
        fail("config.APP_VERSION 与 VERSION 不一致")

    agent = ROOT.joinpath("agent/main.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"]+)"$', agent, re.MULTILINE)
    if not match or match.group(1) != version:
        fail("agent APP_VERSION 与 VERSION 不一致")

    frontend = json.loads(
        ROOT.joinpath("frontend/package.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        ROOT.joinpath("frontend/package-lock.json").read_text(encoding="utf-8")
    )
    if frontend.get("version") != version:
        fail("frontend/package.json version 与 VERSION 不一致")
    if lock.get("version") != version or lock["packages"][""].get("version") != version:
        fail("frontend/package-lock.json version 与 VERSION 不一致")

    env_example = ROOT.joinpath(".env.example").read_text(encoding="utf-8")
    match = re.search(r"^FW_IMAGE_TAG=([^\s#]+)$", env_example, re.MULTILINE)
    if not match or match.group(1) != version:
        fail(".env.example 的 FW_IMAGE_TAG 与 VERSION 不一致")

    for compose_name in ("docker-compose.prod.yml", "docker-compose.prod.cn.yml"):
        compose = ROOT.joinpath(compose_name).read_text(encoding="utf-8")
        if f"FW_IMAGE_TAG={version}" not in compose:
            fail(f"{compose_name} 缺少当前版本部署示例")

    notes = ROOT / f"docs/releases/v{version}.md"
    if not notes.is_file():
        fail(f"缺少发布说明: {notes.relative_to(ROOT)}")
    changelog = ROOT.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{version}]" not in changelog:
        fail(f"CHANGELOG.md 缺少 {version} 条目")

    ref_type = os.environ.get("GITHUB_REF_TYPE", "")
    ref = os.environ.get("GITHUB_REF", "")
    if ref_type == "tag" or ref.startswith("refs/tags/"):
        tag = os.environ.get("GITHUB_REF_NAME", "")
        if tag != f"v{version}":
            fail(f"标签 {tag!r} 与 VERSION v{version} 不一致")

    print(f"Fireworks v{version} release preflight passed")


if __name__ == "__main__":
    main()
