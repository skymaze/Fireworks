# 🎆 Fireworks — DGX Spark Cluster Management

[中文](README.md) | [English](README.en.md)

[![Release](https://img.shields.io/github/v/release/skymaze/Fireworks)](https://github.com/skymaze/Fireworks/releases) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) · [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

A web-based management tool for NVIDIA DGX Spark (GB10) clusters, covering nodes, clusters, models, tasks, and recipes:

- **Nodes**: add a node and automatically deploy the Agent over SSH, with explicit errors and rollback on installation or connectivity failure; monitor CPU, GPU, temperature, unified memory, disk, and network metrics in real time, plus `nvidia-smi` output
- **Clusters**: automatically discover the physical links, existing subnets, and IP usage across all four RoCE rails; form clusters with transactional high-speed network configuration; run ping, iperf3, and perftest checks with one click
- **Models and images**: download a single copy to the control plane, send it to the head node over the management network, then let worker Agents pull it in parallel over planned high-speed IPs without node-to-node SSH or rsync; show per-node progress, speed, and current file
- **Tasks**: run containerized workloads with Docker Compose; assign head/worker roles and ranks per node; publish, pause, resume, stop, **start**, **restart**, delete, inspect logs, and run health checks (stop/start/restart reuse containers, no recreation)
- **Recipes**: reusable task configuration templates with automatic values from shared, node, and user sources, integrated into a guided deployment workflow
- **Overview**: visualize cluster and node topology, aggregate online GPU resources, and track real vLLM traffic across Decode and Prefill tok/s, request counts, TTFT P95, KV cache, and window peaks over the last hour or 24 hours; the backend passively reads `/metrics`, computes summaries from every source interval, and time-buckets only chart output without sending synthetic requests

The complete workflow has been validated end to end on two-node and four-node DGX Spark systems.

![Fireworks overview showing cluster topology, GPU resources, and inference metrics](docs/images/overview.png)

_The overview brings together cluster topology, online node resources, and inference performance for running tasks._

## Quick start

### 1. Prerequisites

- Install **Git**. On Windows or macOS, download it from [git-scm.com](https://git-scm.com/downloads); on Linux, use your system package manager.
- Install **Docker** with Compose v2. Use [Docker Desktop](https://www.docker.com/products/docker-desktop/) on Windows or macOS, or Docker Engine on Linux.
- On Windows or macOS, start Docker Desktop and wait until Docker Engine reports that it is running.
- Clone the repository and enter its directory:

```bash
git clone https://github.com/skymaze/Fireworks.git
cd Fireworks
docker compose version
```

If `docker compose version` prints a version, the environment is ready. Run all commands below from this `Fireworks` directory.

- If you plan to download large models, choose a disk with sufficient capacity before the first launch by following [Bind host directories by operating system](#bind-host-directories-by-operating-system). Otherwise, Docker's default data disk is used.

### 2. Choose a deployment mode

#### HTTP deployment (local machine or trusted LAN)

Use this option when you do not have a domain and HTTPS reverse proxy. It pulls versioned images, starts the services, and makes Fireworks available at `http://DEPLOYMENT_HOST_IP:3000`.

**Mainland China (Alibaba Cloud registry)**

```bash
FW_IMAGE_TAG=0.2.0 COOKIE_SECURE=0 docker compose -f docker-compose.prod.cn.yml up -d --pull always
```

**International (GHCR)**

```bash
FW_IMAGE_TAG=0.2.0 COOKIE_SECURE=0 docker compose -f docker-compose.prod.yml up -d --pull always
```

<details>
<summary>Windows PowerShell commands</summary>

Mainland China:

```powershell
$env:FW_IMAGE_TAG = "0.2.0"
$env:COOKIE_SECURE = "0"
docker compose -f docker-compose.prod.cn.yml up -d --pull always
```

International:

```powershell
$env:FW_IMAGE_TAG = "0.2.0"
$env:COOKIE_SECURE = "0"
docker compose -f docker-compose.prod.yml up -d --pull always
```

</details>

Use `COOKIE_SECURE=0` only for plain HTTP on a local machine or trusted LAN. Never expose a plaintext login endpoint to the public internet.

#### HTTPS deployment (domain and reverse proxy)

First configure certificates and a reverse proxy using [`deploy/nginx-fireworks.conf.example`](deploy/nginx-fireworks.conf.example). Proxy the entire site, including the `/api/ws/events` WebSocket endpoint, to the frontend on port `3000`, then start Fireworks.

**Mainland China (Alibaba Cloud registry)**

```bash
FW_IMAGE_TAG=0.2.0 docker compose -f docker-compose.prod.cn.yml up -d --pull always
```

**International (GHCR)**

```bash
FW_IMAGE_TAG=0.2.0 docker compose -f docker-compose.prod.yml up -d --pull always
```

<details>
<summary>Windows PowerShell commands</summary>

If you previously ran the HTTP command in the same PowerShell window, remove `COOKIE_SECURE` first:

```powershell
Remove-Item Env:COOKIE_SECURE -ErrorAction SilentlyContinue
$env:FW_IMAGE_TAG = "0.2.0"
```

Mainland China:

```powershell
docker compose -f docker-compose.prod.cn.yml up -d --pull always
```

International:

```powershell
docker compose -f docker-compose.prod.yml up -d --pull always
```

</details>

Do not set `COOKIE_SECURE=0` in HTTPS mode. Production Compose enables secure cookies by default. Configure the reverse proxy to redirect all HTTP requests to HTTPS.

### 3. Initialize and sign in

1. For HTTP, open **http://DEPLOYMENT_HOST_IP:3000**. For HTTPS, open the configured domain.
2. On the first visit, use the initialization page to create an **administrator account**.
3. Sign in with that account and enter the console.

Once the services are running, follow the [Workflow](#workflow) to connect real nodes.

If the page does not open, run `docker compose -f docker-compose.prod.cn.yml ps` for Mainland China or `docker compose -f docker-compose.prod.yml ps` internationally. Both `backend` and `frontend` should be `Up` or `healthy`. On Linux and macOS, run `curl -fsS http://127.0.0.1:8000/api/health`; in Windows PowerShell, run `Invoke-RestMethod http://127.0.0.1:8000/api/health`. A healthy backend returns its version and an `ok` status.

### Troubleshooting

| Symptom | Resolution |
|---|---|
| Port already in use | Change the host side of `"3000:3000"` in the Compose file, for example to `"8080:3000"`, then open `localhost:8080`. |
| Redirected to sign-in immediately, or session is lost | For plain HTTP, rerun the deployment command with `COOKIE_SECURE=0`. |
| Need access from another computer on the LAN | The frontend port is exposed to the LAN; open the deployment host's LAN IP. Backend port `8000` must remain reachable from every Agent. |
| Slow image pull or `pull access denied` | Confirm that you selected the correct Compose file; use the `cn` file in Mainland China. Images are public and allow anonymous pulls. |
| `Mounts denied` or path cannot be shared | Allow Docker Desktop to access the selected directory or disk in its file-sharing settings, then retry. |
| `no space left on device` | The Docker data disk or bound disk is full. Review [Volumes and large-model capacity](#volumes-and-large-model-capacity) and move `fireworks-cache`. |

## Workflow

Manage a real cluster in this order. Each page also includes contextual guidance:

1. **Add nodes and deploy Agents automatically**: go to `Nodes → Add Node`, enter the IP address and SSH credentials, and save. The control plane immediately uploads the Agent and its **offline dependency bundle** over SSH, installs it as a systemd service, and verifies connectivity. The node does not need PyPI access. A node is added only after deployment and verification succeed; any failure is reported explicitly and rolled back by uninstalling the Agent and removing the incomplete node record. Adding a node runs **initial optimization** by default (toggleable via a checkbox): 4 system-level tweaks performed over SSH root/sudo to improve cluster usability — disable Wi-Fi/Bluetooth, disable the GUI, grant the current SSH user Docker group access, and disable swap — then **reboots the node once** to apply everything (also verifying the Agent auto-starts with the system). Optimization is **best-effort**: missing root privileges or a failed step never blocks adding the node, it is only surfaced as a warning. You can also run it manually on any existing node via the **Optimize** button in the node list (this also reboots the node; the result is stored on the node; requires root or sudo). The node list shows the optimization status (Optimized / Incomplete / Not optimized).
2. **Optionally redeploy an Agent**: for nodes in `offline`, `unknown`, or `error` state, use **Redeploy Agent** from the node list to repair or reinstall it.
3. **Create a cluster**: go to `Clusters → Create Cluster`. The dialog pre-fills a free subnet, which you may edit; selecting members does not trigger expensive network requests. Submission performs one authoritative check against your subnet: node snapshots, ARP discovery, network application, and validation all run in parallel by node. On conflicts with the live network the API returns a suggested subnet, which the UI shows and applies automatically; existing node networks are not reused. The cluster is created only if all checks pass, and failures roll back automatically. Node data is refreshed from the Agents afterward so stale network addresses are not retained.
4. **Add members**: on a cluster details page, select **Add Node**. The Web UI allocates IP slots while avoiding conflicts, performs the same physical-link and occupancy checks, applies the configuration, and validates both directions between new and existing nodes without manual scripts or SSH work.
5. **Configure recipe sources**: go to `Recipe Store → Add Recipe Source` and enter only the repository URL. Fireworks discovers remote branches and selects the default branch automatically. Use **Source Settings** to switch branches and resynchronize, or remove a source and its local mirror without affecting installed recipes.
6. **Deploy a task**: go to `Tasks → Deploy Task`, then choose a recipe and cluster, assign head/worker roles and ranks (the head is always rank 0), configure variables, preview, and deploy. Both preview and deployment refresh node data from the selected Agents first, preventing templates from using stale hardware values.
7. **Operate tasks and assets**: inspect logs; pause, resume, stop, or delete tasks; run health checks; and track model and image distribution. A single real-time stream handles both log history and live updates. Deleting a task also removes its inference and benchmark data; see [Task lifecycle](docs/task-lifecycle.md). Models and images download to the control plane with one click. After completion, distribute from the cache/archive list by selecting a cluster; all member nodes are selected by default and the first is the head. [Model transfer](docs/model-transfer.md) and [Image transfer](docs/image-transfer.md) describe their state machines and recovery behavior.

![Fireworks task details showing node roles, live inference metrics, benchmarks, and logs](docs/images/task.png)

_Task details show each node's container and rank, live LLM inference statistics, benchmarks, and continuous logs._

Existing high-speed IPs may use different subnets on different nodes. As long as the matching rails share the same Layer 2 switched network, active ARP discovery can still verify physical connectivity and let the Web UI reconfigure them consistently. See [High-speed network automation](docs/networking.md) for topology requirements, address planning, create/add-node state machines, rollback boundaries, and error handling.

> Want to change the code? See [CONTRIBUTING.md](CONTRIBUTING.md) for local backend, frontend, and test workflows.

## Deployment and storage

### Network and ports

- **HTTP**: access the frontend directly on port `3000` and explicitly set `COOKIE_SECURE=0`. Use this only on a local machine or trusted LAN.
- **HTTPS**: terminate TLS with nginx, HAProxy, Caddy, or another reverse proxy and keep Fireworks' production default `COOKIE_SECURE=1`. See [`deploy/nginx-fireworks.conf.example`](deploy/nginx-fireworks.conf.example).
- **Ports**: backend port `8000` is used by node Agents to pull models and images over the management network. It must stay bound to all host interfaces so Agents entering through different management-network interfaces can reach it. Frontend port `3000` serves browsers or the reverse proxy. Restrict access with the host firewall or network ACLs rather than narrowing Compose's bind address.

### Volumes and large-model capacity

| Storage | Container path | Contents | Recommended medium |
|---|---|---|---|
| `fireworks-db` | `/data/db` | SQLite database and audit logs | SSD; back up regularly |
| `fireworks-cache` | `/data/cache` | Control-plane model cache, image archives, and recipe-source mirrors | Large SSD or HDD; contents can be fetched again |

Both stores survive container recreation and ordinary `docker compose down`. Do not run `docker compose down -v` unless you intend to delete the database and cache volumes.

Named volumes have no separate default quota, but their capacity depends on Docker's data disk. On Linux, Docker Engine normally stores them on the filesystem containing Docker Root Dir. In Docker Desktop, they live in a virtual disk and are also constrained by the Desktop disk-size setting. The default configuration therefore does **not** guarantee enough room for large models.

Model downloads first store shards and then merge them into the destination file. Peak usage during a merge is approximately the completed model files plus the current file's shards plus the current destination file. Reserve at least the **total model size plus the largest individual file**. If the shard layout is unknown, budget about **twice the total model size**, plus room for image archives and future models. Copies stored on individual nodes are separate from `fireworks-cache`, so check every node's disk as well.

For large-model deployments, create a `.env` file in the repository root before the first launch and place the cache on a host disk with known capacity. You do not need to edit the Compose files. This Linux Docker Engine example binds both stores to the host; with Docker Desktop, it is usually better to retain the named database volume and bind only the cache:

```dotenv
FIREWORKS_DB_PATH=/mnt/ssd/fireworks/db
FIREWORKS_CACHE_PATH=/mnt/hdd/fireworks/cache
```

### Bind host directories by operating system

Run the commands below from the Fireworks repository root. They create or overwrite `.env`; if you already have one, add these settings manually instead. Put the database on a local Linux filesystem with reliable file locking and persistence, such as ext4 or XFS, not NFS, SMB, or exFAT. The large model cache can live on a separate high-capacity disk. On macOS and Windows with Docker Desktop, keeping the database in a named volume generally provides better SQLite I/O performance, so bind only the model cache.

#### Linux

Use `lsblk -f` and `df -h` to find a mounted disk with enough room. Replace `/mnt/large-disk` below with its real mount point. Merely creating a directory with that name may still put data on the system disk. The database example uses local `/var/lib` storage.

```bash
FW_DB_PATH="/var/lib/fireworks/db"
FW_CACHE_PATH="/mnt/large-disk/fireworks/cache"
df -h /mnt/large-disk
sudo mkdir -p "$FW_DB_PATH" "$FW_CACHE_PATH"
sudo chown -R "$(id -u):$(id -g)" "$(dirname "$FW_DB_PATH")" "$(dirname "$FW_CACHE_PATH")"
printf 'FIREWORKS_DB_PATH="%s"\nFIREWORKS_CACHE_PATH="%s"\n' \
  "$FW_DB_PATH" "$FW_CACHE_PATH" > .env
docker compose -f docker-compose.prod.yml config
```

For the Mainland China registry, use `docker compose -f docker-compose.prod.cn.yml config` on the final line. In the output, `/data/db` and `/data/cache` should be `type: bind` and point to the selected paths. Confirm this before running an HTTP or HTTPS start command from Quick start.

#### macOS

Keep the database in a Docker Desktop named volume and bind only the model cache to a large external disk. Run `ls /Volumes` to see volume names and replace `ModelDisk` below with the real name:

```bash
FW_CACHE_PATH="/Volumes/ModelDisk/FireworksCache"
df -h /Volumes/ModelDisk
mkdir -p "$FW_CACHE_PATH"
printf 'FIREWORKS_CACHE_PATH="%s"\n' "$FW_CACHE_PATH" > .env
docker compose -f docker-compose.prod.yml config
```

If Docker reports `Mounts denied`, allow the external volume in Docker Desktop's file-sharing settings and retry. `/data/db` should be `type: volume`, while `/data/cache` should be `type: bind`. For the Mainland China registry, use `docker-compose.prod.cn.yml` in the validation command.

#### Windows (PowerShell)

Run `Get-PSDrive -PSProvider FileSystem` to review free space on each drive. Keep the database in a Docker Desktop named volume to avoid SQLite I/O overhead across the Windows/Linux VM boundary. Replace `D:` in the cache example with a drive that has sufficient capacity:

```powershell
$CachePath = "D:\FireworksCache"
New-Item -ItemType Directory -Force -Path $CachePath | Out-Null
$CacheComposePath = $CachePath.Replace('\', '/')
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllLines(
  (Join-Path $PWD ".env"),
  @("FIREWORKS_CACHE_PATH=`"$CacheComposePath`""),
  $Utf8NoBom
)
docker compose -f docker-compose.prod.yml config
```

In the output, `/data/db` should be `type: volume`; `/data/cache` should be `type: bind` with a source path such as `D:/FireworksCache`. If Docker Desktop cannot share the drive, allow it in settings and retry. For the Mainland China registry, use `docker-compose.prod.cn.yml` in the validation command and use the corresponding PowerShell start command above.

Before switching an existing named-volume deployment to host paths, stop the services and migrate the data manually. Compose does not copy old volume contents. After the new deployment starts, run `docker compose -f docker-compose.prod.cn.yml ps` or `docker compose -f docker-compose.prod.yml ps` and confirm that both services are healthy.

Use `docker system df` to inspect Docker disk usage. On Linux, `docker info --format '{{.DockerRootDir}}'` locates the data root, and `df -h` shows free space on its filesystem. Back up the database volume. Model and image caches can be downloaded again after you confirm they are no longer needed.

v0.2.0 can reuse the v0.1.1 `fireworks-db`; startup removes legacy inference-stat samples. Back up the database before upgrading, then redeploy every node Agent after the control plane is updated. See the [v0.2.0 release notes](docs/releases/v0.2.0.md).

v0.2.1 is a maintenance release: on startup it runs an idempotent primary-key monotonicity migration for `clusters` / `nodes` / `recipes` (seconds, data preserved) and heals interrupted residue; back up the database volume before upgrading and redeploy node Agents to keep capabilities aligned. See the [v0.2.1 release notes](docs/releases/v0.2.1.md).

## Architecture

```text
┌────────────── Control plane (Docker Compose) ──────────────┐
│  Nuxt 4 frontend (3000) ──/api proxy──► FastAPI backend (8000) │
│                                        SQLite metrics DB  │
└──────────────────────┬─────────────────────────────┘
                SSH deployment │ REST (9000)
       ┌───────────────┬──────────────────┐
       ▼               ▼                  ▼
   Node Agent       Node Agent         Node Agent
   (metrics / Docker Compose / network tests; head/worker selected per task)
```

The request path is Nuxt frontend → FastAPI control plane → one lightweight Agent per node. The Agent is deployed automatically over SSH when a node is added, collects metrics, runs containerized tasks, and performs network tests.

## Repository layout

```text
├── docker-compose.yml          # Development: build backend and frontend locally
├── docker-compose.prod.yml     # Production, international: prebuilt GHCR images
├── docker-compose.prod.cn.yml  # Production, Mainland China: Alibaba Cloud images
├── .github/workflows/          # CI validation and release image publishing
├── deploy/nginx-fireworks.conf.example  # TLS and WebSocket reverse-proxy example
├── docs/networking.md          # High-speed network discovery, configuration, validation
├── docs/task-lifecycle.md      # Node refresh, continuous logs, and data cleanup
├── docs/model-transfer.md      # Model manifest, Agent transfer, progress, and recovery
├── docs/image-transfer.md      # Image pull, Agent transfer, progress, and recovery
├── docs/releases/              # Per-release installation, upgrade, and component notes
├── docs/releasing.md           # Maintainer release, verification, and rollback checklist
├── agent/                      # Lightweight node Agent and offline deployment scripts
├── backend/app/                # FastAPI control plane
└── frontend/app/               # Nuxt 4 frontend and server API proxy
```

## Environment variables

Backend control-plane settings can be overridden in Compose:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:////data/db/fireworks.db` | Database path |
| `COOKIE_SECURE` | Disabled by the backend; production Compose defaults to `1` | Explicitly use `0` for HTTP; retain the production default for HTTPS |
| `SESSION_TTL_HOURS` | `168` | Login session lifetime in hours |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins; normally irrelevant for same-origin deployments |
| `METRIC_POLL_INTERVAL` | `5` | Metrics polling interval in seconds |
| `METRIC_RETENTION_HOURS` | `24` | Metrics retention period in hours |
| `INFERENCE_RETENTION_HOURS` | `25` | Inference snapshot retention; keeps an extra hour as the 24-hour window baseline |
| `AGENT_PORT` / `AGENT_DEPLOY_DIR` | `9000` / `/opt/fireworks-agent` | Agent listening port and installation directory |
| `API_PROXY_TARGET` | `http://backend:8000` | Frontend `/api` proxy target |

> The address used by Agents to pull models and images needs no manual configuration. Fireworks infers the control-plane address from the source IP of the request sent to each Agent.
