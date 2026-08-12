"""版本比较与 NodeOut 派生字段（Agent 版本 / 过旧判定）。"""

from datetime import datetime, timezone

from app import config
from app.models import Node
from app.schemas import NodeOut
from app.services.versioning import version_compare


def test_version_compare_numeric():
    """点分数字段比较（数值而非字典序），忽略 rc/dev 后缀。"""
    assert version_compare("0.1.0", "0.1.1") == -1
    assert version_compare("0.1.1", "0.1.0") == 1
    assert version_compare("0.1.1", "0.1.1") == 0
    assert version_compare("0.9", "0.10") == -1   # 数值：0.9 < 0.10
    assert version_compare("1.0.1", "1.0") == 1
    assert version_compare("0.1.1-dev", "0.1.1") == 0  # 忽略后缀
    # 不可解析时按字符串兜底（不崩溃即可）
    assert version_compare("abc", "0.1.0") == 1


def _node(version: str | None) -> Node:
    return Node(
        id=1, name="n", ip="192.0.2.1", ssh_port=22, ssh_username="root",
        ssh_auth_type="key", agent_port=9000, agent_status="online",
        hardware_info={"agent_version": version} if version is not None else None,
        last_seen=None,
        created_at=datetime.now(timezone.utc),
        cluster_id=None,
    )


def test_node_out_agent_version_fields():
    """NodeOut 派生：agent_version / agent_required / agent_outdated 由 hardware_info + 控制平面版本得出。"""
    out = NodeOut.model_validate(_node("0.1.0"))
    assert out.agent_version == "0.1.0"
    assert out.agent_required == config.APP_VERSION
    assert out.agent_outdated is True  # 0.1.0 < 要求版本

    fresh = NodeOut.model_validate(_node(config.APP_VERSION))
    assert fresh.agent_outdated is False

    unknown = NodeOut.model_validate(_node(None))
    assert unknown.agent_version is None
    assert unknown.agent_outdated is None
