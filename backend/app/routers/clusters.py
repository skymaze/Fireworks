"""集群管理：CRUD、成员（head/worker）管理、集群参数、网络测试。"""

import asyncio
import concurrent.futures
import copy
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, sessionmaker

from .. import schemas
from ..db import get_db
from ..errors import Code, api_error
from ..models import (
    Cluster,
    ClusterNode,
    InferenceSample,
    MetricSample,
    Node,
    Task,
    TaskBenchmark,
    TaskNode,
)
from ..services import agent_client, agent_ws
from ..services import network_config as network_config_svc
from ..services import network_test as network_test_svc
from ..services import node_info, recipe_render
from ..services import task_runtime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clusters", tags=["clusters"])


def _merge_planned_network_snapshot(node: Node, plan: dict, index: int) -> None:
    """在完整 Agent 刷新前先把已验证的规划 IP 合入硬件快照，避免旧地址留库。"""
    hw = copy.deepcopy(node.hardware_info or {})
    planned = network_config_svc.node_ips(plan, index)
    interfaces = list(hw.get("interfaces") or [])
    by_name = {item.get("name"): item for item in interfaces}
    for iface, ip in planned.items():
        item = by_name.get(iface)
        if item is None:
            item = {"name": iface}
            interfaces.append(item)
            by_name[iface] = item
        item["ipv4"] = [ip]
        item["up"] = True
    hw["interfaces"] = interfaces
    roce = list(hw.get("roce") or [])
    for item in roce:
        ip = planned.get(item.get("netdev"))
        if ip:
            item["ipv4"] = [ip]
            item["rocev2_ip"] = ip
    hw["roce"] = roce
    node.hardware_info = hw


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


