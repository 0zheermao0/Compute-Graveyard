# Lab GPU Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

实验室 GPU / 计算容器统一申请与管理平台。支持 GPU 与纯 CPU 容器申请、租期管理、工作区文件编辑、Code Server（浏览器版 VS Code）和 Docker 容器生命周期管理。

系统支持三种部署角色：

- **Standalone**：单机独立运行，适合个人或单服务器部署。
- **Master**：主节点，保留完整本地功能，并通过 Agent 统一查看和管理多个 Worker 节点。
- **Worker**：从节点，保留完整用户界面和本地功能，同时向 Master 提供受保护的 Agent API。

---

## 功能特性

- **资源看板**：GPU 数字孪生、CPU/内存/磁盘负载、容器占用和使用时长排行
- **多节点管理**：Master 注册 Worker、测试连接、查看节点资源、启用/禁用调度
- **容器申请**：选择 GPU 或纯 CPU、租期（1–7 天），自动分配 SSH 和服务端口
- **节点调度**：支持自动调度、本机节点和指定节点
- **容器管理**：查看、停止、删除、续租容器；Master 可管理已注册 Worker 上的容器
- **GPU 共用审批**：当目标 GPU 已被其他用户使用时，向占用者发起审批
- **磁盘配额**：按用户限制工作区容量，超限后阻止新申请并可自动停止容器
- **GPU 低利用回收**：连续低利用达到阈值后自动停止并销毁 GPU 容器，保留工作区
- **工作区文件**：在线浏览、编辑、新建和删除个人文件
- **用户与权限**：注册、登录、管理员审批和用户管理
- **安全 Agent API**：Worker 仅开放受保护的资源查询和容器操作接口，不暴露 Docker API
- **用户容器镜像**：支持 NVIDIA CUDA、Miniconda、code-server 和 SSH，个人目录挂载至 `/workspace`

---

## 主从架构

```text
浏览器
   │
   ▼
Master: 用户认证、资源聚合、调度、远程容器管理
   │ HTTPS/HTTP + Bearer Agent Token
   ├──────────────► Worker 1: 本地 Docker / GPU / 工作区
   ├──────────────► Worker 2: 本地 Docker / GPU / 工作区
   └──────────────► Worker N: 本地 Docker / GPU / 工作区
```

| 角色 | 用户 UI/API | 本地 Docker | Agent API | 定时任务 | 主要用途 |
|------|-------------|-------------|-----------|----------|----------|
| `standalone` | 完整 | 是 | 否 | 是 | 单机部署 |
| `master` | 完整 | 是 | 否 | 是 | 统一管理本机和 Worker |
| `worker` | 完整 | 是 | 是 | 是 | 独立运行本地功能并接受 Master 管理 |

多节点场景建议使用 `master` 作为统一入口。`standalone` 主要用于单机部署；虽然管理员接口可以保存节点配置，但只有 `master` 会在看板中聚合远程节点资源。

每个节点使用独立数据库、配置和本地工作区，用户账号不会自动同步。Master 创建的远程容器记录保存在 Master 数据库中；Worker 的直接访问页面主要管理该节点本地创建的业务记录。

工作区默认按节点本地保存，不在节点之间自动同步。容器部署到 Worker 后，文件应通过该 Worker 的工作区页面或容器自身的 Code Server 访问；容器迁移不会自动迁移工作区。

---

## 环境要求

每台运行管理服务的服务器都需要：

- Docker 与 Docker Compose
- 如需 GPU 信息和 GPU 容器：NVIDIA 驱动与 `nvidia-container-toolkit`
- 用户容器镜像 `lab-image:latest`，或可访问的统一镜像仓库
- 宿主机开放 SSH 和服务端口段
- Master 能访问所有 Worker 的 Agent 地址
- 用户访问端能访问 `NODE_PUBLIC_HOST` 对应的 SSH 和服务端口

