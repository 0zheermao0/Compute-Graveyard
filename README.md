# Lab GPU Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

实验室 GPU / 计算容器统一申请与调度平台。支持 GPU 与纯 CPU 容器申请、租期管理、工作区文件编辑与 Code Server（VS Code 浏览器版）直连，界面风格「Compute Graveyard / 算力坟场」。

---

## 功能特性

- **资源看板**：GPU 数字孪生展示、系统负载、占用列表、使用时长排行
- **容器申请**：选择 GPU 或纯 CPU、租期（1–7 天），自动分配 SSH 端口与随机密码
- **我的容器**：表格查看所有容器，SSH 端口/密码、服务端口（Jupyter、TensorBoard、Code Server），一键打开 Code Server、复制 SSH 命令、续租
- **工作区文件**：在线浏览与编辑个人目录（Monaco Editor），新建/删除文件与目录
- **用户与权限**：注册、登录、个人资料编辑；管理员审批、用户管理
- **容器镜像**：基于 NVIDIA CUDA + Miniconda，预装 code-server（与 SSH 同密码）、SSH，个人目录挂载至 `/workspace`

---
## TODO List
- 🔲 用户的个人 webhook 通知
- 🔲 多集群/服务器支持
- 🔲 参数管理员可配置


## 技术栈

| 层级     | 技术 |
|----------|------|
| 前端     | React 18, TypeScript, Vite, Motion, Monaco Editor, Lucide Icons |
| 后端     | Python 3.11, FastAPI, SQLAlchemy, JWT, APScheduler |
| 容器     | Docker（宿主机 Docker Socket），用户容器镜像见 `lab_image/` |
| 部署     | 单镜像前后端一体（Node 构建前端 + Uvicorn  serving） |

---

## 环境要求

- Docker 与 Docker Compose
- 如需 GPU 信息与 GPU 容器：宿主机安装 **nvidia-container-toolkit**
- 宿主机预留端口段：SSH（如 20000–21000）、服务端口（如 30000–40000）

---

## 快速开始

### 1. 构建用户容器镜像（lab_image）

用户申请到的容器将使用该镜像，内含 CUDA、Conda、code-server、SSH 等。

```bash
cd lab_image
docker build -t lab-image:latest .
cd ..
```

### 2. 使用 Docker Compose 启动

```bash
# 复制并编辑环境（可选）
cp .env.example .env

# 启动（前台可加 -d 后台）
docker compose up --build
```

默认访问：**http://localhost:8099**（compose 中映射为 `8099:3000`）。

### 3. 首次使用

- 若数据库无用户，可调用初始化接口创建管理员：  
  `POST http://localhost:8099/api/auth/init-admin`  
  （默认账号 `admin` / `admin123`，仅当无用户或 admin 密码错误时生效）
- 登录后即可在「资源看板」申请 GPU/CPU 容器，在「我的容器」查看并打开 Code Server。

---

## 配置说明

通过环境变量配置（`docker-compose.yml` 或 `.env`）：

| 变量 | 说明 | 默认 |
|------|------|------|
| `USER_DATA_BASE` | 用户工作区根目录（宿主机路径，挂载为容器内 `/workspace`） | `/data/users` |
| `DOCKER_BASE_IMAGE` | 用户容器镜像名 | `lab-image:latest` |
| `SSH_PORT_START` / `SSH_PORT_END` | SSH 端口池 | `20000`–`21000` |
| `SERVICE_PORT_START` / `SERVICE_PORT_END` | Jupyter/TensorBoard/Code Server 映射端口池 | `30000`–`40000` |
| `DEFAULT_LEASE_DAYS` / `MAX_LEASE_DAYS` | 默认与最大租期（天） | `3` / `7` |
| `MAX_GPUS_PER_USER` / `MAX_CONTAINERS_PER_USER` | 每用户最大 GPU 数 / 最大同时容器数 | `2` / `4` |
| `DATABASE_URL` | 数据库连接（SQLite 或 PostgreSQL 等） | `sqlite:///./data/lab_gpu.db` |
| `JWT_SECRET` | JWT 签名密钥 | **务必在生产环境修改** |
| `PUBLIC_DATASETS` | 可选，公共数据集路径（只读挂载到容器 `/datasets`） | - |
| `NOTIFY_WEBHOOK` | 可选，钉钉/飞书等 Webhook | - |

Compose 中需把宿主机用户目录挂载进管理服务，例如：

```yaml
volumes:
  - /data1:/data1   # USER_DATA_BASE=/data1 时，/data1/{username} 即对应用户工作区
```

---

## 项目结构

```
.
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── api/             # 路由：auth, dashboard, containers, leases, admin, workspace
│   │   ├── auth.py          # JWT、密码
│   │   ├── config.py        # 配置项
│   │   ├── database*.py     # 数据库与模型
│   │   └── docker_service.py # 容器创建、端口分配、GPU 信息
│   ├── main.py              # 应用入口
│   └── requirements.txt
├── frontend/                # React 前端
│   ├── src/
│   │   ├── components/      # Layout, GPUTwin, ApplyModal 等
│   │   ├── pages/           # Dashboard, MyContainers, Workspace, Profile, Admin
│   │   └── api/
│   └── package.json
├── lab_image/               # 用户容器镜像（CUDA + Conda + code-server + SSH）
│   ├── Dockerfile
│   └── entrypoint.sh
├── docker-compose.yml
├── Dockerfile               # 管理服务镜像（前后端一体）
├── LICENSE
└── README.md
```

---

## 开发

### 后端

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端开发时需配置 API 代理到后端（如 Vite `proxy` 到 `http://localhost:8000`），或通过环境变量指定 API 地址。

### 数据库

默认 SQLite，库文件位于 `backend/data/`（或 `DATA_DIR` 指定路径）。迁移与表结构见 `app/database.py`。

---

## API 概览

- `POST /api/auth/register` — 注册  
- `POST /api/auth/login` — 登录  
- `GET /api/auth/me` / `PATCH /api/auth/me` — 当前用户信息与修改  
- `GET /api/dashboard` — 看板数据（GPU、负载、占用、排行）  
- `POST /api/containers/apply` — 申请容器  
- `GET /api/containers/my` — 我的容器列表  
- `POST /api/leases/renew/{id}` — 续租  
- `GET /api/workspace/list` — 工作区目录列表  
- `GET /api/workspace/file` — 读取文件  
- `PUT /api/workspace/file` — 写入文件  
- `POST /api/workspace/dir` — 创建目录  
- `DELETE /api/workspace` — 删除文件/空目录  
- `GET /api/admin/*` — 管理员接口（用户审批、列表等）  

---

## 部署注意

1. **生产环境**：修改 `JWT_SECRET`，使用强随机值。  
2. **数据持久化**：将 `lab-gpu-data` 卷或 `DATA_DIR` 对应目录放到可靠存储。  
3. **用户目录**：保证 `USER_DATA_BASE` 在宿主机存在且对运行 Docker 的用户可写。  
4. **HTTPS**：如需 HTTPS，在 Compose 前增加 Nginx/Traefik 等反向代理并配置 TLS。  
5. **Code Server 访问**：前端「打开 Code」使用当前页面的 `hostname` + 映射端口；若管理端与用户访问域名不同，需在前端或网关侧做端口/域名配置。

---

## 贡献

欢迎提交 Issue 与 Pull Request。请确保代码风格与现有项目一致，并补充/更新必要说明。

---

## 许可证

本项目采用 [MIT License](LICENSE)。  
你可以在遵守许可证条款的前提下自由使用、修改与再分发。