def find_available_cidr(
    db: Session,
    base_cidr: str,
    exclude_cluster_id: int | None = None,
    max_tries: int | None = None,
    extra_used_cidrs: list[str] | None = None,
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
    for cidr in extra_used_cidrs or []:
        try:
            used.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
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


def _ensure_cidr_available(db: Session, cidr: str) -> None:
    """校验最终实际使用的网段，而不是表单提交但可能被现网检测替换的网段。"""
    free = find_available_cidr(db, cidr)
    if free == cidr:
        return
    clash = next(
        (
            cluster
            for cluster in db.query(Cluster).filter(Cluster.network_cidr.isnot(None))
            if cluster.network_cidr and _cidr_overlap(cidr, cluster.network_cidr)
        ),
        None,
    )
    name = clash.name if clash else "unknown"
    raise api_error(
        409,
        Code.CIDR_CONFLICT,
        f"网段 {cidr} 已被集群「{name}」占用，请改用可用网段（如 {free}）",
        params={"cidr": cidr, "name": name, "free": free},
    )


def _find_arp_free_plan(
    db: Session,
    nodes: list[Node],
    snapshots: dict[int, dict[str, dict]],
    node_indices: dict[int, int],
    base_cidr: str,
    mtu: int,
    extra_used_cidrs: list[str] | None = None,
    max_candidates: int = 16,
) -> tuple[dict, list[dict]]:
    """同时避开数据库网段与交换机广播域中的在用 IP，自动向后寻找候选网段。"""
    excluded = list(extra_used_cidrs or [])
    last_conflicts: list[dict] = []
    candidate_base = base_cidr
    for _ in range(max_candidates):
        candidate = find_available_cidr(
            db, candidate_base, extra_used_cidrs=excluded
        )
        plan = network_config_svc.plan_cluster_network(candidate, mtu)
        conflicts = network_config_svc.probe_plan_ip_conflicts(
            nodes, plan, node_indices, snapshots
        )
        if not conflicts:
            return plan, []
        last_conflicts = conflicts
        excluded.append(candidate)
        candidate_base = candidate
    return network_config_svc.plan_cluster_network(candidate_base, mtu), last_conflicts


def _configure_cluster_network(
    db: Session, node_ids: list[int], cidr: str | None, mtu: int | None
) -> tuple[dict | None, list[tuple[Node, int]], dict[int, int]]:
    """为成员节点配置高速网络并验证；任一失败回滚已应用节点并抛 400。

    始终按用户提供的网段规划并配置（未提供时自动找空闲网段）；不再自动复用
    节点现有网络——现网检测不可靠，且用户网段意图优先。流程：
    物理链路预检 -> 计划 IP 占用检测（冲突时 409 携带建议网段，由前端提示
    用户更换）-> 逐节点 apply + 验证，失败自动回滚。
    返回 (plan, [(本次实际修改的 node, index)], {node_id: net_index})。
    """
    if not node_ids:
        return None, [], {}
    nodes = [db.get(Node, nid) for nid in node_ids]
    missing = [nid for nid, node in zip(node_ids, nodes, strict=True) if node is None]
    if missing:
        raise api_error(404, Code.NODE_NOT_FOUND, f"节点 {missing[0]} 不存在",
                        params={"id": missing[0]})
    if not cidr:
        cidr = find_available_cidr(db, "10.0.0.0/16")

    applied: list[tuple[Node, int]] = []
    try:
        snapshots = network_config_svc.inspect_nodes_network(nodes)
        physical = network_config_svc.probe_cluster_physical_links(nodes, snapshots)
        if not physical["ok"]:
            detail = "；".join(physical["issues"])
            raise api_error(
                400,
                Code.NETWORK_PHYSICAL_LINK_FAILED,
                f"高速网络物理链路预检失败：{detail}",
                details=detail,
            )
        try:
            plan = network_config_svc.plan_cluster_network(cidr, mtu or 9000)
        except ValueError as e:
            raise api_error(400, Code.CIDR_FORMAT_ERROR, str(e), details=str(e)) from e
        _ensure_cidr_available(db, plan["cidr"])
        node_indices = {node.id: i for i, node in enumerate(nodes, start=1)}
        conflicts = network_config_svc.probe_plan_ip_conflicts(
            nodes, plan, node_indices, snapshots
        )
        if conflicts:
            detail = "；".join(
                f"{item['node']} {item['iface']} 的 {item['ip']}：{item['reason']}"
                + (f"（MAC {item['observed_mac']}）" if item.get("observed_mac") else "")
                for item in conflicts
            )
            suggested_plan, suggested_conflicts = _find_arp_free_plan(
                db, nodes, snapshots, node_indices, plan["cidr"], plan["mtu"],
                extra_used_cidrs=[plan["cidr"]],
            )
            suggested = suggested_plan["cidr"] if not suggested_conflicts else ""
            raise api_error(
                409,
                Code.NETWORK_IP_CONFLICT,
                f"计划分配的高速网 IP 已被占用：{detail}。"
                + (f"已找到可用网段 {suggested}" if suggested else "未能自动找到无冲突网段"),
                params={"suggested": suggested},
                details=detail,
            )
        def apply_one(node: Node):
            return node, node_indices[node.id], network_config_svc.apply_node_network(
                node, plan, node_indices[node.id]
            )

        apply_errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
            futures = [pool.submit(apply_one, node) for node in nodes]
            for future in futures:
                try:
                    node, i, (ok, msg) = future.result()
                    if ok:
                        applied.append((node, i))
                    else:
                        apply_errors.append((node, msg))
                except Exception as exc:  # noqa: BLE001
                    apply_errors.append((None, str(exc)))
        if apply_errors:
            node, msg = apply_errors[0]
            name = node.name if node else "unknown"
            raise api_error(400, Code.NETWORK_CONFIGURE_FAILED,
                            f"节点 {name} 高速网络配置失败：{msg}",
                            params={"name": name}, details=msg)

        def verify_applied(item: tuple[Node, int]):
            node, i = item
            peers = [(n, idx) for n, idx in applied if n.id != node.id]
            return node, i, network_config_svc.verify_node_network(node, plan, i, peers)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(applied))) as pool:
            verified = list(pool.map(verify_applied, applied))
        for node, i, (ok, detail) in verified:
            if not ok:
                raise api_error(400, Code.NETWORK_VERIFY_FAILED_ROLLBACK,
                                f"节点 {node.name} 高速网络验证失败（已回滚）：{detail}",
                                params={"name": node.name}, details=detail)
            _merge_planned_network_snapshot(node, plan, i)
    except Exception as exc:
        for node, _ in reversed(applied):
            try:
                network_config_svc.rollback_node_network(node)
            except Exception:  # noqa: BLE001
                pass
        if isinstance(exc, HTTPException):
            raise
        raise api_error(
            400,
            Code.NETWORK_CONFIGURE_FAILED,
            f"高速网络配置异常：{exc}",
            details=str(exc),
        ) from exc
    return plan, applied, node_indices


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


