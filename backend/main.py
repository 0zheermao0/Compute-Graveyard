"""
Lab-GPU-Manager 主入口
前后端一体，Docker 部署
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import auth, dashboard, containers, admin, leases, workspace
from app.database import init_db, create_default_admin

app = FastAPI(title="Lab-GPU-Manager", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API 路由
app.include_router(auth.router, prefix="/api/auth", tags=["认证"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["看板"])
app.include_router(containers.router, prefix="/api/containers", tags=["容器"])
app.include_router(leases.router, prefix="/api/leases", tags=["租期"])
app.include_router(admin.router, prefix="/api/admin", tags=["管理员"])
app.include_router(workspace.router, prefix="/api/workspace", tags=["工作区"])


@app.get("/api/health")
async def health():
    return {"status": "ok"}

# 静态文件（前端 SPA）必须放在最后，否则会拦截 /api 请求
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(static_dir / "assets")), name="assets")

    # 关键：处理 SPA 刷新 404 问题
    from fastapi.responses import FileResponse

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # 如果请求的不是 API，且文件不存在，则返回 index.html
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
