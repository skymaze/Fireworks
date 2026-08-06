"""集群管理：CRUD、成员（head/worker）管理、集群参数、网络测试。"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..models import Cluster, ClusterNode, Node, Task
from ..services import network_config as network_config_svc
from ..services import network_test as network_test_svc
from ..services import recipe_render
from ..services import ssh_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clusters", tags=["clusters"])


def get_cluster_or_404(db: Session, cluster_id: int) -> Cluster:
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404, "集群不存在")
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


def _setup_cluster_trust(db: Session, cluster: Cluster, head_node_id: int) -> None:
    """配置 head → 各成员 SSH 免密（镜像/模型 RoCE rsync 依赖）。

    幂等（deploy_agent.ensure_ssh_trust 会剔除旧条目）；失败仅警告（写入
    服务端日志）不阻断。
    """
    from ..services.deploy_agent import ensure_ssh_trust

    head = db.get(Node, head_node_id)
    if not head:
        return
    for m in cluster.members:
        if m.node_id == head_node_id:
            continue
        node = db.get(Node, m.node_id)
        if not node:
            continue
        try:
            ok, msg = ensure_ssh_trust(head, node)
            if not ok:
                logger.warning("%s→%s 免密配置失败：%s", head.name, node.name, msg)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s→%s 免密配置异常：%s", head.name, node.name, e)


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
        raise HTTPException(400, f"网段格式错误：{base_cidr}（{e}）") from e
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
    raise HTTPException(409, "10.x 高速网网段均已被占用，无可用网段，请手动设置其他网段")


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
                raise HTTPException(404, f"节点 {nid} 不存在")
            ok, msg = network_config_svc.apply_node_network(node, plan, i)
            if not ok:
                raise HTTPException(400, f"节点 {node.name} 高速网络配置失败：{msg}")
            applied.append((node, i))
        time.sleep(8)  # 等 NM 完成重配、SSH 恢复稳定
        for node, i in applied:
            peers = [(n, idx) for n, idx in applied if n.id != node.id]
            ok, detail = network_config_svc.verify_node_network(node, plan, i, peers)
            if not ok:
                raise HTTPException(400, f"节点 {node.name} 高速网络验证失败（已回滚）：{detail}")
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
        raise HTTPException(409, "同名集群已存在")
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
            raise HTTPException(409, f"以下节点已加入其他集群，不可重复加入：{names}")
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
                raise HTTPException(
                    409,
                    f"网段 {req.network_cidr} 已被集群「{clash.name}」占用，请改用可用网段（如 {free}）",
                )
    # 高速网络配置（创建时即配置成员节点，测试通过才落库；失败自动回滚）
    plan, applied = _configure_cluster_network(db, req.node_ids, req.network_cidr, req.network_mtu)
    cluster = Cluster(
        name=req.name,
        description=req.description,
        network_type=req.network_type,
        master_port=req.master_port,
        network_cidr=req.network_cidr,
        network_mtu=req.network_mtu,
        network_plan=plan,
    )
    db.add(cluster)
    db.commit()
    db.refresh(cluster)
    try:
        # 初始成员（第一个为 head rank0，其余 worker 按序）；原子占用 cluster_id（一节点一集群）
        for i, nid in enumerate(req.node_ids):
            claim = (
                db.query(Node)
                .filter(Node.id == nid, Node.cluster_id.is_(None))
                .update({Node.cluster_id: cluster.id})
            )
            if not claim:
                raise HTTPException(409, f"节点 {nid} 已被其他集群占用，创建已取消")
            db.add(
                ClusterNode(
                    cluster_id=cluster.id,
                    node_id=nid,
                    role="head" if i == 0 else "worker",
                    node_rank=i,
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
    # head → 各成员 SSH 免密（镜像/模型 RoCE 分发依赖；失败仅警告）
    if req.node_ids:
        _setup_cluster_trust(db, cluster, req.node_ids[0])
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


def _cleanup_node_roce_network(node: Node, cidr_prefix: str = "10.100") -> tuple[bool, str]:
    """从节点移除集群高速网络配置（cidr_prefix 网段地址的接口），保留管理口/路由。

    通过 SSH + sudo（sudo 密码 = 节点 SSH 密码）执行：
    脚本以 base64 落到 /tmp（避免 heredoc 与 sudo -S 的 stdin 冲突）→
    备份 netplan -> PyYAML 移除含 cidr_prefix 地址的 ethernets 条目 -> netplan apply。
    返回 (ok, 说明/错误)。
    """
    if not node.ssh_password:
        return False, "节点未保存 SSH 密码，无法提权清理"
    import base64

    script = (
        "import yaml, glob, shutil, time\n"
        f"PREFIX = '{cidr_prefix}.'\n"
        "cleaned = []\n"
        "for f in glob.glob('/etc/netplan/*.yaml'):\n"
        "    txt = open(f).read()\n"
        "    if PREFIX not in txt:\n"
        "        continue\n"
        "    shutil.copy(f, f + '.bak-' + time.strftime('%Y%m%d%H%M%S'))\n"
        "    data = yaml.safe_load(txt) or {}\n"
        "    eth = (data.get('network') or {}).get('ethernets') or {}\n"
        "    removed = []\n"
        "    for name in list(eth.keys()):\n"
        "        addrs = eth[name].get('addresses') or []\n"
        "        if any(isinstance(a, str) and a.startswith(PREFIX) for a in addrs):\n"
        "            del eth[name]\n"
        "            removed.append(name)\n"
        "    if removed:\n"
        "        with open(f, 'w') as fh:\n"
        "            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)\n"
        "        cleaned.append(f + ':' + ','.join(removed))\n"
        "print('CLEANED ' + ';'.join(cleaned))\n"
        "print('DONE')\n"
    )
    b64 = base64.b64encode(script.encode()).decode()
    client = ssh_client.connect(node, timeout=20)
    try:
        ssh_client.exec(client, f"echo {b64} | base64 -d > /tmp/fw_cleanup_roce.py", timeout=15)
        out, err, _ = ssh_client.exec(
            client,
            f"printf '%s\\n' '{node.ssh_password}' | sudo -S python3 /tmp/fw_cleanup_roce.py 2>&1; "
            f"printf '%s\\n' '{node.ssh_password}' | sudo -S netplan apply 2>&1 | tail -5; echo APPLY_DONE",
            timeout=240,
        )
        text = (out or "") + (err or "")
        if "DONE" in text and "APPLY_DONE" in text:
            return True, "已清理节点高速网络配置（netplan 已备份 .bak-*）"
        return False, text[-300:] or "清理脚本未返回预期结果"
    finally:
        client.close()


@router.delete("/{cluster_id}")
def delete_cluster(
    cluster_id: int,
    force: bool = Query(False, description="集群下存在已结束任务时仍删除（任务将失去集群引用）；运行中/已发布/暂停任务须先停止，无论 force 均拒绝"),
    cleanup_network: bool = Query(False, description="同时清理成员节点上的集群高速网络配置（10.100.x）"),
    db: Session = Depends(get_db),
):
    cluster = get_cluster_or_404(db, cluster_id)
    tasks = db.query(Task).filter(Task.cluster_id == cluster_id).all()
    # 运行中/已发布/暂停的任务（容器仍在节点上）：无论 force 都拒绝，须先停止任务
    ACTIVE_STATUSES = ("published", "running", "paused")
    active_tasks = [t for t in tasks if t.status in ACTIVE_STATUSES]
    if active_tasks:
        names = ", ".join(f"#{t.id} {t.name}（{t.status}）" for t in active_tasks)
        raise HTTPException(
            409,
            f"集群下存在未停止的任务（{names}）。请先在任务详情停止后再删除集群，"
            "避免节点容器失去管理",
        )
    if tasks and not force:
        names = ", ".join(f"#{t.id} {t.name}" for t in tasks)
        raise HTTPException(
            409,
            f"集群下存在历史任务（{names}）。请先删除任务，或确认 force 删除（任务将失去集群引用）",
        )

    cleaned_nodes: list[str] = []
    warnings: list[str] = []
    if cleanup_network:
        # 清理网段前缀：从集群 network_cidr 推导（前两段；缺省 10.100）
        cidr_prefix = "10.100"
        if cluster.network_cidr:
            try:
                import ipaddress

                cidr_prefix = ".".join(str(ipaddress.ip_network(cluster.network_cidr, strict=False).network_address).split(".")[:2])
            except ValueError:
                pass
        for m in cluster.members:
            node = db.get(Node, m.node_id)
            if not node:
                continue
            try:
                ok, msg = _cleanup_node_roce_network(node, cidr_prefix)
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
        raise HTTPException(404, "节点不存在")
    if any(m.node_id == req.node_id for m in cluster.members):
        raise HTTPException(409, "该节点已在集群中")
    if any(m.node_rank == req.node_rank for m in cluster.members):
        raise HTTPException(409, f"node_rank {req.node_rank} 已被其他成员占用")
    # 一节点一集群：原子占用（仅 cluster_id 为空才可更新；并发/重复加入在此拦截）
    claim = (
        db.query(Node)
        .filter(Node.id == req.node_id, Node.cluster_id.is_(None))
        .update({Node.cluster_id: cluster_id})
    )
    if not claim:
        other = db.get(Cluster, node.cluster_id) if node.cluster_id else None
        raise HTTPException(
            409,
            f"节点「{node.name}」已加入集群「{other.name if other else '#' + str(node.cluster_id)}」，一个节点只能属于一个集群",
        )
    db.commit()
    db.refresh(node)

    # 按集群保存的高速网络规划配置新节点（同一接口同网段，序号=成员数+1），
    # 验证通过才加入；失败释放 cluster_id 并回滚网络。
    def _release_claim():
        node.cluster_id = None
        db.commit()

    if req.configure_network and cluster.network_plan:
        plan = cluster.network_plan
        # plan 序号与 node_rank 严格一致（rank+1）；勿用 len(members)+1，
        # 否则 node_rank 不连续（如 rank=5）时分配的 IP 与 plan/验证/网络测试不一致
        index = req.node_rank + 1
        ok, msg = network_config_svc.apply_node_network(node, plan, index)
        if not ok:
            _release_claim()
            raise HTTPException(400, f"节点 {node.name} 高速网络配置失败：{msg}")
        # 现有成员的 plan 序号 = node_rank + 1（创建时 rank 与 apply 序号一致）
        members_sorted = sorted(cluster.members, key=lambda m: m.node_rank)
        peers: list[tuple[Node, int]] = []
        for m in members_sorted:
            mn = db.get(Node, m.node_id)
            if mn:
                peers.append((mn, m.node_rank + 1))
        ok, detail = network_config_svc.verify_node_network(node, plan, index, peers)
        if not ok:
            try:
                network_config_svc.rollback_node_network(node)
            except Exception:  # noqa: BLE001
                pass
            _release_claim()
            raise HTTPException(400, f"节点 {node.name} 高速网络验证失败，已回滚：{detail}")

    # 若设为 head，则取消其他 head
    if req.role == "head":
        for m in cluster.members:
            if m.role == "head":
                m.role = "worker"
    db.add(ClusterNode(cluster_id=cluster_id, node_id=req.node_id, role=req.role, node_rank=req.node_rank))
    db.commit()
    db.refresh(cluster)
    # 配置 head → 新成员免密（新节点为 head 时重配全部成员方向）
    head_member = next((m for m in cluster.members if m.role == "head"), None)
    if head_member:
        _setup_cluster_trust(db, cluster, head_member.node_id)
    return cluster


@router.patch("/{cluster_id}/nodes/{node_id}", response_model=schemas.ClusterOut)
def update_cluster_node(cluster_id: int, node_id: int, req: schemas.ClusterNodeUpdate, db: Session = Depends(get_db)):
    cluster = get_cluster_or_404(db, cluster_id)
    link = db.query(ClusterNode).filter_by(cluster_id=cluster_id, node_id=node_id).first()
    if not link:
        raise HTTPException(404, "节点不在集群中")
    # 已配置高速网络的集群：node_rank 决定 plan IP 分配，直接修改会使
    # plan/实际配置不一致（master_addr 等自动变量错误）→ 拒绝并提示走移除+重加
    if req.node_rank is not None and req.node_rank != link.node_rank and cluster.network_plan:
        raise HTTPException(
            409,
            "该集群已配置高速网络，node_rank 对应已分配的 plan IP，不可直接修改；"
            "请先移除该节点再按新 rank 重新添加（将自动重新配置网络）",
        )
    if req.role == "head":
        for m in cluster.members:
            if m.role == "head" and m.node_id != node_id:
                m.role = "worker"
    if req.role is not None:
        link.role = req.role
    if req.node_rank is not None:
        # 非网络集群也校验 rank 不重复
        dup = (
            db.query(ClusterNode)
            .filter(
                ClusterNode.cluster_id == cluster_id,
                ClusterNode.node_rank == req.node_rank,
                ClusterNode.node_id != node_id,
            )
            .first()
        )
        if dup:
            raise HTTPException(409, f"node_rank {req.node_rank} 已被其他成员占用")
        link.node_rank = req.node_rank
    db.commit()
    db.refresh(cluster)
    return cluster


@router.delete("/{cluster_id}/nodes/{node_id}", response_model=schemas.ClusterOut)
def remove_cluster_node(cluster_id: int, node_id: int, db: Session = Depends(get_db)):
    cluster = get_cluster_or_404(db, cluster_id)
    link = db.query(ClusterNode).filter_by(cluster_id=cluster_id, node_id=node_id).first()
    if not link:
        raise HTTPException(404, "节点不在集群中")
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
    """集群发布预览：cluster 变量 + 各节点自动变量（供发布向导自动填充）。"""
    cluster = get_cluster_or_404(db, cluster_id)
    members = sorted(cluster.members, key=lambda m: m.node_rank)
    assignments = []
    nodes_out = []
    # 集群已配置高速网络时，node_roce_ip 以 plan 分配的 IP 为准（权威来源），
    # 避免 agent 上报的硬件缓存滞后于最新配置
    plan = cluster.network_plan or {}
    for m in members:
        node = db.get(Node, m.node_id)
        if not node:
            continue
        assignments.append((node, m.role, m.node_rank))
        auto_vars = recipe_render.node_auto_vars(node, m.role, m.node_rank)
        if plan and "iface_subnets" in plan:
            try:
                plan_ips = network_config_svc.node_ips(plan, m.node_rank + 1)
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
                "role": m.role,
                "node_rank": m.node_rank,
                "agent_status": node.agent_status,
                "auto_vars": auto_vars,
            }
        )
    cluster_vars = recipe_render.cluster_auto_vars(cluster, assignments)
    # 有网络规划时 master_addr 用 head 的 plan IP（权威来源）
    if plan and "iface_subnets" in plan:
        try:
            head_node = next((n for n, role, _ in assignments if role == "head"), None)
            if head_node is not None:
                head_rank = next((r for n, _, r in assignments if n.id == head_node.id), 0)
                cluster_vars["master_addr"] = network_config_svc.node_ips(plan, head_rank + 1)["enp1s0f0np0"]
        except Exception:  # noqa: BLE001
            pass
    # 分布式协调约定：MASTER_ADDR 需指向 rank0 节点；head 非 rank0 时跨节点初始化会失败
    warnings: list[str] = []
    head_member = next((m for m in cluster.members if m.role == "head"), None)
    if head_member is None:
        warnings.append("集群尚未设置 head 节点，发布任务时请选择 Head 节点")
    elif head_member.node_rank != 0:
        warnings.append(
            f"head 节点（{head_member.node_id}）当前 node_rank={head_member.node_rank}，"
            "分布式初始化要求 MASTER_ADDR 指向 rank0。若 head 非 rank0，跨节点平台可能握手超时，"
            "建议把 head 节点设为 rank 0"
        )
    return {"cluster_vars": cluster_vars, "nodes": nodes_out, "warnings": warnings}


@router.post("/{cluster_id}/network-test")
async def cluster_network_test(cluster_id: int, req: schemas.NetworkTestRequest, db: Session = Depends(get_db)):
    """在集群内两个节点间运行网络测试（iperf3 / ib_write_bw / ib_read_bw / ping）。

    高速网 IP 优先取集群 network_plan 中该节点的接口 IP（权威来源），
    避免 agent 上报的硬件信息缓存滞后于最新配置。
    """
    cluster = get_cluster_or_404(db, cluster_id)
    member_ids = {m.node_id for m in cluster.members}
    if req.from_node_id not in member_ids or req.to_node_id not in member_ids:
        raise HTTPException(400, "测试的两个节点必须在集群中")
    from_node = db.get(Node, req.from_node_id)
    to_node = db.get(Node, req.to_node_id)

    # 集群 plan 中节点序号 = node_rank + 1（创建/加节点时按此分配 IP）
    rank_map = {m.node_id: m.node_rank for m in cluster.members}
    plan = cluster.network_plan or {}
    roce_override: str | None = None
    if plan and from_node.id in rank_map and "iface_subnets" in plan:
        from_index = rank_map[from_node.id] + 1
        try:
            plan_ips = network_config_svc.node_ips(plan, from_index)
            # RDMA 目标 IP 跟随该节点勾选后的 primary netdev（与 NCCL_IB_HCA 一致）
            from_vars = recipe_render.node_auto_vars(from_node, "worker", rank_map[from_node.id])
            iface = from_vars.get("netdev")
            roce_override = plan_ips.get(iface) or plan_ips.get("enp1s0f0np0")
        except Exception:  # noqa: BLE001 - 取不到时回退默认
            pass

    return await network_test_svc.run_network_test(
        from_node, to_node, req.tool, req.duration, req.ib_device, roce_ip_override=roce_override
    )
