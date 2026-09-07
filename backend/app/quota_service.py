import os
import stat
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Optional

from app.config import DEFAULT_DISK_QUOTA_BYTES, DISK_QUOTA_GRACE_HOURS, USER_DATA_BASE


DISK_QUOTA_GRACE_PERIOD = timedelta(hours=DISK_QUOTA_GRACE_HOURS)
_QUOTA_LOCKS: defaultdict[str, RLock] = defaultdict(RLock)


@dataclass(frozen=True)
class WorkspaceUsage:
    usage_bytes: int
    complete: bool


@dataclass(frozen=True)
class QuotaStatus:
    usage_bytes: int
    quota_bytes: int
    over_quota: bool
    blocked: bool
    exceeded_since: Optional[datetime]
    exempt: bool
    scan_complete: bool = True

    @property
    def allowed(self) -> bool:
        return self.exempt or (self.scan_complete and not self.blocked)


def _scan_directory(root: Path) -> WorkspaceUsage:
    total = 0
    complete = True
    pending = [root]
    seen_files: set[tuple[int, int]] = set()

    while pending:
        directory = pending.pop()
        try:
            if directory.is_symlink():
                complete = False
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        entry_stat = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(entry_stat.st_mode):
                            pending.append(Path(entry.path))
                            continue
                        if not stat.S_ISREG(entry_stat.st_mode):
                            continue
                        file_key = (int(entry_stat.st_dev), int(entry_stat.st_ino))
                        if file_key in seen_files:
                            continue
                        seen_files.add(file_key)
                        total += max(0, int(entry_stat.st_size))
                    except (FileNotFoundError, PermissionError, OSError):
                        complete = False
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            complete = False

    return WorkspaceUsage(usage_bytes=total, complete=complete)


def calculate_workspace_usage_result(root: str | Path) -> WorkspaceUsage:
    path = Path(root)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return WorkspaceUsage(usage_bytes=0, complete=True)
    except (PermissionError, OSError):
        return WorkspaceUsage(usage_bytes=0, complete=False)
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        return WorkspaceUsage(usage_bytes=0, complete=False)
    return _scan_directory(path)


def calculate_workspace_usage(root: str | Path) -> int:
    return calculate_workspace_usage_result(root).usage_bytes


scan_workspace_usage = calculate_workspace_usage


def _workspace_path_for_user(username: str) -> Optional[Path]:
    name = str(username or "")
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        return None
    return Path(USER_DATA_BASE) / name


def workspace_usage_for_user_result(username: str) -> WorkspaceUsage:
    base = Path(USER_DATA_BASE)
    try:
        base_stat = base.stat()
        if not stat.S_ISDIR(base_stat.st_mode):
            return WorkspaceUsage(usage_bytes=0, complete=False)
    except (FileNotFoundError, PermissionError, OSError):
        return WorkspaceUsage(usage_bytes=0, complete=False)

    root = _workspace_path_for_user(username)
    if root is None:
        return WorkspaceUsage(usage_bytes=0, complete=False)
    return calculate_workspace_usage_result(root)


def workspace_usage_for_user(username: str) -> int:
    return workspace_usage_for_user_result(username).usage_bytes


def _quota_value(user) -> int:
    for name in ("disk_quota_bytes", "quota_bytes"):
        value = getattr(user, name, None)
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                continue
    return max(1, DEFAULT_DISK_QUOTA_BYTES)


def _usage_value(user) -> int:
    for name in ("disk_usage_bytes", "usage_bytes"):
        value = getattr(user, name, None)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
    return 0


def _exceeded_since_value(user) -> Optional[datetime]:
    for name in (
        "disk_quota_exceeded_since",
        "disk_quota_over_since",
        "quota_exceeded_since",
        "over_quota_since",
    ):
        value = getattr(user, name, None)
        if value is not None:
            return value
    return None


def _set_quota_state(
    user,
    usage_bytes: int,
    blocked: bool,
    exceeded_since: Optional[datetime],
    checked_at: Optional[datetime],
    scan_complete: bool,
) -> None:
    setattr(user, "disk_usage_bytes", usage_bytes)
    setattr(user, "disk_quota_blocked", blocked)
    setattr(user, "disk_quota_exceeded_since", exceeded_since)
    setattr(user, "disk_usage_scan_complete", scan_complete)
    if checked_at is not None:
        setattr(user, "disk_usage_checked_at", checked_at)
    setattr(user, "usage_bytes", usage_bytes)
    setattr(user, "quota_blocked", blocked)
    setattr(user, "quota_exceeded_since", exceeded_since)
    setattr(user, "disk_quota_over_since", exceeded_since)
    setattr(user, "over_quota_since", exceeded_since)