默认 `docker-compose.yml` 为 GPU 节点配置了 NVIDIA 设备预留；没有 NVIDIA 设备的纯 CPU 服务器需要使用自定义 Compose 覆盖文件移除 `deploy.resources.reservations.devices` 配置。GPU 节点也可以申请纯 CPU 容器。

建议生产环境使用反向代理提供 HTTPS，并通过防火墙限制 Worker Agent 端口仅允许 Master 访问。

---

## 快速开始：单机 Standalone

### 1. 构建用户容器镜像

用户申请的计算容器使用 `lab_image/` 中的镜像。GPU 节点需要在该节点构建或拉取镜像。

```bash
cd lab_image
docker build -t lab-image:latest .
cd ..
```

### 2. 创建环境配置

在项目根目录创建 `.env`：

```dotenv
JWT_SECRET=replace-with-a-long-random-value
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD=replace-with-a-strong-password
NODE_ROLE=standalone
NODE_ID=local
NODE_NAME=Local GPU Node
NODE_PUBLIC_HOST=127.0.0.1
NODE_SERVICE_SCHEME=http
USER_DATA_BASE=/data1
```

`JWT_SECRET` 和 `INITIAL_ADMIN_PASSWORD` 没有安全默认值，生产环境必须显式配置。应用首次启动且数据库为空时，会自动创建初始管理员。

生成随机密钥示例：

```bash
openssl rand -hex 32
```

### 3. 启动管理服务

```bash
docker compose up --build -d
```

默认访问：`http://localhost:8099`。

查看日志：

```bash
docker compose logs -f lab-gpu-manager
```

### 4. 首次登录

使用 `.env` 中的 `INITIAL_ADMIN_USERNAME` 和 `INITIAL_ADMIN_PASSWORD` 登录。旧版本文档中的固定 `admin/admin123` 不再作为默认凭据。

兼容初始化接口：

```text
POST /api/auth/init-admin
Authorization: Bearer <INIT_ADMIN_TOKEN>
```

该接口要求同时配置 `INIT_ADMIN_TOKEN` 和 `INITIAL_ADMIN_PASSWORD`，且数据库必须完全为空。由于应用启动时会优先使用 `INITIAL_ADMIN_PASSWORD` 自动创建管理员，正常启动后该接口通常返回 `409`；无论何种情况都不会重置已有管理员密码。

---

## 快速开始：Master + Worker

### 1. 在每台服务器准备镜像和目录

Master 和每个 Worker 都需要执行：

```bash
cd lab_image
docker build -t lab-image:latest .
cd ..
mkdir -p /data1 /data/public_datasets
```

如果使用私有镜像仓库，请确保每台节点都能拉取相同镜像版本。每台机器的 `lab-gpu-data` 和用户工作区都是本地数据卷，不会自动复制。

### 2. 配置 Master

Master 主机的 `.env` 示例：

```dotenv
JWT_SECRET=master-jwt-secret
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD=master-admin-password
NODE_ROLE=master
NODE_ID=master
NODE_NAME=GPU Master
NODE_PUBLIC_HOST=master.example.com
NODE_SERVICE_SCHEME=https
USER_DATA_BASE=/data1
PUBLIC_DATASETS_HOST_PATH=/data/public_datasets
PUBLIC_DATASETS=/data/public_datasets
```

启动：

```bash
docker compose up --build -d
```

`NODE_PUBLIC_HOST` 是用户访问 Master 本机容器服务时使用的主机名或 IP，只填写主机名/IP，不要包含协议或端口。

### 3. 配置 Worker

每个 Worker 使用不同的 `NODE_ID` 和独立的 Agent token。Worker 主机的 `.env` 示例：

```dotenv
JWT_SECRET=worker-01-jwt-secret
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD=worker-01-admin-password
NODE_ROLE=worker
NODE_ID=worker-01
NODE_NAME=GPU Worker 01
NODE_PUBLIC_HOST=worker-01.example.com
NODE_SERVICE_SCHEME=https
AGENT_API_TOKEN=worker-01-agent-token
AGENT_REQUEST_TIMEOUT_SECONDS=10
USER_DATA_BASE=/data1
PUBLIC_DATASETS_HOST_PATH=/data/public_datasets
PUBLIC_DATASETS=/data/public_datasets
```

