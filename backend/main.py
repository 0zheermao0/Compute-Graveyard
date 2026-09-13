"""
Lab-GPU-Manager 主入口
前后端一体，Docker 部署
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import agent, auth, dashboard, containers, admin, leases, workspace
from app.config import CORS_ORIGINS, NODE_ROLE
from app.database import create_default_admin, init_db

app = FastAPI(title="Lab-GPU-Manager", version="1.0.0")

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth.router, prefix="/api/auth", tags=["认证"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["看板"])
app.include_router(containers.router, prefix="/api/containers", tags=["容器"])
app.include_router(leases.router, prefix="/api/leases", tags=["租期"])
app.include_router(admin.router, prefix="/api/admin", tags=["管理员"])
app.include_router(workspace.router, prefix="/api/workspace", tags=["工作区"])
app.include_router(agent.router, prefix="/api/agent/v1", tags=["节点 Agent"])


@app.get("/api/health")
async def health():
    return {"status": "ok", "role": NODE_ROLE}


static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(static_dir / "assets")), name="assets")

    from fastapi.responses import FileResponse

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = static_dir / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(static_dir / "index.html")


@app.on_event("startup")
async def startup():
    init_db()
    create_default_admin()
    from app.scheduler import start_scheduler
    start_scheduler()


@app.on_event("shutdown")
async def shutdown():
    from app.scheduler import stop_scheduler
    stop_scheduler()