def _refresh_user_quota(
    db,
    user,
    now: Optional[datetime] = None,
    usage_bytes: Optional[int] = None,
    commit: bool = True,
) -> QuotaStatus:
    now = now or datetime.now()
    exempt = getattr(user, "role", None) == "admin"
    quota_bytes = _quota_value(user)
    if getattr(user, "disk_quota_bytes", None) is None:
        setattr(user, "disk_quota_bytes", quota_bytes)

    if exempt:
        measured_usage = _usage_value(user) if usage_bytes is None else max(0, int(usage_bytes))
        _set_quota_state(user, measured_usage, False, None, now if usage_bytes is not None else None, True)
        status = QuotaStatus(
            usage_bytes=measured_usage,
            quota_bytes=quota_bytes,
            over_quota=False,
            blocked=False,
            exceeded_since=None,
            exempt=True,
            scan_complete=True,
        )
    else:
        measurement = (
            WorkspaceUsage(max(0, int(usage_bytes)), True)
            if usage_bytes is not None
            else workspace_usage_for_user_result(user.username)
        )
        if not measurement.complete:
            stored_usage = _usage_value(user)
            exceeded_since = _exceeded_since_value(user)
            blocked = True
            _set_quota_state(user, stored_usage, blocked, exceeded_since, None, False)
            status = QuotaStatus(
                usage_bytes=stored_usage,
                quota_bytes=quota_bytes,
                over_quota=False,
                blocked=blocked,
                exceeded_since=exceeded_since,
                exempt=False,
                scan_complete=False,
            )
        else:
            measured_usage = measurement.usage_bytes
            over_quota = measured_usage >= quota_bytes
            exceeded_since = _exceeded_since_value(user) if over_quota else None
            if over_quota and exceeded_since is None:
                exceeded_since = now
            blocked = over_quota
            _set_quota_state(user, measured_usage, blocked, exceeded_since, now, True)
            status = QuotaStatus(
                usage_bytes=measured_usage,
                quota_bytes=quota_bytes,
                over_quota=over_quota,
                blocked=blocked,
                exceeded_since=exceeded_since,
                exempt=False,
                scan_complete=True,
            )

    if commit and hasattr(db, "commit"):
        db.commit()
    return status


def refresh_user_quota(
    db,
    user,
    now: Optional[datetime] = None,
    usage_bytes: Optional[int] = None,
    commit: bool = True,
) -> QuotaStatus:
    username = str(getattr(user, "username", ""))
    with _QUOTA_LOCKS[username]:
        return _refresh_user_quota(db, user, now=now, usage_bytes=usage_bytes, commit=commit)


def quota_status(user) -> QuotaStatus:
    exempt = getattr(user, "role", None) == "admin"
    usage_bytes = _usage_value(user)
    quota_bytes = _quota_value(user)
    stored_blocked = getattr(user, "disk_quota_blocked", None)
    if stored_blocked is None:
        stored_blocked = getattr(user, "quota_blocked", False)
    scan_complete = True if exempt else bool(getattr(user, "disk_usage_scan_complete", False))
    over_quota = False if exempt or not scan_complete else usage_bytes >= quota_bytes
    blocked = False if exempt else over_quota or bool(stored_blocked) or not scan_complete
    exceeded_since = None if exempt else _exceeded_since_value(user)
    return QuotaStatus(
        usage_bytes=usage_bytes,
        quota_bytes=quota_bytes,
        over_quota=over_quota,
        blocked=blocked,
        exceeded_since=exceeded_since,
        exempt=exempt,
        scan_complete=scan_complete,
    )


def quota_status_payload(user, status: Optional[QuotaStatus] = None) -> dict:
    status = status or quota_status(user)
    grace_deadline = None
    if status.exceeded_since is not None:
        grace_deadline = status.exceeded_since + DISK_QUOTA_GRACE_PERIOD
    checked_at = getattr(user, "disk_usage_checked_at", None)
    return {
        "user_id": getattr(user, "id", None),
        "username": getattr(user, "username", ""),
        "role": getattr(user, "role", "user"),
        "quota_bytes": status.quota_bytes,
        "usage_bytes": status.usage_bytes,
        "disk_quota_bytes": status.quota_bytes,
        "disk_usage_bytes": status.usage_bytes,
        "over_quota": status.over_quota,
        "blocked": status.blocked,
        "quota_blocked": status.blocked,
        "disk_quota_blocked": status.blocked,
        "over_quota_since": status.exceeded_since,
        "quota_exceeded_since": status.exceeded_since,
        "disk_quota_exceeded_since": status.exceeded_since,
        "disk_quota_over_since": status.exceeded_since,
        "grace_hours": DISK_QUOTA_GRACE_HOURS,
        "grace_deadline": grace_deadline,
        "usage_checked_at": checked_at,
        "scan_complete": status.scan_complete,
        "quota_exempt": status.exempt,
    }


def check_user_can_provision(db, user, commit: bool = True) -> QuotaStatus:
    return refresh_user_quota(db, user, commit=commit)


ensure_provision_allowed = check_user_can_provision