启动：

```bash
docker compose up --build -d
```

Worker 的 `AGENT_API_TOKEN` 必须是非空随机值，并在 Master 管理后台添加节点时填写同一个值。每个 Worker 应使用不同 token。

### 4. 在 Master 注册 Worker

1. 使用管理员账号登录 Master。
2. 打开「管理后台」中的「计算节点管理」。
3. 填写：
   - **节点 ID**：必须与 Worker 的 `NODE_ID` 完全一致，例如 `worker-01`。
   - **节点名称**：用于界面显示。
   - **Agent 地址**：例如 `https://worker-01.example.com:8099`，不要附加 `/api/agent/v1` 路径。
   - **公开访问主机**：例如 `worker-01.example.com`，不要包含协议或端口。
   - **Agent Token**：填写 Worker 的 `AGENT_API_TOKEN`。
4. 点击「添加节点」后点击「连接测试」。
5. 确认节点在线后，根据需要切换「启用」和「可调度」。

Master 会通过以下地址调用 Worker：

```text
<Agent 地址>/api/agent/v1/health
<Agent 地址>/api/agent/v1/inventory
```

Master 保存节点配置时不会在 API 响应中返回 Agent token。节点删除前必须先清理该节点关联的未归档容器。

### 5. 申请容器

在 Master 的「资源看板」打开申请窗口，可选择：

- **自动调度**：从在线、启用且可调度的节点中选择资源满足要求的节点。
- **本机节点**：只使用当前 Master 节点。
- **指定节点**：选择一个在线且可调度的节点，再选择该节点的 GPU。

容器创建成功后，系统返回节点名称、SSH 主机、SSH 端口和服务 URL。用户不应再使用 Master 的主机名替换 Worker 主机名。

---

## 配置说明

Docker Compose 会读取项目根目录的 `.env`，也可以直接设置环境变量。当前 Compose 将 `USER_DATA_BASE` 同时作为管理容器内路径和 Docker 宿主机路径使用，因此该路径必须在宿主机和管理容器中保持一致；如果宿主机路径不同，需要调整 Docker Compose 挂载配置和节点运行时配置。

### 节点和 Agent

| 变量 | 说明 | 默认 |
|------|------|------|
| `NODE_ROLE` | 节点角色：`standalone`、`master` 或 `worker` | `standalone` |
| `NODE_ID` | 节点唯一 ID；Master 注册 Worker 时必须一致 | `local` |
| `NODE_NAME` | 节点显示名称 | 应用默认 `NODE_ID`；Compose 默认 `Local GPU Node` |
| `NODE_PUBLIC_HOST` | 用户访问该节点 SSH/服务端口的主机名或 IP，不含协议和端口 | `localhost` |
| `NODE_SERVICE_SCHEME` | 服务 URL 协议，只能是 `http` 或 `https`；不会自动启用 TLS | `http` |
| `AGENT_API_TOKEN` | Worker Agent Bearer token；Master/Standalone 不需要 | 空 |
| `AGENT_REQUEST_TIMEOUT_SECONDS` | Master 调用 Worker Agent 的超时时间 | `10` |
| `PUBLIC_DATASETS_HOST_PATH` | Compose 宿主机公共数据集路径 | `/data/public_datasets` |

### 初始化、安全和数据库

