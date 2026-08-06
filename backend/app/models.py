"""SQLAlchemy 数据模型。

- nodes          : 受管节点（SSH 信息、agent 状态、硬件信息缓存）
- clusters       : 集群
- cluster_nodes  : 集群成员（role: head/worker, node_rank）
- recipes        : 配方（compose 模板 + 变量定义）
- tasks          : 任务（发布/运行/暂停）
- task_nodes     : 任务在各节点上的容器
- metric_samples : 指标样本（控制平面轮询 agent 入库，图表读库）
- users / auth_sessions : 登录用户与会话（阶段一单一用户，token 存 sha256 摘要）
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class UTCDateTime(TypeDecorator):
    """SQLite 时间列：存储 naive UTC，读取时附加 UTC 时区。

    SQLite 无原生时区类型，UTCDateTime 读回是 naive datetime，
    Pydantic 序列化（response_model）输出无时区标记，前端 new Date() 会误当
    本地时间解析（偏移 8 小时）。统一在读取侧补 tzinfo。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    """UTC datetime -> ISO 字符串（带 +00:00 时区标记）。

    SQLite 读回的 datetime 可能是 naive UTC，直接 isoformat() 无时区标记，
    前端 new Date() 会误当本地时间解析（偏移 8 小时）。统一补时区。
    """
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    ip: Mapped[str] = mapped_column(String(64))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_username: Mapped[str] = mapped_column(String(64), default="root")
    ssh_auth_type: Mapped[str] = mapped_column(String(16), default="password")  # password|key
    ssh_password: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    ssh_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # 私钥内容
    agent_port: Mapped[int] = mapped_column(Integer, default=9000)
    agent_status: Mapped[str] = mapped_column(String(16), default="unknown")  # unknown|online|offline|error
    hardware_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # 所属集群（一节点一集群：NULL=空闲；加入集群时原子更新，避免重复加入）
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id"), nullable=True)

    cluster_links: Mapped[list["ClusterNode"]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )
    cluster: Mapped["Cluster | None"] = relationship(back_populates="nodes")


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    network_type: Mapped[str] = mapped_column(String(32), default="roce")  # roce|ib|ethernet
    master_port: Mapped[int] = mapped_column(Integer, default=25000)
    # 高速网络规划（创建/添加成员时配置，网络测试通过才提交）：
    # network_cidr 网段前缀（如 10.100.0.0/16）；network_mtu 高速口 MTU；
    # network_plan 接口→子网映射（新成员同接口分同一网段，按序号分配 IP）
    network_cidr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    network_mtu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    network_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    members: Mapped[list["ClusterNode"]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan"
    )
    nodes: Mapped[list["Node"]] = relationship(back_populates="cluster")


class ClusterNode(Base):
    __tablename__ = "cluster_nodes"
    __table_args__ = (
        UniqueConstraint("cluster_id", "node_id", name="uq_cluster_node"),
        # 一个节点只能加入一个集群（数据库层强制）
        Index("uq_cluster_nodes_node", "node_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"))
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16), default="worker")  # head|worker
    node_rank: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    node: Mapped["Node"] = relationship(back_populates="cluster_links")
    cluster: Mapped["Cluster"] = relationship(back_populates="members")


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    compose_template: Mapped[str] = mapped_column(Text)
    variables: Mapped[list] = mapped_column(JSON, default=list)  # [{key,label,type,source,auto,default,options,required,help}]
    is_seed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"))
    status: Mapped[str] = mapped_column(
        String(32), default="published"
    )  # published|running|paused|stopped|error
    variables: Mapped[dict] = mapped_column(JSON, default=dict)  # 用户变量快照
    rendered: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 逐节点渲染结果
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    nodes: Mapped[list["TaskNode"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class TaskNode(Base):
    __tablename__ = "task_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    node_rank: Mapped[int] = mapped_column(Integer, default=0)
    container_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    container_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    task: Mapped["Task"] = relationship(back_populates="nodes")


class MetricSample(Base):
    __tablename__ = "metric_samples"
    __table_args__ = (Index("ix_metric_node_ts", "node_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    ts: Mapped[float] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ModelDownload(Base):
    """模型传输任务：控制平面下载 -> 管理网发送 head -> RoCE 同步 worker。"""

    __tablename__ = "model_downloads"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(500))
    revision: Mapped[str] = mapped_column(String(128), default="main")
    # 为 None 表示仅下载到控制平面（不发送节点）
    head_node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="downloading")
    # downloading | sending | syncing | completed | failed
    sync_jobs: Mapped[dict] = mapped_column(JSON, default=dict)  # {node_id: {job_id, status, error}}
    downloaded_bytes: Mapped[int] = mapped_column(default=0)  # 控制平面本地
    sent_bytes: Mapped[int] = mapped_column(default=0)        # 已发送到 head
    total_bytes: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class Setting(Base):
    """全局设置（模型下载：endpoint/token/连接数等）。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class ImageTransfer(Base):
    """镜像传输任务：控制平面拉取（skopeo）-> 管理网发送 head -> RoCE 同步 worker -> 节点 docker load。

    与模型分发同构：解决多节点同时向公网拉镜像的带宽竞争/网络不稳定问题。
    head_node_id 为 None 时仅下载到控制平面（可之后分发）。
    """

    __tablename__ = "image_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    image: Mapped[str] = mapped_column(String(500))          # ghcr.io/anemll/dspark-vllm-gx10:0.1.1
    digest: Mapped[str | None] = mapped_column(String(128), nullable=True)  # sha256:...
    head_node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pulling")
    # pulling | sending | syncing | loading | completed | failed
    sync_jobs: Mapped[dict] = mapped_column(JSON, default=dict)  # {node_id: {status, error}}
    downloaded_bytes: Mapped[int] = mapped_column(default=0)
    sent_bytes: Mapped[int] = mapped_column(default=0)
    size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class User(Base):
    """登录用户（阶段一：仅单一用户，无角色/无多用户管理）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # bcrypt
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    """登录会话。DB 只存 token 的 sha256 摘要（原文仅在登录响应 cookie 中出现一次）。

    token 原文泄露面最小化：即使数据库被读取也无法伪造会话。
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("uq_auth_sessions_token_hash", "token_hash", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    user: Mapped["User"] = relationship(back_populates="sessions")
