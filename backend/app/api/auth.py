"""认证 API"""
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordRequestForm

from app.database import get_db
from app.database_models import UserModel
from app.models import UserResponse, Token, UserRegister, UserProfileUpdate, UserPasswordChange
from app.auth import verify_password, create_access_token, get_current_user, get_password_hash
from app.config import DEFAULT_DISK_QUOTA_BYTES, INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME, INIT_ADMIN_TOKEN

router = APIRouter()
bootstrap_security = HTTPBearer(auto_error=False)

# 用户名：名字全拼，小写字母，可含连字符，2-30 位
USERNAME_PINYIN_RE = re.compile(r"^[a-z][a-z0-9\-]{1,29}$")


def _do_login(username: str, password: str, db):
    user = db.query(UserModel).filter(UserModel.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.approved and user.role != "admin":
        raise HTTPException(status_code=403, detail="账号尚未通过管理员审批，请联系管理员")
    token = create_access_token(data={"sub": user.username})
    return Token(
        access_token=token,
        user=UserResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name or "",
            role=user.role,
            real_name=user.real_name or None,
            contact_type=user.contact_type or None,
            contact_value=user.contact_value or None,
            approved=bool(user.approved),
            created_at=user.created_at,
        ),
    )


@router.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
    return _do_login(form.username, form.password, db)


@router.post("/login/json", response_model=Token)
def login_json(body: dict = Body(...), db=Depends(get_db)):
    username = body.get("username")
    password = body.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="缺少 username 或 password")
    return _do_login(username, password, db)


@router.post("/init-admin")
def init_admin(
    credentials: HTTPAuthorizationCredentials = Depends(bootstrap_security),
    db=Depends(get_db),
):
    if not INIT_ADMIN_TOKEN or not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=404, detail="Not Found")
    if not secrets.compare_digest(credentials.credentials, INIT_ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="无效的初始化凭据")
    if not INITIAL_ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="未配置初始管理员密码")
    if db.query(UserModel).count() != 0:
        raise HTTPException(status_code=409, detail="系统已初始化")
    admin = UserModel(
        username=INITIAL_ADMIN_USERNAME,
        hashed_password=get_password_hash(INITIAL_ADMIN_PASSWORD),
        role="admin",
        display_name="管理员",
        approved=1,
        disk_quota_bytes=DEFAULT_DISK_QUOTA_BYTES,
    )
    db.add(admin)
    db.commit()
    return {"message": "已创建初始管理员"}


@router.post("/register")
def register(req: UserRegister, db=Depends(get_db)):
    """用户自助注册，需管理员审批后才能登录使用"""
    username = req.username.strip().lower()
    if not USERNAME_PINYIN_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="用户名请使用名字全拼（小写字母，如 zhangsan、ouyang-xiao）",
        )
    if req.contact_type not in ("phone", "wechat"):
        raise HTTPException(status_code=400, detail="联系方式请选择 手机号(phone) 或 微信号(wechat)")
    if not req.real_name or not req.contact_value.strip():
        raise HTTPException(status_code=400, detail="请填写实名和联系方式")
    if db.query(UserModel).filter(UserModel.username == username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")
    user = UserModel(
        username=username,
        hashed_password=get_password_hash(req.password),
        display_name=req.real_name,
        real_name=req.real_name,
        contact_type=req.contact_type,
        contact_value=req.contact_value.strip(),
        approved=0,
        role="user",
        disk_quota_bytes=DEFAULT_DISK_QUOTA_BYTES,
    )
    db.add(user)
    db.commit()
    return {"message": "注册成功，请等待管理员审批通过后再登录"}


@router.get("/me", response_model=UserResponse)
def me(user=Depends(get_current_user)):
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name or "",
        role=user.role,
        real_name=getattr(user, "real_name", None) or None,
        contact_type=getattr(user, "contact_type", None) or None,
        contact_value=getattr(user, "contact_value", None) or None,
        approved=bool(getattr(user, "approved", 1)),
        created_at=user.created_at,
    )


@router.patch("/me", response_model=UserResponse)
def update_me(req: UserProfileUpdate, user=Depends(get_current_user), db=Depends(get_db)):
    """用户修改显示名、实名、联系方式"""
    if req.display_name is not None:
        user.display_name = (req.display_name or "").strip() or user.display_name
    if req.real_name is not None:
        setattr(user, "real_name", (req.real_name or "").strip())
    if req.contact_type is not None:
        if req.contact_type and req.contact_type not in ("phone", "wechat"):
            raise HTTPException(status_code=400, detail="联系方式类型请选择 phone 或 wechat")
        setattr(user, "contact_type", req.contact_type or "")
    if req.contact_value is not None:
        setattr(user, "contact_value", (req.contact_value or "").strip())
    db.commit()
    db.refresh(user)
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name or "",
        role=user.role,
        real_name=getattr(user, "real_name", None) or None,
        contact_type=getattr(user, "contact_type", None) or None,
        contact_value=getattr(user, "contact_value", None) or None,
        approved=bool(getattr(user, "approved", 1)),
        created_at=user.created_at,
    )


@router.post("/password")
def change_password(req: UserPasswordChange, user=Depends(get_current_user), db=Depends(get_db)):
    """修改密码"""
    if not verify_password(req.old_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="原密码不正确")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度至少为 6 位")
    user.hashed_password = get_password_hash(req.new_password)
    db.commit()
    return {"message": "密码修改成功"}
