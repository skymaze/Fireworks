"""Pydantic API Schema。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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


class RecipeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    image: str | None = None
    compose_template: str | None = None
    variables: list[VariableDef] | None = None


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


# ---------- 任务 ----------


class TaskNodeAssignment(BaseModel):
    """任务级节点分配：发布任务时为每个节点显式指定 head/worker 与 rank（随任务保存）。"""

    node_id: int
    role: str  # head | worker
    node_rank: int


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


class OverviewOut(BaseModel):
    nodes_total: int
    nodes_online: int
    clusters_total: int
    recipes_total: int
    tasks_total: int
    tasks_running: int
    tasks_paused: int
    gpu_aggregate: dict  # {"total": N, "utilization": x, "mem_used": x, "mem_total": x}