def _create_cluster_locked(req: schemas.ClusterCreate, db: Session):
    if db.query(Cluster).filter(Cluster.name == req.name).first():
        raise api_error(409, Code.CLUSTER_NAME_EXISTS, "同名集群已存在")
    if len(req.node_ids) != len(set(req.node_ids)):
        raise api_error(400, Code.NODE_ALREADY_IN_CLUSTER, "成员节点不可重复")
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
    # 高速网络配置（创建时即配置成员节点，测试通过才落库；失败自动回滚）
    plan, applied, node_indices = _configure_cluster_network(
        db, req.node_ids, req.network_cidr, req.network_mtu
    )
    cluster = Cluster(
        name=req.name,
        description=req.description,
        network_type=req.network_type,
        network_cidr=plan["cidr"] if plan else req.network_cidr,
        network_mtu=plan["mtu"] if plan else req.network_mtu,
        network_plan=plan,
    )
    db.add(cluster)
    try:
        # 集群与成员占用必须处于同一事务：先 flush 获取 cluster.id，全部节点
        # claim/成员行成功后再一次性提交，失败时 rollback 不留下空集群。
        db.flush()
        # 初始成员：net_index 按成员序号 1 起（与高速网 plan 分配索引一致）；
        # head/worker/rank 属任务级，随任务发布指定，不在集群成员上保存。
        # 原子占用 cluster_id（一节点一集群）
        for nid in req.node_ids:
            i = node_indices[nid]
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
    except Exception:
        # 数据库回滚 + 节点网络回滚（配置已 apply，避免残留）
        db.rollback()
        for node, _ in reversed(applied):
            try:
                network_config_svc.rollback_node_network(node)
            except Exception:  # noqa: BLE001
                pass
        raise
    db.refresh(cluster)
    return cluster


def _create_cluster_with_locks(req: schemas.ClusterCreate, db: Session) -> int:
    locks = network_config_svc.acquire_operation_locks(req.node_ids)
    try:
        # 等待并发配置锁后必须重新执行占用检查；否则另一个请求可能已完成 claim。
        return _create_cluster_locked(req, db).id
    finally:
        network_config_svc.release_operation_locks(locks)


def _create_cluster_in_worker(req: schemas.ClusterCreate, bind) -> int:
    """在线程内使用独立 Session，避免请求 Session 跨线程访问。"""
    worker_db = sessionmaker(bind=bind, autocommit=False, autoflush=False)()
    try:
        return _create_cluster_with_locks(req, worker_db)
    finally:
        worker_db.close()


