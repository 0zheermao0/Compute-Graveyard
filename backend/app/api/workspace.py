"""用户工作区文件 API：列出、读取、编辑、删除、创建（仅限当前用户目录）"""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.auth import get_current_user
from app.config import USER_DATA_BASE
from app.docker_service import ensure_user_dir

router = APIRouter()

# 允许的文本扩展名（可编辑）
TEXT_EXTENSIONS = {".txt", ".py", ".json", ".yml", ".yaml", ".md", ".sh", ".env", ".cfg", ".ini", ".log", ".csv", ".xml", ".html", ".css", ".js", ".ts", ".jsx", ".tsx", ".vue"}
# 默认当无扩展名时按文本处理
DEFAULT_AS_TEXT = True


def _user_root(user) -> Path:
    return Path(USER_DATA_BASE) / user.username


def _resolve_path(user, subpath: str) -> Path:
    """解析子路径，确保在用户目录内，禁止 .."""
    root = _user_root(user)
    subpath = (subpath or "").strip().lstrip("/")
    if ".." in subpath or subpath.startswith(".."):
        raise HTTPException(status_code=400, detail="路径非法")
    full = (root / subpath).resolve()
    if not str(full).startswith(str(root.resolve())):
        raise HTTPException(status_code=400, detail="路径超出工作区")
    return full


def _is_text_path(path: Path) -> bool:
    if path.is_dir():
        return False
    suf = path.suffix.lower()
    if suf in TEXT_EXTENSIONS:
        return True
    if DEFAULT_AS_TEXT and not suf:
        return True
    return False


@router.get("/list")
def list_dir(
    path: str = Query("", description="相对路径，空为根目录"),
    user=Depends(get_current_user),
):
    """列出工作区目录内容"""
    ensure_user_dir(user.username)
    root = _user_root(user)
    target = _resolve_path(user, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")
    items = []
    for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        rel = p.relative_to(root)
        items.append({
            "name": p.name,
            "path": str(rel).replace("\\", "/"),
            "is_dir": p.is_dir(),
            "size": p.stat().st_size if p.is_file() else None,
        })
    return {"path": path or "", "items": items}


class WriteBody(BaseModel):
    content: str


@router.get("/file")
def read_file(
    path: str = Query(..., description="相对路径"),
    user=Depends(get_current_user),
):
    """读取文本文件内容"""
    target = _resolve_path(user, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="不能读取目录")
    if not _is_text_path(target):
        raise HTTPException(status_code=400, detail="仅支持文本文件编辑")
    try:
        raw = target.read_bytes()
        # 简单检测：若含 null 或过多非文本字节则拒绝
        if b"\x00" in raw or sum(1 for b in raw if b < 0x20 and b not in (0x09, 0x0a, 0x0d)) > len(raw) // 4:
            raise HTTPException(status_code=400, detail="疑似二进制文件，不支持在线编辑")
        return PlainTextResponse(raw.decode("utf-8", errors="replace"))
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件编码不支持")


@router.put("/file")
def write_file(
    path: str = Query(..., description="相对路径"),
    body: WriteBody = ...,
    user=Depends(get_current_user),
):
    """创建或覆盖文本文件"""
    target = _resolve_path(user, path)
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=400, detail="路径是目录")
    ensure_user_dir(user.username)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = (body.content or "").encode("utf-8")
    target.write_bytes(content)
    return {"path": path, "ok": True}


class CreateDirBody(BaseModel):
    path: str  # 相对路径，如 "foo" 或 "foo/bar"


@router.post("/dir")
def create_dir(
    body: CreateDirBody,
    user=Depends(get_current_user),
):
    """创建目录"""
    target = _resolve_path(user, body.path.strip())
    root = _user_root(user)
    if target.exists():
        raise HTTPException(status_code=400, detail="已存在")
    target.mkdir(parents=True, exist_ok=True)
    return {"path": body.path, "ok": True}


@router.delete("")
def delete_path(
    path: str = Query(..., description="相对路径"),
    user=Depends(get_current_user),
):
    """删除文件或空目录"""
    target = _resolve_path(user, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="不存在")
    if target == _user_root(user):
        raise HTTPException(status_code=400, detail="不能删除根目录")
    if target.is_dir():
        if any(target.iterdir()):
            raise HTTPException(status_code=400, detail="目录非空，请先删除内容")
        target.rmdir()
    else:
        target.unlink()
    return {"path": path, "ok": True}
