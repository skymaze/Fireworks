"""模型/镜像节点分发选择校验。"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..errors import Code, api_error
from ..models import Cluster, Node


def validate_roles(head_node_id: int | None, sync_node_ids: list[int]) -> None:
    """保证 head/worker 角色完整、互斥且 worker 不重复。"""
    if head_node_id is None and sync_node_ids:
        raise HTTPException(422, "选择 worker 时必须同时指定 head 节点")
    if len(sync_node_ids) != len(set(sync_node_ids)):
        raise HTTPException(422, "worker 节点不能重复")
    if head_node_id in sync_node_ids:
        raise HTTPException(422, "head 节点不能同时作为 worker")


def validate_distribution_cluster(
    db: Session,
    head_node_id: int | None,
    sync_node_ids: list[int],
    cluster_id: int | None = None,
) -> None:
    """分发节点必须全部属于同一个已选择的集群。"""
    validate_roles(head_node_id, sync_node_ids)
    if head_node_id is None:
        if cluster_id is not None:
            raise HTTPException(422, "选择集群后必须至少选择一个分发节点")
        return

    node_ids = [head_node_id, *sync_node_ids]
    nodes: list[Node] = []
    for node_id in node_ids:
        node = db.get(Node, node_id)
        if not node:
            raise api_error(404, Code.NODE_NOT_FOUND, "节点不存在")
        nodes.append(node)

    selected_cluster_id = cluster_id if cluster_id is not None else nodes[0].cluster_id
    if selected_cluster_id is None:
        raise HTTPException(422, "分发节点必须先加入集群")
    if not db.get(Cluster, selected_cluster_id):
        raise api_error(404, Code.CLUSTER_NOT_FOUND, "集群不存在")
    mismatched = [node.name for node in nodes if node.cluster_id != selected_cluster_id]
    if mismatched:
        raise HTTPException(422, f"禁止跨集群分发，以下节点不属于所选集群：{', '.join(mismatched)}")