@router.post("", response_model=schemas.ClusterOut, status_code=201)
async def create_cluster(req: schemas.ClusterCreate, db: Session = Depends(get_db)):
    # SSH/Netplan/ARP 都是阻塞操作，放在线程中避免一次集群创建卡住整个 API 事件循环。
    cluster_id = await asyncio.to_thread(_create_cluster_in_worker, req, db.get_bind())
    db.expire_all()
    nodes = [db.get(Node, node_id) for node_id in req.node_ids]
    failures = await node_info.refresh_nodes_best_effort(
        db, [node for node in nodes if node is not None], retry=False
    )
    if failures:
        logger.warning("集群 %s 创建后节点信息部分刷新失败: %s", cluster_id, "; ".join(failures))
    db.expire_all()
    return get_cluster_or_404(db, cluster_id)


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
    cleanup_network: bool = Query(False, description="同时清理成员节点上本项目写入的高速网络配置（删除本项目 999 文件并还原节点）"),
    db: Session = Depends(get_db),
):
    """删除集群并清理关联数据。

    未停止的任务（published/running/paused，容器仍在节点上）须先停止；
    已结束任务及其关联数据（task_nodes / 推理统计 / 压测记录）随集群一并清理，
    不再保留失去集群引用的孤儿任务。
    """
    cluster = get_cluster_or_404(db, cluster_id)
    tasks = db.query(Task).filter(Task.cluster_id == cluster_id).all()
    # 运行中/已发布/暂停的任务（容器仍在节点上）：须先停止任务
    ACTIVE_STATUSES = ("published", "running", "paused")
    active_tasks = [t for t in tasks if t.status in ACTIVE_STATUSES]
    if active_tasks:
        names = ", ".join(f"#{t.id} {t.name}（{t.status}）" for t in active_tasks)
        raise api_error(409, Code.CLUSTER_HAS_RUNNING_TASKS,
                        f"集群下存在未停止的任务（{names}）。请先在任务详情停止后再删除集群，"
                        "避免节点容器失去管理", params={"names": names})

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

    # 清理关联任务及其历史数据：先尽力停止残留容器（正常流程任务已停止），
    # 再显式清理任务域数据（SQLite 未启用外键级联，避免孤儿记录在新任务复用
    # 相同 id 时被误认为新任务的历史数据），删除失败仅告警不阻断集群删除。
    deleted_tasks = 0
    deleted_task_ids: list[int] = []
    for task in tasks:
        for tn in task.nodes:
            node = db.get(Node, tn.node_id)
            if not node:
                continue
            try:
                asyncio.run(agent_client.compose_down(node, task.name))
            except Exception as e:  # noqa: BLE001
                warnings.append(f"任务 #{task.id} 停止容器失败（节点 {tn.node_id}）: {e}")
        try:
            locked_task = task_runtime.lock_task_for_write(db, task.id)
            if locked_task is None:
                warnings.append(f"任务 #{task.id} 已被删除或状态已变更，跳过")
                continue
            db.query(InferenceSample).filter(
                InferenceSample.task_id == task.id
            ).delete(synchronize_session=False)
            db.query(TaskBenchmark).filter(
                TaskBenchmark.task_id == task.id
            ).delete(synchronize_session=False)
            db.delete(task)
            db.commit()
            deleted_tasks += 1
            deleted_task_ids.append(task.id)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            warnings.append(f"任务 #{task.id} 删除失败: {e}")

    # 释放成员节点占用（一节点一集群）
    db.query(Node).filter(Node.cluster_id == cluster_id).update({Node.cluster_id: None})
    db.delete(cluster)
    db.commit()
    for task_id in deleted_task_ids:
        agent_ws.broadcast({"type": "task_deleted", "task_id": task_id})
    return {
        "ok": True,
        "deleted_tasks": deleted_tasks,
        "cleaned_nodes": cleaned_nodes,
        "warnings": warnings,
    }


