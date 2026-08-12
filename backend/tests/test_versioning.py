"""NodeOut Agent 版本派生字段。"""

from datetime import datetime, timezone

from app import config
from app.models import Node
from app.schemas import NodeOut
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
    """NodeOut 从硬件信息派生当前版本，并公开控制平面目标版本。"""
    out = NodeOut.model_validate(_node("0.1.0"))
    assert out.agent_version == "0.1.0"
    assert out.agent_required == config.APP_VERSION
    assert "agent_outdated" not in out.model_dump()

    unknown = NodeOut.model_validate(_node(None))
    assert unknown.agent_version is None
