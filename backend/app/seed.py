"""种子配方：DeepSeek-V4-Flash 2x DGX Spark（改编自
https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark）

将参考仓库的 .env + docker-compose.dspark.yml 抽象为「compose 模板 + 三类变量」：
- source=cluster : 发布时按集群自动填充（MASTER_ADDR/NODES_TOTAL/MASTER_PORT）
- source=node    : 发布时按节点角色逐节点渲染（NODE_RANK/VLLM_HOST_IP/NCCL_*）
- source=user    : 发布向导中填写或使用默认值
"""

from sqlalchemy.orm import Session

from .models import Recipe

SEED_RECIPES = [
    {
        "name": "DeepSeek-V4-Flash 2x DGX Spark (DSpark)",
        "description": (
            "基于 MiaAI-Lab 参考配方的 vLLM 2 节点 TP=2 服务（Anemll dspark-vllm-gx10 镜像，"
            "NVFP4 DS-MLA + flashinfer_b12x + DSpark 投机解码）。发布时选择集群、head/worker，"
            "RoCE/NCCL/节点变量自动填充。模型默认 deepseek-ai/DeepSeek-V4-Flash-DSpark，"
            "HF 缓存挂载到各节点 ~/.cache/huggingface，首次发布会自动下载模型（约 167GB）。"
        ),
        "image": "ghcr.io/anemll/dspark-vllm-gx10:0.1.1",
        "compose_template": """services:
  vllm-dspark:
    image: ${DSPARK_VLLM_IMAGE:-ghcr.io/anemll/dspark-vllm-gx10:0.1.1}
    container_name: ${TASK_NAME}-rank${NODE_RANK}
    pull_policy: if_not_present
    entrypoint: []
    network_mode: host
    ipc: host
    shm_size: "64gb"
    ulimits:
      memlock: -1
      stack: 67108864
    gpus: all
    devices:
      - /dev/infiniband:/dev/infiniband
    volumes:
      - ${HF_CACHE:-${HOME}/.cache/huggingface}:/cache/huggingface
      - ${HOME}/.cache/vllm-cache:/cache/huggingface/vllm-cache
      - ${DSPARK_TMP_HOST:-${HOME}/.cache/dspark-tmp}:/tmp
    environment:
      HF_HOME: /cache/huggingface
      HF_HUB_OFFLINE: "1"  # 模型由管理平台分发到节点缓存，固定离线加载，避免启动时访问 HF
      TRANSFORMERS_OFFLINE: "${TRANSFORMERS_OFFLINE:-0}"
      HF_HUB_DISABLE_XET: "${HF_HUB_DISABLE_XET:-1}"
      # 挂载到宿主 vllm-cache 目录（kernel 编译/autotune 缓存持久化，容器重建不丢）
      VLLM_CACHE_ROOT: /cache/huggingface/vllm-cache
      DG_JIT_CACHE_DIR: /tmp/deep-gemm
      VLLM_HOST_IP: "${VLLM_HOST_IP:-}"
      VLLM_ALLOW_LONG_MAX_MODEL_LEN: "${VLLM_ALLOW_LONG_MAX_MODEL_LEN:-1}"
      VLLM_SPARSE_INDEXER_MAX_LOGITS_MB: "${VLLM_SPARSE_INDEXER_MAX_LOGITS_MB:-256}"
      VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS: "${VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS:-0}"
      ENABLE_VLLM_GB10_PATCH: "${ENABLE_VLLM_GB10_PATCH:-0}"
      GB10_HYBRID_NVFP4_M_THRESHOLD: "${GB10_HYBRID_NVFP4_M_THRESHOLD:-128}"
      VLLM_USE_FLASHINFER_SAMPLER: "${VLLM_USE_FLASHINFER_SAMPLER:-1}"
      VLLM_USE_BREAKABLE_CUDAGRAPH: "${VLLM_USE_BREAKABLE_CUDAGRAPH:-0}"
      VLLM_USE_B12X_MOE: "${VLLM_USE_B12X_MOE:-1}"
      VLLM_B12X_W4A16_FORCE_BLOCKS_PER_SM: "${VLLM_B12X_W4A16_FORCE_BLOCKS_PER_SM:-0}"
      VLLM_B12X_W4A16_FORCE_BLOCKS_MAX_M: "${VLLM_B12X_W4A16_FORCE_BLOCKS_MAX_M:-16}"
      # CuTeDSL 编译目标锁定 GB10（sm_121a）：缺失时 b12x 可能在推理中途 JIT
      # 慢速 W4A16 fused MoE 路径而非 GB10 原生 kernel（上游实测确认）
      CUTE_DSL_ARCH: "${CUTE_DSL_ARCH:-sm_121a}"
      FLASHINFER_DISABLE_VERSION_CHECK: "${FLASHINFER_DISABLE_VERSION_CHECK:-1}"
      FLASHINFER_WORKSPACE_BASE: "${FLASHINFER_WORKSPACE_BASE:-/cache/huggingface/flashinfer}"
      TILELANG_CLEANUP_TEMP_FILES: "${TILELANG_CLEANUP_TEMP_FILES:-1}"
      DG_JIT_USE_NVRTC: "${DG_JIT_USE_NVRTC:-0}"
      DG_JIT_NVCC_COMPILER: "${DG_JIT_NVCC_COMPILER:-/usr/local/cuda/bin/nvcc}"
      TORCH_CUDA_ARCH_LIST: "${TORCH_CUDA_ARCH_LIST:-12.1a}"
      FLASHINFER_CUDA_ARCH_LIST: "${FLASHINFER_CUDA_ARCH_LIST:-12.1a}"
      NCCL_NET: "${NCCL_NET:-IB}"
      NCCL_IB_HCA: "${NCCL_IB_HCA:-}"
      NCCL_SOCKET_IFNAME: "${NCCL_SOCKET_IFNAME:-}"
      # TP/GLOO 与 NCCL 同网卡（mp 后端分布式通信不落到默认路由）
      TP_SOCKET_IFNAME: "${TP_SOCKET_IFNAME:-${NCCL_SOCKET_IFNAME}}"
      GLOO_SOCKET_IFNAME: "${GLOO_SOCKET_IFNAME:-${NCCL_SOCKET_IFNAME}}"
      NCCL_IB_GID_INDEX: "${NCCL_IB_GID_INDEX:-}"
      NCCL_IB_GID_AUTO: "1"
      NCCL_IB_ADDR_FAMILY: AF_INET
      NCCL_IB_ROCE_VERSION_NUM: 2
      NCCL_IB_DISABLE: "0"
      NCCL_CROSS_NIC: "1"
      NCCL_NVLS_ENABLE: "0"
      NCCL_CUMEM_ENABLE: "0"
      NCCL_IGNORE_CPU_AFFINITY: "1"
      NCCL_DEBUG: WARN
      # 投机解码 k 必须 ≥ checkpoint dspark_block_size(5)：k<5 静默截断草稿块
      # 放入容器环境，使命令内 $${MTP_NUM_TOKENS} 展开为真实值（.env 只影响 compose 插值）
      MTP_NUM_TOKENS: "${MTP_NUM_TOKENS:-5}"
      DEFAULT_THINKING: "${DEFAULT_THINKING:-off}"
      PYTORCH_CUDA_ALLOC_CONF: expandable_segments:True
    command:
      - bash
      - -lc
      - >
        export PATH="/usr/local/cuda/bin:/usr/local/bin:$${PATH:-}";
        export CUDA_HOME="$${CUDA_HOME:-/usr/local/cuda}";
        export CUDA_PATH="$${CUDA_PATH:-$${CUDA_HOME}}";
        export CUDAToolkit_ROOT="$${CUDAToolkit_ROOT:-$${CUDA_HOME}}";
        export LD_LIBRARY_PATH="/usr/local/cuda/lib64:$${LD_LIBRARY_PATH:-}";
        DEFAULT_THINKING_MODE="$${DEFAULT_THINKING:-off}";
        case "$${DEFAULT_THINKING_MODE}" in
          off) DEFAULT_CHAT_TEMPLATE_KWARGS='{"thinking":false}' ;;
          low) DEFAULT_CHAT_TEMPLATE_KWARGS='{"thinking":true,"reasoning_effort":"low"}' ;;
          high) DEFAULT_CHAT_TEMPLATE_KWARGS='{"thinking":true,"reasoning_effort":"high"}' ;;
          max) DEFAULT_CHAT_TEMPLATE_KWARGS='{"thinking":true,"reasoning_effort":"max"}' ;;
          *) echo "DEFAULT_THINKING 必须为 off/low/high/max（当前: $${DEFAULT_THINKING_MODE}）" >&2; exit 2 ;;
        esac;
        SPECULATIVE_CONFIG="{\\"method\\":\\"dspark\\",\\"num_speculative_tokens\\":$${MTP_NUM_TOKENS:-5},\\"draft_sample_method\\":\\"probabilistic\\"}";
        exec /usr/local/bin/vllm serve ${DSPARK_MODEL:-deepseek-ai/DeepSeek-V4-Flash-DSpark}
        --served-model-name ${SERVED_MODEL_NAME:-deepseek-v4-flash-dspark}
        --host ${VLLM_HOST:-0.0.0.0}
        --port ${VLLM_PORT:-8888}
        --trust-remote-code
        --tensor-parallel-size 2
        --pipeline-parallel-size 1
        --kv-cache-dtype nvfp4_ds_mla
        --block-size 256
        --max-model-len ${MAX_MODEL_LEN:-1048576}
        --max-num-seqs ${MAX_NUM_SEQS:-6}
        --max-num-batched-tokens ${MAX_NUM_BATCHED_TOKENS:-8192}
        --max-cudagraph-capture-size $$(( ${MAX_NUM_SEQS:-6} * (${MTP_NUM_TOKENS:-5} + 1) ))
        --gpu-memory-utilization ${GPU_MEMORY_UTILIZATION:-0.80}
        --enable-prefix-caching
        --enable-prompt-tokens-details
        --async-scheduling
        --enable-chunked-prefill
        --speculative-config "$${SPECULATIVE_CONFIG}"
        --tokenizer-mode deepseek_v4
        --distributed-executor-backend mp
        --moe-backend flashinfer_b12x
        --tool-call-parser deepseek_v4
        --enable-auto-tool-choice
        --reasoning-parser deepseek_v4
        --reasoning-config '{"reasoning_parser":"deepseek_v4","reasoning_start_str":"<think>","reasoning_end_str":"</think>"}'
        --default-chat-template-kwargs "$${DEFAULT_CHAT_TEMPLATE_KWARGS}"
        --generation-config vllm
        --enable-flashinfer-autotune
        --nnodes ${NODES_TOTAL:-2}
        --node-rank ${NODE_RANK}
        --master-addr ${MASTER_ADDR}
        --master-port ${MASTER_PORT:-25000}
        ${HEADLESS:+--headless}
""",
        "variables": [
            # ---- 用户变量（发布向导填写）----
            {"key": "DSPARK_VLLM_IMAGE", "label": "vLLM 镜像", "type": "string", "source": "user",
             "default": "ghcr.io/anemll/dspark-vllm-gx10:0.1.1", "picker": "image",
             "help": "从已拉取镜像中选择（控制平面先拉取再分发）"},
            {"key": "DSPARK_MODEL", "label": "模型", "type": "string", "source": "user",
             "default": "deepseek-ai/DeepSeek-V4-Flash-DSpark", "required": True, "picker": "model",
             "help": "从已下载模型中选择（控制平面下载后分发到各节点缓存）"},
            {"key": "SERVED_MODEL_NAME", "label": "对外服务名", "type": "string", "source": "user",
             "default": "deepseek-v4-flash-dspark"},
            {"key": "VLLM_PORT", "label": "vLLM API 端口", "type": "int", "source": "user",
             "default": "8888"},
            {"key": "MAX_MODEL_LEN", "label": "最大上下文长度", "type": "int", "source": "user",
             "default": "1048576"},
            {"key": "MAX_NUM_SEQS", "label": "最大并发序列数", "type": "int", "source": "user",
             "default": "6"},
            {"key": "MAX_NUM_BATCHED_TOKENS", "label": "单批最大 token 数", "type": "int", "source": "user",
             "default": "8192"},
            {"key": "GPU_MEMORY_UTILIZATION", "label": "GPU 显存利用率", "type": "float", "source": "user",
             "default": "0.80"},
            {"key": "MTP_NUM_TOKENS", "label": "DSpark 投机 token 数", "type": "int", "source": "user",
             "default": "5",
             "help": "必须 ≥ checkpoint 的 dspark_block_size(5)：k<5 会静默截断草稿块，解码吞吐下降"},
            {"key": "DEFAULT_THINKING", "label": "默认思考模式", "type": "string", "source": "user",
             "default": "off",
             "help": "off 不生成思考（最快）；low/high/max 开启思考并控制 reasoning effort；请求级参数可覆盖"},
            # ---- 集群变量（自动填充，可覆盖）----
            {"key": "MASTER_ADDR", "label": "Head 节点地址", "type": "string", "source": "cluster",
             "auto": "master_addr", "required": True},
            {"key": "MASTER_PORT", "label": "分布式主端口", "type": "int", "source": "cluster",
             "auto": "master_port", "default": "25000"},
            {"key": "NODES_TOTAL", "label": "节点总数", "type": "int", "source": "cluster",
             "auto": "nodes_total", "required": True, "min": 2,
             "help": "TP=2 分布式需要 ≥2 节点（head + 至少 1 worker）"},
            # ---- 节点变量（按节点角色逐节点渲染）----
            {"key": "NODE_RANK", "label": "节点序号", "type": "int", "source": "node",
             "auto": "node_rank", "required": True},
            {"key": "HEADLESS", "label": "Headless（worker 不跑 API server）", "type": "string", "source": "node",
             "auto": "headless", "help": "自动按角色填充：head 为空、worker 为 1（mp 多节点协调必需）"},
            {"key": "VLLM_HOST_IP", "label": "本节点 vLLM 绑定 IP", "type": "string", "source": "node",
             "auto": "node_roce_ip", "help": "必须为各节点 RoCE IP，避免分布式绑定错误地址"},
            {"key": "NCCL_IB_HCA", "label": "RoCE HCA", "type": "string", "source": "node",
             "auto": "hca", "help": "如 rocep1s0f0"},
            {"key": "NCCL_SOCKET_IFNAME", "label": "NCCL 网络接口", "type": "string", "source": "node",
             "auto": "netdev", "help": "如 enp1s0f0np0"},
            {"key": "NCCL_IB_GID_INDEX", "label": "RoCE GID index", "type": "int", "source": "node",
             "auto": "gid_index", "help": "每节点/HCA 不同且重启漂移，自动解析"},
        ],
    }
]


def seed_recipes(db: Session) -> None:
    """首次启动时写入种子配方（仅当 recipes 表为空）。"""
    if db.query(Recipe).count() > 0:
        return
    for data in SEED_RECIPES:
        db.add(Recipe(**data, is_seed=True))
    db.commit()