def _preflight_add_node(cluster: Cluster, node: Node, configure_network: bool, db: Session) -> dict:
    """添加成员的只读预检；WebUI 预览和最终提交共用同一实现。"""
    # 高速网槽位：追加分配（max+1），移除不复用，既有成员的 plan IP 保持稳定
    net_index = max((m.net_index for m in cluster.members), default=0) + 1
    peers: list[tuple[Node, int]] = []
    for member in cluster.members:
        member_node = db.get(Node, member.node_id)
        if member_node:
            peers.append((member_node, member.net_index))

    # 所有破坏性动作前完成物理 rail 与目标 IP 预检。节点当前地址可不同：ARP Probe
    # 直接验证同一二层广播域；没有当前地址时允许 carrier-only，配置后再严格验证。
    plan = cluster.network_plan if configure_network else None
    already_configured = False
    if plan:
        topology_nodes = [peer for peer, _ in peers] + [node]
        snapshots = network_config_svc.inspect_nodes_network(topology_nodes)
        physical = network_config_svc.probe_cluster_physical_links(topology_nodes, snapshots)
        if not physical["ok"]:
            detail = "；".join(physical["issues"])
            raise api_error(
                400, Code.NETWORK_PHYSICAL_LINK_FAILED,
                f"节点 {node.name} 无法加入：高速网络物理链路预检失败：{detail}",
                params={"name": node.name}, details=detail,
            )
        # 若追加槽位的某个 IP 已被交换机内其它设备占用，自动继续尝试后续槽位，
        # 不要求用户换整个集群网段或手写节点网络。
        indices = {peer.id: index for peer, index in peers}
        conflicts: list[dict] = []
        for _ in range(16):
            indices[node.id] = net_index
            conflicts = network_config_svc.probe_plan_ip_conflicts(
                topology_nodes, plan, indices, snapshots
            )
            new_node_conflicts = [item for item in conflicts if item["node"] == node.name]
            if not new_node_conflicts:
                conflicts = [item for item in conflicts if item["node"] != node.name]
                break
            net_index += 1
        if conflicts:
            detail = "；".join(
                f"{item['node']} {item['iface']} 的 {item['ip']}：{item['reason']}"
                + (f"（MAC {item['observed_mac']}）" if item.get("observed_mac") else "")
                for item in conflicts
            )
            raise api_error(
                409, Code.NETWORK_IP_CONFLICT,
                f"节点 {node.name} 计划使用的高速网 IP 已被占用：{detail}",
                params={"name": node.name}, details=detail,
            )
        profile = network_config_svc._detect_snapshot_network(snapshots[node.id])
        already_configured = bool(
            profile and profile["plan"] == plan and profile["index"] == net_index
        )
    return {
        "net_index": net_index,
        "plan": plan,
        "peers": peers,
        "physical": physical if plan else None,
        "conflicts": conflicts if plan else [],
        "already_configured": already_configured,
    }


@router.post("/{cluster_id}/nodes/preflight")
def preflight_cluster_node(
    cluster_id: int, req: schemas.ClusterNodeAdd, db: Session = Depends(get_db)
):
    cluster = get_cluster_or_404(db, cluster_id)
    node = db.get(Node, req.node_id)
    if not node:
        raise api_error(404, Code.NODE_NOT_FOUND, "节点不存在")
    if any(member.node_id == req.node_id for member in cluster.members):
        raise api_error(409, Code.NODE_ALREADY_IN_CLUSTER, "该节点已在集群中")
    if node.cluster_id is not None:
        raise api_error(409, Code.NODE_BELONGS_OTHER,
                        f"节点「{node.name}」已加入其它集群", params={"name": node.name})
    result = _preflight_add_node(cluster, node, req.configure_network, db)
    return {
        "ok": True,
        "net_index": result["net_index"],
        "physical": result["physical"],
        "ip_check": {
            "ok": not result["conflicts"],
            "cidr": result["plan"]["cidr"] if result["plan"] else None,
            "conflicts": result["conflicts"],
        },
        "already_configured": result["already_configured"],
    }


