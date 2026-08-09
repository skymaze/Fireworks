"""集群管理：CRUD、成员（head/worker）管理、集群参数、网络测试。"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..errors import Code, api_error
from ..models import Cluster, ClusterNode, MetricSample, Node, Task, TaskNode
from ..services import network_config as network_config_svc
from ..services import network_test as network_test_svc
from ..services import recipe_render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clusters", tags=["clusters"])


def get_cluster_or_404(db: Session, cluster_id: int) -> Cluster:
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise api_error(404, Code.CLUSTER_NOT_FOUND, "集群不存在")
    return cluster


def _occupied_node_names(db: Session, node_ids: list[int]) -> dict[int, str]:
    """返回已加入集群的节点 {node_id: 集群名}（一节点一集群：cluster_id 非空即占用）。"""
    nodes = db.query(Node).filter(Node.id.in_(node_ids), Node.cluster_id.isnot(None)).all()
    if not nodes:
        return {}
    cluster_names = {
        c.id: c.name
        for c in db.query(Cluster).filter(Cluster.id.in_({n.cluster_id for n in nodes})).all()
    }
    return {n.id: cluster_names.get(n.cluster_id, f"#{n.cluster_id}") for n in nodes}


def _cidr_overlap(cidr_a: str, cidr_b: str) -> bool:
    """两个 CIDR 网段是否有重叠。"""
    import ipaddress

    try:
        return ipaddress.ip_network(cidr_a, strict=False).overlaps(
            ipaddress.ip_network(cidr_b, strict=False)
        )
    except ValueError:
        return False


def _setup_cluster_trust(db: Session, cluster: Cluster) -> None:
    """配置集群内成员双向 SSH 免密（镜像/模型 RoCE rsync 依赖）。

    head/worker 由每次任务或传输动态指定，不存在集群级唯一 head，因此让全部
    成员两两互信（幂等：deploy_agent.ensure_ssh_trust 会剔除旧条目）；失败仅
    警告（写入服务端日志）不阻断。
    """
    from ..services.deploy_agent import ensure_ssh_trust

    members = [db.get(Node, m.node_id) for m in cluster.members]
    members = [n for n in members if n is not None]
    for head in members:
        for other in members:
            if other.id == head.id:
                continue
            try:
                ok, msg = ensure_ssh_trust(head, other)
                if not ok:
                    logger.warning("%s→%s 免密配置失败：%s", head.name, other.name, msg)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s→%s 免密配置异常：%s", head.name, other.name, e)


def find_available_cidr(
    db: Session, base_cidr: str, exclude_cluster_id: int | None = None, max_tries: int | None = None
) -> str:
    """从 base_cidr 起自增（按掩码步进）找不与任何已有集群网段重叠的网段。

    例如 10.0.0.0/16 被占用时依次尝试 10.1.0.0/16、10.2.0.0/16……
    自增限制在 10/8 私网段（10.255.255.255）内；全部占用则抛 409 提示手动设置。
    """
    import ipaddress

    try:
        base = ipaddress.ip_network(base_cidr, strict=False)
    except ValueError as e:
        raise api_error(400, Code.CIDR_FORMAT_ERROR, f"网段格式错误：{base_cidr}（{e}）",
                        params={"cidr": base_cidr}, details=str(e)) from e
    used = [
        ipaddress.ip_network(c.network_cidr, strict=False)
        for c in db.query(Cluster).filter(Cluster.network_cidr.isnot(None))
        if c.network_cidr and (exclude_cluster_id is None or c.id != exclude_cluster_id)
    ]
    step = 1 << (32 - base.prefixlen)  # 按网段大小步进（/16 → 65536，/24 → 256）
    max_addr = int(ipaddress.ip_network("10.255.255.255/32").network_address)
    # 10/8 内可容纳的同掩码网段数（/16 → 256，/22 → 16384）；未指定时用此值
    if max_tries is None:
        max_tries = 1 << (base.prefixlen - 8) if base.prefixlen > 8 else 1 << 24
    candidate = base
    for _ in range(max_tries):
        if not any(candidate.overlaps(u) for u in used):
            return str(candidate)
        nxt = int(candidate.network_address) + step
        if nxt > max_addr:
            break
        candidate = ipaddress.ip_network((nxt, base.prefixlen))
    raise api_error(409, Code.CIDR_NO_AVAILABLE, "10.x 高速网网段均已被占用，无可用网段，请手动设置其他网段")


def _configure_cluster_network(
    db: Session, node_ids: list[int], cidr: str | None, mtu: int | None
) -> tuple[dict | None, list[tuple[Node, int]]]:
    """为成员节点配置高速网络并验证；任一失败回滚已应用节点并抛 400。

    返回 (plan, [(node, index)])；node_ids 为空或未指定网段时不配置。
    """
    if not node_ids or not cidr:
        return None, []
    plan = network_config_svc.plan_cluster_network(cidr, mtu or 9000)
    applied: list[tuple[Node, int]] = []
    try:
        for i, nid in enumerate(node_ids, start=1):
            node = db.get(Node, nid)
            if not node:
                raise api_error(404, Code.NODE_NOT_FOUND, f"节点 {nid} 不存在",
                                params={"id": nid})
            # 先确认规划 IP 在本机可用、不与已有设备冲突（允许覆盖既有配置，
            # 但拒绝与其它接口/在网主机撞 IP，避免破坏交换机在用网段）
            conflicts = network_config_svc.node_ip_conflicts(node, plan, i)
            if conflicts:
                raise api_error(400, Code.NETWORK_CONFIGURE_FAILED,
                                f"节点 {node.name} 高速网 IP 冲突: {'；'.join(conflicts)}",
                                params={"name": node.name},
                                details="；".join(conflicts))
            ok, msg = network_config_svc.apply_node_network(node, plan, i)
            if not ok:
                raise api_error(400, Code.NETWORK_CONFIGURE_FAILED,
                                f"节点 {node.name} 高速网络配置失败：{msg}",
                                params={"name": node.name}, details=msg)
            applied.append((node, i))
        time.sleep(8)  # 等 NM 完成重配、SSH 恢复稳定
        for node, i in applied:
            peers = [(n, idx) for n, idx in applied if n.id != node.id]
            ok, detail = network_config_svc.verify_node_network(node, plan, i, peers)
            if not ok:
                raise api_error(400, Code.NETWORK_VERIFY_FAILED_ROLLBACK,
                                f"节点 {node.name} 高速网络验证失败（已回滚）：{detail}",
                                params={"name": node.name}, details=detail)
    except HTTPException:
        for node, _ in reversed(applied):
            try:
                network_config_svc.rollback_node_network(node)
            except Exception:  # noqa: BLE001
                pass
        raise
    return plan, applied


@router.get("", response_model=list[schemas.ClusterOut])
def list_clusters(db: Session = Depends(get_db)):
    return db.query(Cluster).order_by(Cluster.id).all()


@router.get("/available-cidr")
def available_cidr(
    base: str = Query("10.0.0.0/16", description="自增起始网段"),
    db: Session = Depends(get_db),
):
    """返回从 base 起首个空闲的高速网网段（10/8 内按掩码自增）。

    供创建集群弹窗打开时填入；全部占用时 409。
    """
    return {"cidr": find_available_cidr(db, base)}


@router.post("", response_model=schemas.ClusterOut, status_code=201)
def create_cluster(req: schemas.ClusterCreate, db: Session = Depends(get_db)):
    if db.query(Cluster).filter(Cluster.name == req.name).first():
        raise api_error(409, Code.CLUSTER_NAME_EXISTS, "同名集群已存在")
    # 成员节点不得已加入任何集群（一节点一集群：cluster_id 非空即占用）
    if req.node_ids:
        occupied = _occupied_node_names(db, req.node_ids)
        if occupied:
            node_names = {
                n.id: n.name for n in db.query(Node).filter(Node.id.in_(occupied.keys())).all()
            }
            names = "、".join(
                f"{node_names.get(nid, nid)}(已在「{cname}」)" for nid, cname in occupied.items()
            )
            raise api_error(409, Code.NODE_BELONGS_OTHER,
                            f"以下节点已加入其他集群，不可重复加入：{names}",
                            params={"names": names})
        # 网段校验：与已有集群重叠时 409（前端会重新获取可用网段并提示）
        if req.network_cidr:
            try:
                free = find_available_cidr(db, req.network_cidr)
            except HTTPException:
                raise
            if free != req.network_cidr:
                clash = next(
                    (
                        c
                        for c in db.query(Cluster).filter(Cluster.network_cidr.isnot(None))
                        if c.network_cidr and _cidr_overlap(req.network_cidr, c.network_cidr)
                    ),
                    None,
                )
                raise api_error(409, Code.CIDR_CONFLICT,
                                f"网段 {req.network_cidr} 已被集群「{clash.name}」占用，请改用可用网段（如 {free}）",
                                params={"cidr": req.network_cidr, "name": clash.name, "free": free})
    # 高速网络配置（创建时即配置成员节点，测试通过才落库；失败自动回滚）
    plan, applied = _configure_cluster_network(db, req.node_ids, req.network_cidr, req.network_mtu)
    cluster = Cluster(
        name=req.name,
        description=req.description,
        network_type=req.network_type,
        network_cidr=req.network_cidr,
        network_mtu=req.network_mtu,
        network_plan=plan,
    )
    db.add(cluster)
    db.commit()
    db.refresh(cluster)
    try:
        # 初始成员：net_index 按成员序号 1 起（与高速网 plan 分配索引一致）；
        # head/worker/rank 属任务级，随任务发布指定，不在集群成员上保存。
        # 原子占用 cluster_id（一节点一集群）
        for i, nid in enumerate(req.node_ids, start=1):
            claim = (
                db.query(Node)
                .filter(Node.id == nid, Node.cluster_id.is_(None))
                .update({Node.cluster_id: cluster.id})
            )
            if not claim:
                raise api_error(409, Code.NODE_BELONGS_OTHER,
                                f"节点 {nid} 已被其他集群占用，创建已取消",
                                params={"id": nid})
            db.add(
                ClusterNode(
                    cluster_id=cluster.id,
                    node_id=nid,
                    net_index=i,
                )
            )
        db.commit()
    except HTTPException:
        # 数据库回滚 + 节点网络回滚（配置已 apply，避免残留）
        db.rollback()
        for node, _ in reversed(applied):
            try:
                network_config_svc.rollback_node_network(node)
            except Exception:  # noqa: BLE001
                pass
        raise
    db.refresh(cluster)
    # 全成员双向 SSH 免密（镜像/模型 RoCE 分发依赖；失败仅警告）
    if req.node_ids:
        _setup_cluster_trust(db, cluster)
    return cluster


@router.get("/{cluster_id}", response_model=schemas.ClusterOut)
def get_cluster(cluster_id: int, db: Session = Depends(get_db)):
    return get_cluster_or_404(db, cluster_id)


@router.patch("/{cluster_id}", response_model=schemas.ClusterOut)
def update_cluster(cluster_id: int, req: schemas.ClusterUpdate, db: Session = Depends(get_db)):
    cluster = get_cluster_or_404(db, cluster_id)
    for k, v in req.model_dump(exclude_unset=True).items():
        setattr(cluster, k, v)
    db.commit()
    db.refresh(cluster)
    return cluster


@router.delete("/{cluster_id}")
def delete_cluster(
    cluster_id: int,
    force: bool = Query(False, description="集群下存在已结束任务时仍删除（任务将失去集群引用）；运行中/已发布/暂停任务须先停止，无论 force 均拒绝"),
    cleanup_network: bool = Query(False, description="同时清理成员节点上本项目写入的高速网络配置（删除本项目 999 文件并还原节点）"),
    db: Session = Depends(get_db),
):
    cluster = get_cluster_or_404(db, cluster_id)
    tasks = db.query(Task).filter(Task.cluster_id == cluster_id).all()
    # 运行中/已发布/暂停的任务（容器仍在节点上）：无论 force 都拒绝，须先停止任务
    ACTIVE_STATUSES = ("published", "running", "paused")
    active_tasks = [t for t in tasks if t.status in ACTIVE_STATUSES]
    if active_tasks:
        names = ", ".join(f"#{t.id} {t.name}（{t.status}）" for t in active_tasks)
        raise api_error(409, Code.CLUSTER_HAS_RUNNING_TASKS,
                        f"集群下存在未停止的任务（{names}）。请先在任务详情停止后再删除集群，"
                        "避免节点容器失去管理", params={"names": names})
    if tasks and not force:
        names = ", ".join(f"#{t.id} {t.name}" for t in tasks)
        raise HTTPException(
            409,
            f"集群下存在历史任务（{names}）。请先删除任务，或确认 force 删除（任务将失去集群引用）",
        )

    cleaned_nodes: list[str] = []
    warnings: list[str] = []
    if cleanup_network:
        # 仅清理本项目写入的高速网配置（删除 999 声明 + 还原被接管文件），
        # 使用与 apply 一致的回滚实现；不产生时间戳备份残留、不碰管理网。
        for m in cluster.members:
            node = db.get(Node, m.node_id)
            if not node:
                continue
            try:
                ok, msg = network_config_svc.rollback_node_network(node)
                if ok:
                    cleaned_nodes.append(f"{node.name}: {msg}")
                else:
                    warnings.append(f"{node.name}: {msg}")
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{node.name}: {e}")

    # 释放成员节点占用（一节点一集群）
    db.query(Node).filter(Node.cluster_id == cluster_id).update({Node.cluster_id: None})
    db.delete(cluster)
    db.commit()
    return {
        "ok": True,
        "force": bool(tasks and force),
        "cleaned_nodes": cleaned_nodes,
        "warnings": warnings,
    }


@router.post("/{cluster_id}/nodes", response_model=schemas.ClusterOut, status_code=201)
def add_cluster_node(cluster_id: int, req: schemas.ClusterNodeAdd, db: Session = Depends(get_db)):
    cluster = get_cluster_or_404(db, cluster_id)
    node = db.get(Node, req.node_id)
    if not node:
        raise api_error(404, Code.NODE_NOT_FOUND, "节点不存在")
    if any(m.node_id == req.node_id for m in cluster.members):
        raise api_error(409, Code.NODE_ALREADY_IN_CLUSTER, "该节点已在集群中")
    # 高速网槽位：追加分配（max+1），移除不复用，既有成员的 plan IP 保持稳定
    net_index = max((m.net_index for m in cluster.members), default=0) + 1
    # 一节点一集群：原子占用（仅 cluster_id 为空才可更新；并发/重复加入在此拦截）
    claim = (
        db.query(Node)
        .filter(Node.id == req.node_id, Node.cluster_id.is_(None))
        .update({Node.cluster_id: cluster_id})
    )
    if not claim:
        other = db.get(Cluster, node.cluster_id) if node.cluster_id else None
        raise api_error(409, Code.NODE_BELONGS_OTHER,
                        f"节点「{node.name}」已加入集群「{other.name if other else '#' + str(node.cluster_id)}」，一个节点只能属于一个集群",
                        params={"name": node.name})
    db.commit()
    db.refresh(node)

    # 按集群保存的高速网络规划配置新节点（同一接口同网段，序号=net_index），
    # 验证通过才加入；失败释放 cluster_id 并回滚网络。
    def _release_claim():
        node.cluster_id = None
        db.commit()

    if req.configure_network and cluster.network_plan:
        plan = cluster.network_plan
        ok, msg = network_config_svc.apply_node_network(node, plan, net_index)
        if not ok:
            _release_claim()
            raise api_error(400, Code.NETWORK_CONFIGURE_FAILED,
                            f"节点 {node.name} 高速网络配置失败：{msg}",
                            params={"name": node.name}, details=msg)
        # 对端以既有成员的 net_index 取 plan IP（与 apply/验证/网络测试一致）
        peers: list[tuple[Node, int]] = []
        for m in cluster.members:
            mn = db.get(Node, m.node_id)
            if mn:
                peers.append((mn, m.net_index))
        ok, detail = network_config_svc.verify_node_network(node, plan, net_index, peers)
        if not ok:
            try:
                network_config_svc.rollback_node_network(node)
            except Exception:  # noqa: BLE001
                pass
            _release_claim()
            raise api_error(400, Code.NETWORK_VERIFY_FAILED_ROLLBACK,
                            f"节点 {node.name} 高速网络验证失败，已回滚：{detail}",
                            params={"name": node.name}, details=detail)

    db.add(ClusterNode(cluster_id=cluster_id, node_id=req.node_id, net_index=net_index))
    db.commit()
    db.refresh(cluster)
    # 全成员双向免密（镜像/模型 RoCE 分发依赖）
    _setup_cluster_trust(db, cluster)
    return cluster


@router.delete("/{cluster_id}/nodes/{node_id}", response_model=schemas.ClusterOut)
def remove_cluster_node(cluster_id: int, node_id: int, db: Session = Depends(get_db)):
    cluster = get_cluster_or_404(db, cluster_id)
    link = db.query(ClusterNode).filter_by(cluster_id=cluster_id, node_id=node_id).first()
    if not link:
        raise api_error(404, Code.NODE_NOT_IN_CLUSTER, "节点不在集群中")
    # 防御：节点正被未停止任务（published/running/paused）使用（容器仍在运行）时
    # 拒绝移除成员，避免任务在节点上继续运行却失去集群引用（孤儿容器/损坏任务）。
    busy = (
        db.query(Task)
        .join(TaskNode, TaskNode.task_id == Task.id)
        .filter(TaskNode.node_id == node_id,
                Task.status.in_(("published", "running", "paused")))
        .first()
    )
    if busy:
        raise api_error(409, Code.NODE_BUSY,
                        f"节点仍在使用任务「{busy.name}」运行中，请先停止/删除该任务后再移除成员",
                        params={"task": busy.name})
    db.delete(link)
    # 释放节点占用（一节点一集群）
    node = db.get(Node, node_id)
    if node and node.cluster_id == cluster_id:
        node.cluster_id = None
    db.commit()
    db.refresh(cluster)
    return cluster


@router.get("/{cluster_id}/plan")
def cluster_plan(cluster_id: int, db: Session = Depends(get_db)):
    """集群发布信息：各成员节点 + 逐节点自动变量。

    head/worker/rank 属任务级，由发布向导按节点指定（head_roce_ip / nodes_total
    等共享变量依赖本次选择，由 /recipes/{id}/preview 返回）；
    此处仅提供每节点网络信息（node_roce_ip 等，本地高速网槽位 net_index 分配）。
    """
    cluster = get_cluster_or_404(db, cluster_id)
    members = sorted(cluster.members, key=lambda m: m.net_index)
    # 集群已配置高速网络时，node_roce_ip 以 plan 分配的 IP 为准（权威来源），
    # 避免 agent 上报的硬件缓存滞后于最新配置
    plan = cluster.network_plan or {}
    nodes_out = []
    for m in members:
        node = db.get(Node, m.node_id)
        if not node:
            continue
        # role/node_rank 仅为占位（渲染按任务 assignments 重新计算），
        # 此处取 plan IP 的关键是 net_index
        auto_vars = recipe_render.node_auto_vars(node, "worker", 0, plan=plan, net_index=m.net_index)
        if plan and "iface_subnets" in plan:
            try:
                plan_ips = network_config_svc.node_ips(plan, m.net_index)
                # 跟随该节点勾选后的 primary netdev（与 NCCL_IB_HCA 选择一致）；
                # 未配置网络时 node_auto_vars 已回退硬件值
                iface = auto_vars.get("netdev")
                if iface in plan_ips:
                    auto_vars["node_roce_ip"] = plan_ips[iface]
                else:
                    auto_vars["node_roce_ip"] = plan_ips.get("enp1s0f0np0", auto_vars["node_roce_ip"])
            except Exception:  # noqa: BLE001 - 规划取不到时保持硬件值
                pass
        nodes_out.append(
            {
                "node_id": m.node_id,
                "name": node.name,
                "ip": node.ip,
                "net_index": m.net_index,
                "agent_status": node.agent_status,
                "auto_vars": auto_vars,
            }
        )
    return {"nodes": nodes_out}


@router.post("/{cluster_id}/network-test")
async def cluster_network_test(cluster_id: int, req: schemas.NetworkTestRequest, db: Session = Depends(get_db)):
    """在集群内两个节点间运行网络测试（iperf3 / ib_write_bw / ib_read_bw / ping）。

    高速网 IP 优先取集群 network_plan 中该节点的接口 IP（权威来源），
    避免 agent 上报的硬件信息缓存滞后于最新配置。
    """
    cluster = get_cluster_or_404(db, cluster_id)
    member_ids = {m.node_id for m in cluster.members}
    if req.from_node_id not in member_ids or req.to_node_id not in member_ids:
        raise api_error(400, Code.NETWORK_TEST_NODES_NOT_IN_CLUSTER,
                        "测试的两个节点必须在集群中")
    from_node = db.get(Node, req.from_node_id)
    to_node = db.get(Node, req.to_node_id)

    # 集群 plan 中节点序号 = net_index（创建/加节点时按此分配 IP）
    net_map = {m.node_id: m.net_index for m in cluster.members}
    plan = cluster.network_plan or {}
    roce_override: str | None = None
    if plan and from_node.id in net_map and "iface_subnets" in plan:
        from_index = net_map[from_node.id]
        try:
            plan_ips = network_config_svc.node_ips(plan, from_index)
            # RDMA 目标 IP 跟随该节点勾选后的 primary netdev（与 NCCL_IB_HCA 一致）
            from_vars = recipe_render.node_auto_vars(from_node, "worker", 0, plan=plan, net_index=from_index)
            iface = from_vars.get("netdev")
            roce_override = plan_ips.get(iface) or plan_ips.get("enp1s0f0np0")
        except Exception:  # noqa: BLE001 - 取不到时回退默认
            pass

    return await network_test_svc.run_network_test(
        from_node, to_node, req.tool, req.duration, req.ib_device, roce_ip_override=roce_override
    )


# ---------- 集群级监控大盘（成员并排对比 + 汇总） ----------


def _cluster_nodes(db: Session, cluster_id: int) -> tuple[Cluster, list[Node]]:
    """集群及其实体节点（按成员顺序），404 检查。"""
    cluster = get_cluster_or_404(db, cluster_id)
    node_ids = [m.node_id for m in cluster.members]
    by_id = {n.id: n for n in db.query(Node).filter(Node.id.in_(node_ids)).all()}
    nodes = [by_id[nid] for nid in node_ids if nid in by_id]
    return cluster, nodes


@router.get("/{cluster_id}/metrics")
def cluster_metrics(
    cluster_id: int,
    from_ts: float | None = None,
    to_ts: float | None = None,
    limit: int = 2000,
    db: Session = Depends(get_db),
):
    """集群级监控数据：每个成员节点 -> 降采样指标序列（前端并排对比迷你图）。

    默认最近 1 小时；字段仅取图表所需，避免全量 JSON 传输。
    """
    cluster, nodes = _cluster_nodes(db, cluster_id)
    now = time.time()
    to = to_ts if to_ts else now
    frm = from_ts if from_ts else to - 3600
    if not nodes:
        return {"members": []}
    node_ids = [n.id for n in nodes]
    rows = (
        db.query(MetricSample)
        .filter(MetricSample.node_id.in_(node_ids),
                MetricSample.ts >= frm, MetricSample.ts <= to)
        .order_by(MetricSample.ts)
        .all()
    )
    buckets: dict[int, list[MetricSample]] = {}
    for r in rows:
        buckets.setdefault(r.node_id, []).append(r)
    members = []
    for n in nodes:
        series = buckets.get(n.id, [])
        if limit > 0 and len(series) > limit:
            step = len(series) / limit
            series = [series[int(i * step)] for i in range(limit)]
        # 角色属任务级（head/worker 每次任务不同），不在集群级监控里展示
        members.append({
            "node_id": n.id,
            "node_name": n.name,
            "agent_status": n.agent_status,
            "series": [{
                "ts": r.ts,
                "cpu": (r.data or {}).get("cpu_percent"),
                "mem_percent": ((r.data or {}).get("memory") or {}).get("percent"),
                "gpu_util": ((r.data or {}).get("gpu") or {}).get("utilization"),
                "gpu_mem_used": ((r.data or {}).get("gpu") or {}).get("mem_used"),
                "gpu_mem_total": ((r.data or {}).get("gpu") or {}).get("mem_total"),
                "temp": ((r.data or {}).get("temperatures") or {}).get("cpu"),
                "gpu_temp": _first_gpu_temp((r.data or {}).get("gpus") or []),
                "net_rx": ((r.data or {}).get("network") or {}).get("rx_bps"),
                "net_tx": ((r.data or {}).get("network") or {}).get("tx_bps"),
            } for r in series],
        })
    return {"members": members}


def _first_gpu_temp(gpus: list) -> float | None:
    if not gpus:
        return None
    t = (gpus[0] or {}).get("temperature")
    return t


@router.get("/{cluster_id}/overview")
def cluster_overview(cluster_id: int, db: Session = Depends(get_db)):
    """集群汇总：在线数、平均 GPU 利用率/温度、显存汇总、网络总吞吐。"""
    cluster, nodes = _cluster_nodes(db, cluster_id)
    online = [n for n in nodes if n.agent_status == "online"]
    util_sum = 0.0
    temp_sum = 0.0
    temp_count = 0
    mem_used = mem_total = 0
    net_rx = net_tx = 0
    for n in online:
        latest = (
            db.query(MetricSample)
            .filter(MetricSample.node_id == n.id)
            .order_by(MetricSample.ts.desc())
            .first()
        )
        if not latest:
            continue
        d = latest.data or {}
        g = d.get("gpu") or {}
        if g.get("utilization") is not None:
            util_sum += g["utilization"]
        mem_used += g.get("mem_used", 0)
        mem_total += g.get("mem_total", 0)
        t = d.get("temperatures") or {}
        if t.get("cpu") is not None:
            temp_sum += t["cpu"]
            temp_count += 1
        net = d.get("network") or {}
        net_rx += net.get("rx_bps", 0)
        net_tx += net.get("tx_bps", 0)
    return {
        "name": cluster.name,
        "nodes_total": len(nodes),
        "nodes_online": len(online),
        "gpu_util_avg": round(util_sum / len(online), 1) if online else None,
        "cpu_temp_avg": round(temp_sum / temp_count, 1) if temp_count else None,
        "gpu_mem_used": mem_used,
        "gpu_mem_total": mem_total,
        "net_rx_bps": net_rx,
        "net_tx_bps": net_tx,
    }