| 变量 | 说明 | 默认 |
|------|------|------|
| `JWT_SECRET` | 用户 JWT 签名密钥；生产环境必须使用强随机值 | 无安全默认值 |
| `INITIAL_ADMIN_USERNAME` | 空数据库首次启动时创建的管理员用户名 | `admin` |
| `INITIAL_ADMIN_PASSWORD` | 空数据库首次启动时创建的管理员密码；为空则不自动创建 | 空 |
| `INIT_ADMIN_TOKEN` | `/api/auth/init-admin` 所需的初始化 Bearer token | 空 |
| `CORS_ORIGINS` | 允许的跨域来源，多个值用逗号分隔 | 空 |
| `NOTIFY_WEBHOOK` | 到期、配额等通知的钉钉/飞书 Webhook | 空 |
| `DATABASE_URL` | 数据库连接；默认使用当前节点本地 SQLite | `sqlite:///./data/lab_gpu.db` |
| `DATA_DIR` | 应用数据目录；Compose 默认容器内 `/app/data` | `backend/data`（开发） |

主从模式默认使用各节点独立 SQLite。不要把多个主从实例指向同一个 SQLite 文件；如果改用其他数据库，需要自行准备对应驱动和连接配置。

### 工作区、镜像和端口

| 变量 | 说明 | 默认 |
|------|------|------|
| `USER_DATA_BASE` | 应用和用户容器看到的工作区根目录 | `/data/users`；Compose 默认 `/data1` |
| `PUBLIC_DATASETS` | 公共数据集目录，只读挂载到容器 `/datasets` | `/data/public_datasets` |
| `DOCKER_BASE_IMAGE` | 用户容器镜像 | `nvidia/cuda:12.0-runtime-ubuntu22.04`；Compose 默认 `lab-image:latest` |
| `SSH_PORT_START` / `SSH_PORT_END` | SSH 宿主机端口池，左闭右开 | `20000`–`21000` |
| `SERVICE_PORT_START` / `SERVICE_PORT_END` | Jupyter、TensorBoard、Code Server 宿主机端口池，左闭右开 | `30000`–`40000` |

容器内固定服务端口为：`8888`（Jupyter）、`6006`（TensorBoard）、`8080`（Code Server）。端口池需要在每个节点分别预留，用户访问地址使用该节点的 `NODE_PUBLIC_HOST`。

### 租期和资源限制

| 变量 | 说明 | 默认 |
|------|------|------|
| `DEFAULT_LEASE_DAYS` / `MAX_LEASE_DAYS` | 默认和最大租期 | `3` / `7` |
| `MAX_GPUS_PER_USER` | 每个用户最多同时使用的 GPU 数 | `2` |
| `MAX_CONTAINERS_PER_USER` | 每个用户最多同时拥有的运行中或待审批容器数 | `4` |
| `DEFAULT_CPU_MEM_GB` | CPU 容器默认内存限制 | `8` GB |
| `DEFAULT_GPU_MEM_GB_PER_GPU` | 每块 GPU 对应的默认容器内存限制 | `32` GB |
| `DEFAULT_MAX_GPU_SHARING_USERS` | 单张 GPU 最多允许的不同用户数 | `4` |

### 磁盘配额

| 变量 | 说明 | 默认 |
|------|------|------|
| `DEFAULT_DISK_QUOTA_GB` | 新用户默认工作区配额 | `100` GiB |
| `DISK_QUOTA_SCAN_INTERVAL_MINUTES` | 后台扫描周期 | `5` 分钟 |
| `DISK_QUOTA_GRACE_HOURS` | 超限持续时间达到后停止用户运行中容器 | `24` 小时 |

规则：

- 统计 `USER_DATA_BASE/<username>` 下的普通文件，不跟随符号链接。
- 使用量达到或超过配额后，新的容器申请会被拒绝。
- 扫描失败时采取 fail-closed 策略，同样拒绝新申请。
- 超限持续达到宽限期后，停止该用户所有运行中容器。
- 停止超过 24 小时的容器由后台任务清理，工作区目录保留。
- 管理员账号免受用户磁盘配额限制。
- 配额是软限制，工作区写入接口不会因为刚刚超限而回滚写入。
- Master 上的配额扫描只覆盖 Master 本地工作区；Worker 本地工作区应在对应 Worker 上独立配置和管理。

### GPU 低利用自动回收