def _add_cluster_node_locked(cluster_id: int, req: schemas.ClusterNodeAdd, db: Session):
    cluster = get_cluster_or_404(db, cluster_id)
    node = db.get(Node, req.node_id)
    if not node:
        raise api_error(404, Code.NODE_NOT_FOUND, "节点不存在")
    if any(m.node_id == req.node_id for m in cluster.members):
        raise api_error(409, Code.NODE_ALREADY_IN_CLUSTER, "该节点已在集群中")
    preflight = _preflight_add_node(cluster, node, req.configure_network, db)
    net_index = preflight["net_index"]
    peers = preflight["peers"]
    plan = preflight["plan"]
    already_configured = preflight["already_configured"]

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
        db.rollback()
        db.query(Node).filter(
            Node.id == req.node_id, Node.cluster_id == cluster_id
        ).update({Node.cluster_id: None})
        db.commit()

    changed_network = False
    try:
        if plan:
            if not already_configured:
                ok, msg = network_config_svc.apply_node_network(node, plan, net_index)
                if not ok:
                    raise api_error(400, Code.NETWORK_CONFIGURE_FAILED,
                                    f"节点 {node.name} 高速网络配置失败：{msg}",
                                    params={"name": node.name}, details=msg)
                changed_network = True
            ok, detail = network_config_svc.verify_node_network(node, plan, net_index, peers)
            if not ok:
                raise api_error(400, Code.NETWORK_VERIFY_FAILED_ROLLBACK,
                                f"节点 {node.name} 高速网络验证失败：{detail}",
                                params={"name": node.name}, details=detail)
            # 新节点能访问集群并不等于集群能访问新节点；逐个既有成员反向验证四 rail。
            reverse_failures = {}
            for peer, _ in peers:
                reverse_ok, reverse_detail = network_config_svc.verify_peer_reachability(
                    peer, plan, [(node, net_index)]
                )
                if not reverse_ok:
                    reverse_failures[peer.name] = reverse_detail
            if reverse_failures:
                raise api_error(
                    400, Code.NETWORK_VERIFY_FAILED_ROLLBACK,
                    f"既有成员到新节点 {node.name} 的反向高速网络验证失败：{reverse_failures}",
                    params={"name": node.name}, details=reverse_failures,
                )

        db.add(ClusterNode(cluster_id=cluster_id, node_id=req.node_id, net_index=net_index))
        db.commit()
    except Exception:
        if changed_network:
            try:
                network_config_svc.rollback_node_network(node)
            except Exception:  # noqa: BLE001
                pass
        _release_claim()
        raise
    db.refresh(cluster)
    return cluster


@router.post("/{cluster_id}/nodes", response_model=schemas.ClusterOut, status_code=201)
def add_cluster_node(cluster_id: int, req: schemas.ClusterNodeAdd, db: Session = Depends(get_db)):
    # 负键作为集群级锁，正键作为节点级锁：并发添加不同节点也不会拿到相同 net_index。
    locks = network_config_svc.acquire_operation_locks([-(cluster_id + 1), req.node_id])
    try:
        return _add_cluster_node_locked(cluster_id, req, db)
    finally:
        network_config_svc.release_operation_locks(locks)


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
    peer_roce_override: str | None = None
    if plan and from_node.id in net_map and "iface_subnets" in plan:
        from_index = net_map[from_node.id]
        try:
            plan_ips = network_config_svc.node_ips(plan, from_index)
            # RDMA 目标 IP 跟随该节点勾选后的 primary netdev（与 NCCL_IB_HCA 一致）
            from_vars = recipe_render.node_auto_vars(from_node, "worker", 0, plan=plan, net_index=from_index)
            iface = from_vars.get("netdev")
            roce_override = plan_ips.get(iface) or plan_ips.get("enp1s0f0np0")
            if to_node.id in net_map:
                peer_ips = network_config_svc.node_ips(plan, net_map[to_node.id])
                peer_roce_override = peer_ips.get(iface) or peer_ips.get("enp1s0f0np0")
        except Exception:  # noqa: BLE001 - 取不到时回退默认
            pass

    return await network_test_svc.run_network_test(
        from_node, to_node, req.tool, req.duration, req.ib_device,
        roce_ip_override=roce_override,
        peer_roce_ip_override=peer_roce_override,
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
    _cluster, nodes = _cluster_nodes(db, cluster_id)
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
