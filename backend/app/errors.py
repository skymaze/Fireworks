"""结构化 API 错误（RFC 9457 Problem Details 的轻量实现）。

- `code`：稳定、语言无关的错误码。前端据此分支并本地化显示（i18n backendError.*）；
- `msg`：默认语言（中文）的人类可读消息，前端未知 code 时回退使用；
- `params`：消息模板插值参数（供前端多语言模板插值）；
- `details`：自由文本/原始输出（工具 stderr、Agent 响应体、异常原文）。
  按业界惯例不做本地化，原样透传（UI 放可展开区）。
"""

from fastapi import HTTPException


class Code:
    """稳定错误码清单。新增时需同步前端 i18n backendError.* 与错误文档。"""

    # 认证
    RATE_LIMITED = "rate_limited"
    ALREADY_INITIALIZED = "already_initialized"
    USERNAME_EMPTY = "username_empty"
    USERNAME_EXISTS = "username_exists"
    INIT_CONFLICT = "init_conflict"
    BAD_CREDENTIALS = "bad_credentials"
    OLD_PASSWORD_WRONG = "old_password_wrong"
    UNAUTHORIZED = "unauthorized"
    AGENT_TOKEN_INVALID = "agent_token_invalid"

    # 集群
    CLUSTER_NOT_FOUND = "cluster_not_found"
    CLUSTER_NAME_EXISTS = "cluster_name_exists"
    CIDR_FORMAT_ERROR = "cidr_format_error"
    CIDR_NO_AVAILABLE = "cidr_no_available"
    CIDR_CONFLICT = "cidr_conflict"
    NODE_BELONGS_OTHER = "node_belongs_other"
    NODE_ALREADY_IN_CLUSTER = "node_already_in_cluster"
    NODE_NOT_IN_CLUSTER = "node_not_in_cluster"
    NETWORK_CONFIGURE_FAILED = "network_configure_failed"
    NETWORK_VERIFY_FAILED_ROLLBACK = "network_verify_failed_rollback"
    NETWORK_TEST_NODES_NOT_IN_CLUSTER = "network_test_nodes_not_in_cluster"
    CLUSTER_HAS_RUNNING_TASKS = "cluster_has_running_tasks"

    # 节点 / Agent
    NODE_NOT_FOUND = "node_not_found"
    NODE_NAME_EXISTS = "node_name_exists"
    AGENT_UNREACHABLE = "agent_unreachable"
    AGENT_EXEC_FAILED = "agent_exec_failed"
    AGENT_RESOURCE_NOT_FOUND = "agent_resource_not_found"
    NVIDIA_SMI_FAILED = "nvidia_smi_failed"
    # 删除节点时的防御性校验
    NODE_HAS_ACTIVE_TASKS = "node_has_active_tasks"
    NODE_IN_CLUSTER = "node_in_cluster"

    # 任务
    TASK_NOT_FOUND = "task_not_found"
    TASK_ALREADY_EXISTS = "task_already_exists"
    TASK_NAME_INVALID = "task_name_invalid"
    TASK_STATE_CHANGED = "task_state_changed"
    TASK_NO_HEAD = "task_no_head"
    TASK_HEAD_NOT_RANK0 = "task_head_not_rank0"
    TASK_RANK_TAKEN = "task_rank_taken"
    # 配方声明了固定拓扑（NODES_TOTAL min==max）时，任务节点数必须恰好匹配
    TASK_NODE_COUNT_MISMATCH = "task_node_count_mismatch"
    NODE_BUSY = "node_busy"
    CONTAINER_LOG_UNAVAILABLE = "container_log_unavailable"
    CONTAINER_NOT_FOUND = "container_not_found"
    WORKER_NOT_IN_CLUSTER = "worker_not_in_cluster"

    # 模型
    MODEL_NOT_FOUND = "model_not_found"
    LOCAL_CACHE_INCOMPLETE = "local_cache_incomplete"
    MODEL_DOWNLOAD_NOT_FOUND = "model_download_not_found"
    DISTRIBUTE_HEAD_REQUIRED = "distribute_head_required"
    RETRY_ONLY_FAILED = "retry_only_failed"

    # 镜像
    IMAGE_TRANSFER_NOT_FOUND = "image_transfer_not_found"
    IMAGE_ARCHIVE_NOT_FOUND = "image_archive_not_found"
    IMAGE_CHECK_FAILED = "image_check_failed"
    INVALID_FILENAME = "invalid_filename"

    # 配方
    RECIPE_NOT_FOUND = "recipe_not_found"
    RECIPE_NAME_EXISTS = "recipe_name_exists"
    RECIPE_IMPORT_INVALID = "recipe_import_invalid"
    RECIPE_INVALID_VARIABLES = "recipe_invalid_variables"
    HEAD_NOT_IN_CLUSTER = "head_not_in_cluster"

    # 配方源 / 目录
    RECIPE_SOURCE_NOT_FOUND = "recipe_source_not_found"
    RECIPE_SOURCE_NAME_EXISTS = "recipe_source_name_exists"
    RECIPE_SOURCE_INVALID = "recipe_source_invalid"
    RECIPE_SYNC_FAILED = "recipe_sync_failed"
    RECIPE_SYNC_IN_PROGRESS = "recipe_sync_in_progress"
    CATALOG_NOT_SYNCED = "catalog_not_synced"
    CATALOG_ITEM_NOT_FOUND = "catalog_item_not_found"

    # 内部 / 回拉
    INVALID_RELPATH = "invalid_relpath"
    FILE_NOT_FOUND = "file_not_found"
    ARCHIVE_NOT_FOUND = "archive_not_found"

    # 通用
    DB_UNAVAILABLE = "db_unavailable"


def api_error(status: int, code: str, msg: str,
              params: dict | None = None, details=None) -> HTTPException:
    """构造结构化 HTTPException（detail 为 RFC 9457 风格对象）。

    默认仍返回中文 `msg` 且只加 `code` 锚点：未迁移/历史错误行为不变，
    前端按 code 本地化、未知 code 回退 msg。
    """
    detail: dict = {"code": code, "msg": msg}
    if params:
        detail["params"] = params
    if details is not None:
        detail["details"] = details
    return HTTPException(status, detail=detail)