| 变量 | 说明 | 默认 |
|------|------|------|
| `IDLE_GPU_RECLAIM_ENABLED` | 环境默认是否启用，管理员后台可动态修改 | `true` |
| `IDLE_GPU_UTIL_THRESHOLD_PERCENT` | GPU 利用率阈值 | `5`% |
| `IDLE_GPU_MEMORY_THRESHOLD_PERCENT` | 整卡显存占用阈值 | `5`% |
| `IDLE_GPU_DURATION_HOURS` | 连续低利用时长 | `24` 小时 |

规则：

- 只处理 GPU 容器，不处理 CPU 容器。
- 分配给容器的每一张 GPU 都必须同时低于利用率和显存阈值。
- 达到连续低利用时长后立即停止并删除容器，宿主机工作区保留。
- 指标缺失、采样中断超过 15 分钟、策略变化或出现高利用率时会重置计时。
- Master 会按容器所属节点执行本地或远程回收。
- 每个节点都会运行自己的本地定时任务；不要让多个服务实例共享同一节点数据库。

---

## Agent API

Agent API 只在 `NODE_ROLE=worker` 时提供有效响应，所有接口都需要：

```http
Authorization: Bearer <AGENT_API_TOKEN>
```

接口列表：

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/agent/v1/health` | Agent 健康检查和节点身份 |
| `GET` | `/api/agent/v1/inventory` | GPU、系统负载和受管容器清单 |
| `GET` | `/api/agent/v1/containers` | 受管容器列表 |
| `POST` | `/api/agent/v1/containers` | 创建容器 |
| `POST` | `/api/agent/v1/containers/{container_id}/stop` | 停止容器 |
| `DELETE` | `/api/agent/v1/containers/{container_id}` | 删除容器 |

Agent 的停止和删除接口只允许操作带有 `compute-graveyard.managed=true` 标签的容器。不要向公网暴露 Docker TCP API；建议只开放 Worker 管理端口给 Master，并在反向代理层启用 HTTPS。

---

## 容器申请 API

申请接口：

```http
POST /api/containers/apply
Authorization: Bearer <用户 JWT>
Content-Type: application/json
```

指定节点申请 GPU 示例：

```json
{
  "cpu_only": false,
  "gpu_ids": [0],
  "lease_days": 3,
  "placement_mode": "specific",
  "node_id": "worker-01"
}
```

调度方式：

- `local`：强制使用当前节点；`node_id` 会被忽略。
- `specific`：必须提供 `node_id`，目标节点必须在线、启用且可调度。
- `auto`：Master 从在线、启用且可调度节点中自动选择；API 默认值为 `local`，多节点前端默认使用 `auto`。

远程 Worker 上已经存在的本地容器会通过 Agent inventory 计入 GPU 占用。由于用户账号和审批记录不跨节点同步，Master 在远程节点上只调度空闲 GPU；Worker 本地用户之间的 GPU 共用审批仍在 Worker 页面完成，Master 创建的远程容器暂不参与跨节点共用审批。

容器响应中的关键访问字段：

- `node_id`、`node_name`
- `access_host`、`ssh_host`
- `ssh_port`、`ssh_url`
- `extra_ports`
- `service_urls`

不要再根据浏览器当前 hostname 拼接远程容器地址，应直接使用后端返回的 `ssh_host` 和 `service_urls`。

---

## 主要 API

### 用户功能

- `POST /api/auth/register`：注册
- `POST /api/auth/login`：登录
- `GET /api/auth/me` / `PATCH /api/auth/me`：当前用户信息和修改
- `GET /api/dashboard`：资源看板和节点聚合资源
- `POST /api/containers/apply`：申请容器
- `GET /api/containers/my`：我的容器
- `DELETE /api/containers/{id}`：删除容器或取消待审批申请
- `POST /api/leases/renew/{id}`：续租
- `GET /api/workspace/list`：工作区目录列表
- `GET /api/workspace/file`：读取文件
- `PUT /api/workspace/file`：写入文件
- `POST /api/workspace/dir`：创建目录
- `DELETE /api/workspace`：删除文件或空目录

### 管理功能

- `GET /api/admin/nodes`：节点列表
- `POST /api/admin/nodes`：添加 Worker
- `PATCH /api/admin/nodes/{node_id}`：修改节点配置
- `DELETE /api/admin/nodes/{node_id}`：删除远程节点
- `POST /api/admin/nodes/{node_id}/test`：测试节点连接
- `GET /api/admin/nodes/inventory`：聚合所有启用节点资源
- `GET /api/admin/containers`：所有容器
- `POST /api/admin/containers/{id}/force-stop`：强制停止
- `POST /api/admin/containers/{id}/force-remove`：强制清理
- `GET /api/admin/settings` / `PUT /api/admin/settings`：动态资源和回收设置
- `GET /api/admin/users`：用户列表和配额
- `PUT /api/admin/users/{id}/quota`：修改用户配额
- `POST /api/admin/users/{id}/quota/refresh`：刷新用户用量

---

## 部署注意

1. **密钥**：为每个节点配置独立 `JWT_SECRET`；为每个 Worker 配置不同的 `AGENT_API_TOKEN`。
2. **初始化**：不要依赖固定默认管理员密码；使用 `INITIAL_ADMIN_PASSWORD` 创建初始管理员，并在部署后修改密码。
3. **HTTPS**：`NODE_SERVICE_SCHEME=https` 只改变返回的服务 URL，不会为 Uvicorn 自动启用 TLS；需要在前置 Nginx、Traefik 或 Caddy 中配置证书。
4. **Agent 网络**：Worker Agent 地址应限制为 Master 可访问；不要暴露远程 Docker Socket。
5. **端口**：每个节点分别检查 SSH/服务端口池、防火墙和云安全组。
6. **数据**：备份每个节点的 `lab-gpu-data` 卷和 `USER_DATA_BASE` 对应的宿主机目录，不要把不同节点的 SQLite 文件放到同一网络目录。
7. **工作区**：工作区按节点独立保存，远程容器的文件访问应使用对应 Worker 的工作区或 Code Server。
8. **镜像**：每个可调度节点都必须具备同名用户容器镜像，或能够从镜像仓库拉取。
9. **资源状态**：Master 看板依赖 Worker Agent inventory；节点离线时会显示离线并停止参与自动调度。
10. **容器权限**：管理服务需要访问本机 Docker Socket，因此应只部署在受信任的管理网络中。

---

## 项目结构

```text
.
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── agent.py          # Worker Agent API
│   │   │   ├── admin.py          # 用户、节点和系统管理
│   │   │   ├── containers.py     # 容器申请和生命周期
│   │   │   └── ...
│   │   ├── node_service.py       # 节点发现、资源聚合和调度
│   │   ├── remote_agent.py       # Master 到 Worker 的 Agent 客户端
│   │   ├── docker_service.py     # 本地 Docker、GPU 和端口操作
│   │   ├── container_lifecycle.py
│   │   ├── scheduler.py          # 到期、配额和低利用回收
│   │   ├── config.py              # 环境变量配置
│   │   └── database*.py           # 数据库和模型
│   ├── main.py
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/           # Layout、GPUTwin、ApplyModal 等
│   │   └── pages/                # Dashboard、Admin、MyContainers、Workspace 等
│   └── package.json
├── lab_image/                    # 用户计算容器镜像
├── docker-compose.yml
├── Dockerfile                    # 管理服务镜像
├── LICENSE
└── README.md
```

---

## 开发与验证

### 后端

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev
```

开发环境 Vite 默认把 `/api` 代理到 `http://localhost:8000`。

### 测试

```bash
cd backend
python -m pytest -q
python -m compileall -q app main.py

cd ../frontend
npm run build
```

---

## 许可证

本项目采用 [MIT License](LICENSE)。
