"""Pydantic API Schema。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from . import config
from .services.versioning import version_compare


# ---------- 节点 ----------


class NodeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    ip: str = Field(..., min_length=1)
    ssh_port: int = 22
    ssh_username: str = "root"
    ssh_auth_type: str = "password"  # password | key
    ssh_password: str | None = None
    ssh_key: str | None = None
    agent_port: int = 9000


class NodeUpdate(BaseModel):
    name: str | None = None
    ip: str | None = None
    ssh_port: int | None = None
    ssh_username: str | None = None
    ssh_auth_type: str | None = None
    ssh_password: str | None = None
    ssh_key: str | None = None
    agent_port: int | None = None


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    ip: str
    ssh_port: int
    ssh_username: str
    ssh_auth_type: str
    agent_port: int
    agent_status: str
    hardware_info: dict | None
    last_seen: datetime | None
    created_at: datetime
    cluster_id: int | None = None  # 所属集群（NULL=空闲，可加入集群）

    # --- Agent 版本（派生自 hardware_info 与控制平面版本，供前端轻量提醒） ---

    @computed_field  # type: ignore[prop-decorator]
    @property
    def agent_version(self) -> str | None:
        info = self.hardware_info
        v = info.get("agent_version") if isinstance(info, dict) else None
        return v if isinstance(v, str) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def agent_required(self) -> str:
        """控制平面期望的 Agent 版本（部署脚本随同仓库分发）。"""
        return config.APP_VERSION

    @computed_field  # type: ignore[prop-decorator]
    @property
    def agent_outdated(self) -> bool | None:
        """required > current → True；版本未知 → None。"""
        current = self.agent_version
        if current is None:
            return None
        return version_compare(config.APP_VERSION, current) > 0


# ---------- 集群 ----------


class ClusterCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    network_type: str = "roce"  # roce | ib | ethernet
    # 高速网络配置（创建时直接配置成员节点网络，测试通过才创建）：
    # node_ids 初始成员；network_cidr 网段（如 10.100.0.0/16）；network_mtu 高速口 MTU
    node_ids: list[int] = []
    network_cidr: str | None = None
    network_mtu: int | None = None


class ClusterNetworkDetect(BaseModel):
    node_ids: list[int] = Field(default_factory=list)
    network_cidr: str | None = None
    network_mtu: int | None = None


class ClusterUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    network_type: str | None = None


class ClusterNodeAdd(BaseModel):
    node_id: int
    # 添加成员时按集群网络规划配置该节点高速网络并验证（失败回滚不加入）
    configure_network: bool = True


class ClusterNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    node_id: int
    net_index: int  # 高速网槽位（1 起）；head/worker/rank 属任务级，不在集群成员上
    node: NodeOut | None = None


class ClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    network_type: str
    network_cidr: str | None = None
    network_mtu: int | None = None
    network_plan: dict | None = None
    created_at: datetime
    members: list[ClusterNodeOut] = []


class NetworkTestRequest(BaseModel):
    from_node_id: int
    to_node_id: int
    tool: str = "iperf3"  # iperf3 | ib_write_bw | ib_read_bw | ping
    duration: int = 10
    ib_device: str | None = None


# ---------- 配方 ----------


class VariableDef(BaseModel):
    key: str
    label: str = ""
    type: str = "string"  # string|int|float|bool|select
    source: str = "user"  # user|cluster|node
    auto: str | None = None
    default: str | None = None
    options: list[str] = []
    required: bool = False
    help: str = ""
    picker: str = ""  # ""|model|image：发布页提供已下载模型/已拉取镜像快速选择
    # 数值校验（如 NODES_TOTAL.min 用于发布页最小节点数校验；导入/导出保留）
    min: int | None = None
    max: int | None = None


class RecipeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    image: str | None = None
    compose_template: str
    variables: list[VariableDef] = []
    # 固定拓扑：确切的节点数（None=不固定）。每个配方按该数量设备调优，发布时须恰好匹配。
    nodes: int | None = Field(None, ge=1, le=64)


class RecipeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    image: str | None = None
    compose_template: str | None = None
    variables: list[VariableDef] | None = None
    nodes: int | None = Field(None, ge=1, le=64)


class RecipeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    image: str | None
    compose_template: str
    variables: list
    is_seed: bool
    created_at: datetime
    updated_at: datetime
    # 仅配方导入接口返回：导入时对 compose 模板做的自适应调整说明
    import_notice: str | None = None
    # 固定拓扑
    node_count: int | None = None
    tensor_parallel: int | None = None


# ---------- 配方源 / 目录（FireworksRecipes git 同步） ----------


class RecipeSourceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    url: str = Field(..., min_length=1)
    # 空值表示读取远端 HEAD，并自动选择仓库默认分支。
    branch: str | None = Field(None, min_length=1, max_length=128)


class RecipeSourceProbe(BaseModel):
    url: str = Field(..., min_length=1)


class RecipeSourceBranchesOut(BaseModel):
    default_branch: str
    branches: list[str]


class RecipeSourceUpdate(BaseModel):
    branch: str = Field(..., min_length=1, max_length=128)


class RecipeSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    branch: str
    status: str  # new|syncing|synced|failed
    last_commit: str | None
    recipe_count: int
    error: str | None
    created_at: datetime
    updated_at: datetime


class CatalogItem(BaseModel):
    """目录条目（来自配方源 recipes/index.json manifest + git 元数据）。

    注意：目录只负责「浏览 + 一键安装」；配方一旦安装即为本地独立个体，
    不做任何来源关联/已安装状态回显。
    """

    path: str  # recipe.json 仓库内相对路径
    id: str  # manifest 内 id
    name: str | None = None  # 显示名（主语言；商店卡片标题）
    name_en: str | None = None  # 英文显示名（缺省回退 name/id）
    provider: str | None = None
    model: str | None = None
    version: str | None = None
    params: str | None = None
    dtype: str | None = None
    context_length: int | None = None
    modality: str | None = None
    topology: str | None = None
    image: str | None = None
    nodes: int | None = None          # 固定拓扑：确切的节点数（配方按此调优）
    tensor_parallel: int | None = None  # 固定拓扑：张量并行度（= 节点数，GB10 每机 1 GPU）
    tags: list[str] = []
    description: str | None = None
    description_en: str | None = None   # 双语：英文描述（缺省回退 description）
    readme: str | None = None  # README.md 仓库内相对路径（默认语言）
    readme_en: str | None = None  # 英文 README 相对路径（缺省回退 readme）
    updated_at: str | None = None  # git commit 时间（ISO）


class CatalogOut(BaseModel):
    source_id: int
    source_name: str
    commit: str | None
    recipe_count: int
    items: list[CatalogItem] = []


class RecipeInstallIn(BaseModel):
    source_id: int
    path: str  # catalog 条目的 path（recipe.json 仓库内相对路径）
    # 安装时本地化的语言（zh | en）：本地只保存当前用户的一种语言快照；
    # en 且配方提供 *_en 字段时取英文，否则回退主语言（zh）。
    lang: str = Field("zh", pattern="^(zh|en)$")


# ---------- 任务 ----------


class TaskNodeAssignment(BaseModel):
    """任务级节点分配：发布任务时为每个节点显式指定 head/worker 与 rank（随任务保存）。"""

    node_id: int = Field(..., gt=0)
    role: Literal["head", "worker"]
    node_rank: int = Field(..., ge=0)


class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    recipe_id: int
    cluster_id: int
    # head/worker/rank 跟随任务：恰好一个 head 且其 node_rank 必须为 0，rank 全任务唯一
    nodes: list[TaskNodeAssignment]
    variables: dict[str, str] = Field(default_factory=dict)
    # 模型/镜像与任务解耦：发布时是否确保已发送到节点（缺失则自动走管理传输）
    send_model: bool = True
    send_image: bool = True


class TaskActionRequest(BaseModel):
    action: str  # pause | resume | stop | delete
    # 终止/删除任务时是否同时删除节点上的模型（释放磁盘）
    delete_model: bool = False


class BenchmarkRequest(BaseModel):
    """推理服务并发 decode 压测参数（回传给 agent /api/probe/benchmark）。"""

    concurrency: int = 8
    num_requests: int = 32
    max_tokens: int = 64


class OverviewTopologyNode(BaseModel):
    id: int
    name: str
    ip: str
    status: str
    cluster_id: int | None = None
    cluster_name: str | None = None
    gpu_count: int = 0
    gpu_utilization: float | None = None
    gpu_mem_used: int = 0
    gpu_mem_total: int = 0


class OverviewTopologyCluster(BaseModel):
    id: int
    name: str
    network_type: str
    network_cidr: str | None = None
    node_ids: list[int] = Field(default_factory=list)


class OverviewOut(BaseModel):
    snapshot_at: float
    window_seconds: int
    nodes_total: int
    nodes_online: int
    clusters_total: int
    recipes_total: int
    tasks_total: int
    tasks_running: int
    tasks_paused: int
    gpu_aggregate: dict  # {"total": N, "utilization": x, "mem_used": x, "mem_total": x}
    topology_nodes: list[OverviewTopologyNode] = Field(default_factory=list)
    topology_clusters: list[OverviewTopologyCluster] = Field(default_factory=list)
    benchmark_peak_tokens_per_sec: float | None = None
    benchmark_peak_at: float | None = None
