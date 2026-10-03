import { useId, useRef, useState } from "react";
import type React from "react";
import {
  ChevronDown,
  HardDrive,
  Plus,
  Search,
  Trash2,
  UserCheck,
  Users,
} from "lucide-react";
import type { User, PendingUser } from "../pages/Admin";
import { fetcher } from "../api/client";
import "./AdminUsersPanel.css";

type NewUser = { username: string; password: string; display_name: string };
type Drafts = Record<number, string>;

export interface AdminUsersPanelProps {
  users: User[];
  pendingUsers: PendingUser[];
  newUser: NewUser;
  setNewUser: React.Dispatch<React.SetStateAction<NewUser>>;
  loading: boolean;
  createError: string;
  quotaDrafts: Drafts;
  setQuotaDrafts: React.Dispatch<React.SetStateAction<Drafts>>;
  gpuQuotaDrafts: Drafts;
  setGpuQuotaDrafts: React.Dispatch<React.SetStateAction<Drafts>>;
  quotaSaving: number | null;
  gpuQuotaSaving: number | null;
  reputationDrafts: Drafts;
  setReputationDrafts: React.Dispatch<React.SetStateAction<Drafts>>;
  reputationSaving: number | null;
  onSaveReputation: (id: number, reset?: boolean) => Promise<void>;
  quotaRefreshing: number | null;
  onCreateUser: (e: React.FormEvent) => Promise<void>;
  onApprove: (id: number) => Promise<void>;
  onDeleteUser: (id: number, username: string) => void;
  onSaveQuota: (id: number) => Promise<void>;
  onSaveGpuQuota: (id: number) => Promise<void>;
  onRefreshQuota: (id: number) => Promise<void>;
  usersLoading: boolean;
  usersError: string;
  pendingLoading: boolean;
  pendingError: string;
  onRetryUsers: () => void;
  onRetryPending: () => void;
}

const GIB = 1024 ** 3;
const filters = [
  { value: "all", label: "全部" },
  { value: "pending", label: "待审批" },
  { value: "quota", label: "配额异常" },
  { value: "admin", label: "管理员" },
] as const;
type Filter = (typeof filters)[number]["value"];

function formatBytes(bytes: number) {
  if (bytes >= GIB) return `${(bytes / GIB).toFixed(1)} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}

function formatDate(value?: string | null) {
  if (!value) return "暂无记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

function eventValue(value: unknown) {
  return typeof value === "string" || typeof value === "number" ? String(value) : "—";
}

const eventLabels: Record<string, string> = {
  idle_warning: "闲置预警",
  idle_reclaim: "闲置回收",
  idle_shrink: "闲置缩卡",
  expiry_reward: "正常到期奖励",
  admin_set: "管理员设置",
  admin_adjustment: "管理员调整",
  admin_reset: "管理员清零",
};

function contact(type?: string, value?: string) {
  if (!value) return "未填写";
  const label =
    type === "wechat"
      ? "微信"
      : type === "phone" || type === "mobile"
        ? "手机"
        : type === "email"
          ? "邮箱"
          : type || "联系方式";
  return `${label} · ${value}`;
}

function abnormalQuota(user: User) {
  return (
    !user.quota_exempt &&
    (!user.scan_complete ||
      user.disk_quota_blocked ||
      user.disk_usage_bytes > user.disk_quota_bytes)
  );
}

function quotaStatus(user: User) {
  if (user.quota_exempt) return "管理员豁免";
  if (!user.scan_complete) return "检测不完整，已暂停申请";
  if (user.disk_quota_blocked) return "已禁止申请";
  if (user.disk_usage_bytes > user.disk_quota_bytes) return "磁盘已超限";
  return "配额正常";
}

function LoadState({
  loading,
  error,
  retry,
  empty,
}: {
  loading: boolean;
  error: string;
  retry: () => void;
  empty: string;
}) {
  return (
    <div className="admin-users-empty" role={error ? "alert" : "status"}>
      <Users size={24} aria-hidden="true" />
      <strong>{loading ? "正在加载…" : error ? "加载失败" : empty}</strong>
      {error && (
        <>
          <p>{error}</p>
          <button
            type="button"
            className="admin-users-button"
            onClick={retry}
            disabled={loading}
          >
            重试
          </button>
        </>
      )}
    </div>
  );
}

export default function AdminUsersPanel(props: AdminUsersPanelProps) {
  const {
    users,
    pendingUsers,
    newUser,
    setNewUser,
    loading,
    createError,
    quotaDrafts,
    setQuotaDrafts,
    gpuQuotaDrafts,
    setGpuQuotaDrafts,
    quotaSaving,
    gpuQuotaSaving,
    reputationDrafts,
    setReputationDrafts,
    reputationSaving,
    onSaveReputation,
    quotaRefreshing,
    onCreateUser,
    onApprove,
    onDeleteUser,
    onSaveQuota,
    onSaveGpuQuota,
    onRefreshQuota,
    usersLoading,
    usersError,
    pendingLoading,
    pendingError,
    onRetryUsers,
    onRetryPending,
  } = props;
  const id = useId();
  const [view, setView] = useState<"users" | "pending">("users");
  const [createOpen, setCreateOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [approvalBusy, setApprovalBusy] = useState<number | null>(null);
  const approvalLock = useRef(false);
  const quotaLock = useRef(false);
  const [localQuotaBusy, setLocalQuotaBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [eventBusy, setEventBusy] = useState<number | null>(null);
  const [eventOpen, setEventOpen] = useState<Record<number, boolean>>({});
  const [events, setEvents] = useState<Record<number, Record<string, unknown>[]>>({});
  const [eventErrors, setEventErrors] = useState<Record<number, string>>({});
  const quotaBusy =
    loading ||
    approvalBusy !== null ||
    localQuotaBusy ||
    quotaSaving !== null ||
    gpuQuotaSaving !== null ||
    reputationSaving !== null ||
    eventBusy !== null ||
    quotaRefreshing !== null;
  const search = query.trim().toLocaleLowerCase();
  const visibleUsers = users.filter((user) => {
    const matches = [
      user.username,
      user.display_name,
      user.real_name,
      user.contact_type,
      user.contact_value,
      String(user.id),
    ].some((value) => value?.toLocaleLowerCase().includes(search));
    return (
      matches &&
      (filter === "all" ||
        (filter === "pending" && !user.approved) ||
        (filter === "quota" && abnormalQuota(user)) ||
        (filter === "admin" && user.role === "admin"))
    );
  });

  async function approve(userId: number) {
    if (approvalLock.current || quotaBusy || quotaLock.current) return;
    approvalLock.current = true;
    setApprovalBusy(userId);
    setActionError("");
    try {
      await onApprove(userId);
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : "审批失败，请重试",
      );
    } finally {
      approvalLock.current = false;
      setApprovalBusy(null);
    }
  }

  async function loadReputationEvents(userId: number) {
    setEventOpen((current) => ({ ...current, [userId]: true }));
    setEventBusy(userId);
    setEventErrors((current) => ({ ...current, [userId]: "" }));
    try {
      const response = await fetcher<unknown>(`/admin/users/${userId}/reputation-events`);
      const envelope = response && typeof response === "object" ? response as Record<string, unknown> : {};
      const list = Array.isArray(response) ? response : envelope.events ?? envelope.items;
      if (!Array.isArray(list)) throw new Error("记录响应格式不正确，请重试");
      const rows = list.filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item));
      setEvents((current) => ({ ...current, [userId]: rows }));
    } catch (error) {
      setEventErrors((current) => ({ ...current, [userId]: error instanceof Error ? error.message : "记录加载失败" }));
    } finally {
      setEventBusy(null);
    }
  }

  async function saveReputation(userId: number, reset = false) {
    await onSaveReputation(userId, reset);
    if (eventOpen[userId]) await loadReputationEvents(userId);
  }

  async function runQuota(
    action: (userId: number) => Promise<void>,
    userId: number,
  ) {
    if (quotaBusy || quotaLock.current) return;
    quotaLock.current = true;
    setLocalQuotaBusy(true);
    setActionError("");
    try {
      await action(userId);
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : "用户资源操作失败，请重试",
      );
    } finally {
      quotaLock.current = false;
      setLocalQuotaBusy(false);
    }
  }

  function approvalButton(userId: number) {
    return (
      <button
        type="button"
        className="admin-users-button admin-users-button-primary"
        disabled={quotaBusy}
        onClick={() => void approve(userId)}
      >
        <UserCheck size={15} aria-hidden="true" />
        {approvalBusy === userId ? "审批中…" : "通过审批"}
      </button>
    );
  }

  return (
    <section className="admin-users-panel" aria-label="用户与审批管理">
      <nav className="admin-users-nav" aria-label="用户管理子导航">
        <button
          type="button"
          aria-pressed={view === "users"}
          aria-controls={`${id}-users`}
          onClick={() => setView("users")}
        >
          <Users size={17} aria-hidden="true" />
          用户<span>{users.length}</span>
        </button>
        <button
          type="button"
          aria-pressed={view === "pending"}
          aria-controls={`${id}-pending`}
          onClick={() => setView("pending")}
        >
          <UserCheck size={17} aria-hidden="true" />
          审批<span>{pendingUsers.length}</span>
        </button>
      </nav>
      {actionError && (
        <p className="admin-users-error" role="alert">
          {actionError}
        </p>
      )}
      <div id={`${id}-users`} hidden={view !== "users"}>
        <details
          className="admin-users-create"
          open={createOpen}
          onToggle={(event) => setCreateOpen(event.currentTarget.open)}
        >
          <summary>
            <Plus size={18} aria-hidden="true" />
            <span>
              创建用户<small>直接创建可用账号</small>
            </span>
            <ChevronDown
              className="admin-user-chevron"
              size={18}
              aria-hidden="true"
            />
          </summary>
          <form className="admin-users-create-form" onSubmit={onCreateUser}>
            <label className="admin-users-field" htmlFor={`${id}-username`}>
              用户名
              <input
                id={`${id}-username`}
                autoComplete="off"
                required
                value={newUser.username}
                disabled={quotaBusy}
                onChange={(e) =>
                  setNewUser((current) => ({
                    ...current,
                    username: e.target.value,
                  }))
                }
              />
            </label>
            <label className="admin-users-field" htmlFor={`${id}-password`}>
              密码
              <input
                id={`${id}-password`}
                type="password"
                autoComplete="new-password"
                required
                value={newUser.password}
                disabled={quotaBusy}
                onChange={(e) =>
                  setNewUser((current) => ({
                    ...current,
                    password: e.target.value,
                  }))
                }
              />
            </label>
            <label className="admin-users-field" htmlFor={`${id}-display-name`}>
              显示名称（可选）
              <input
                id={`${id}-display-name`}
                value={newUser.display_name}
                disabled={quotaBusy}
                onChange={(e) =>
                  setNewUser((current) => ({
                    ...current,
                    display_name: e.target.value,
                  }))
                }
              />
            </label>
            <button
              type="submit"
              className="admin-users-button admin-users-button-primary"
              disabled={quotaBusy}
            >
              {loading ? "创建中…" : "创建用户"}
            </button>
            {createError && (
              <p className="admin-users-error" role="alert">
                {createError}
              </p>
            )}
          </form>
        </details>
        <div className="admin-users-toolbar">
          <label className="admin-users-search" htmlFor={`${id}-search`}>
            <Search size={17} aria-hidden="true" />
            <span className="admin-users-sr-only">
              搜索用户姓名、用户名、联系方式或 ID
            </span>
            <input
              id={`${id}-search`}
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索姓名、用户名、联系方式或 ID"
            />
          </label>
          <div
            className="admin-users-filters"
            role="group"
            aria-label="筛选用户"
          >
            {filters.map((item) => (
              <button
                key={item.value}
                type="button"
                aria-pressed={filter === item.value}
                onClick={() => setFilter(item.value)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
        {!usersLoading && !usersError && (
          <p className="admin-users-result" aria-live="polite">
            显示 {visibleUsers.length} / {users.length} 位用户 ·
            展开卡片管理配额
          </p>
        )}
        {usersLoading && users.length > 0 && (
          <p className="admin-users-result" role="status">
            正在同步用户数据…
          </p>
        )}
        {(usersLoading && users.length === 0) ||
        usersError ||
        !visibleUsers.length ? (
          <LoadState
            loading={usersLoading}
            error={usersError}
            retry={onRetryUsers}
            empty={
              users.length
                ? "没有匹配的用户，请调整搜索或筛选"
                : "暂无用户，可在上方创建账号"
            }
          />
        ) : (
          <div className="admin-users-grid">
            {visibleUsers.map((user) => {
              const percentage =
                user.disk_quota_bytes > 0
                  ? Math.max(
                      0,
                      Math.min(
                        100,
                        (user.disk_usage_bytes / user.disk_quota_bytes) * 100,
                      ),
                    )
                  : 0;
              const diskId = `${id}-disk-${user.id}`;
              const gpuId = `${id}-gpu-${user.id}`;
              return (
                <details className="admin-user-card" key={user.id}>
                  <summary className="admin-user-summary">
                    <div className="admin-user-summary-top">
                      <span className="admin-user-avatar" aria-hidden="true">
                        {(user.display_name || user.real_name || user.username)
                          .slice(0, 1)
                          .toUpperCase()}
                      </span>
                      <div className="admin-user-identity">
                        <strong>
                          {user.display_name || user.real_name || user.username}
                        </strong>
                        <span>
                          @{user.username} · ID {user.id}
                        </span>
                      </div>
                      <ChevronDown
                        className="admin-user-chevron"
                        size={18}
                        aria-hidden="true"
                      />
                    </div>
                    <div className="admin-user-badges">
                      <span className="admin-user-badge">
                        {user.role === "admin"
                          ? "管理员"
                          : user.role === "user"
                            ? "用户"
                            : user.role}
                      </span>
                      <span
                        className={`admin-user-badge ${user.approved ? "admin-user-badge-good" : "admin-user-badge-warning"}`}
                      >
                        {user.approved ? "已通过审批" : "待审批"}
                      </span>
                      <span
                        className={`admin-user-badge ${abnormalQuota(user) ? "admin-user-badge-warning" : "admin-user-badge-good"}`}
                      >
                        {quotaStatus(user)}
                      </span>
                    </div>
                    <div className="admin-user-resources">
                      <span>
                        <HardDrive size={14} aria-hidden="true" />
                        磁盘 <b>{formatBytes(user.disk_usage_bytes)}</b>
                        <small>
                          /{" "}
                          {user.quota_exempt
                            ? "不受限"
                            : formatBytes(user.disk_quota_bytes)}
                        </small>
                      </span>
                      <span>
                        GPU 上限 <b>{user.max_gpus_per_user} 块</b>
                      </span>
                      <span>信誉分 <b>{user.reputation_score ?? 0}</b></span>
                    </div>
                  </summary>
                  <div className="admin-user-body">
                    <dl className="admin-user-meta">
                      <div>
                        <dt>真实姓名</dt>
                        <dd>{user.real_name || "未填写"}</dd>
                      </div>
                      <div>
                        <dt>联系方式</dt>
                        <dd>
                          {contact(user.contact_type, user.contact_value)}
                        </dd>
                      </div>
                      <div>
                        <dt>注册时间</dt>
                        <dd>{formatDate(user.created_at)}</dd>
                      </div>
                    </dl>
                    <section
                      className="admin-user-quota"
                      aria-labelledby={`${diskId}-heading`}
                    >
                      <h3 id={`${diskId}-heading`}>磁盘使用与配额</h3>
                      <p className="admin-user-usage">
                        {formatBytes(user.disk_usage_bytes)} /{" "}
                        {user.quota_exempt
                          ? "不受限"
                          : formatBytes(user.disk_quota_bytes)}
                      </p>
                      {!user.quota_exempt && (
                        <progress
                          className={`admin-user-progress ${abnormalQuota(user) ? "admin-user-progress-warning" : ""}`}
                          value={percentage}
                          max={100}
                          aria-label={`${user.username} 磁盘配额使用比例`}
                        >
                          {percentage.toFixed(1)}%
                        </progress>
                      )}
                      {user.quota_exempt ? (
                        <p className="admin-user-note">
                          管理员磁盘配额豁免，不可编辑；GPU 配额仍可独立设置。
                        </p>
                      ) : (
                        <label className="admin-users-field" htmlFor={diskId}>
                          磁盘配额（GiB）
                          <input
                            id={diskId}
                            type="number"
                            min="0.1"
                            step="0.1"
                            value={
                              quotaDrafts[user.id] ??
                              String(user.disk_quota_bytes / GIB)
                            }
                            disabled={quotaBusy}
                            onChange={(e) =>
                              setQuotaDrafts((current) => ({
                                ...current,
                                [user.id]: e.target.value,
                              }))
                            }
                          />
                        </label>
                      )}
                      <div className="admin-user-actions">
                        {!user.quota_exempt && (
                          <button
                            type="button"
                            className="admin-users-button"
                            disabled={quotaBusy}
                            onClick={() => void runQuota(onSaveQuota, user.id)}
                          >
                            {quotaSaving === user.id
                              ? "保存中…"
                              : "保存磁盘配额"}
                          </button>
                        )}
                        <button
                          type="button"
                          className="admin-users-button"
                          disabled={quotaBusy}
                          onClick={() => void runQuota(onRefreshQuota, user.id)}
                        >
                          {quotaRefreshing === user.id
                            ? "检测中…"
                            : "刷新磁盘用量"}
                        </button>
                      </div>
                      <dl className="admin-user-meta admin-user-scan">
                        <div>
                          <dt>最近检测</dt>
                          <dd>{formatDate(user.usage_checked_at)}</dd>
                        </div>
                        <div>
                          <dt>超限开始</dt>
                          <dd>
                            {user.disk_quota_exceeded_since
                              ? formatDate(user.disk_quota_exceeded_since)
                              : "无超限记录"}
                          </dd>
                        </div>
                      </dl>
                    </section>
                    <section
                      className="admin-user-quota"
                      aria-labelledby={`${gpuId}-heading`}
                    >
                      <h3 id={`${gpuId}-heading`}>GPU 配额</h3>
                      <label className="admin-users-field" htmlFor={gpuId}>
                        GPU 使用上限（块）
                        <input
                          id={gpuId}
                          type="number"
                          min="0"
                          max="2147483647"
                          step="1"
                          value={
                            gpuQuotaDrafts[user.id] ??
                            String(user.max_gpus_per_user)
                          }
                          disabled={quotaBusy}
                          onChange={(e) =>
                            setGpuQuotaDrafts((current) => ({
                              ...current,
                              [user.id]: e.target.value,
                            }))
                          }
                        />
                      </label>
                      <div className="admin-user-actions">
                        <button
                          type="button"
                          className="admin-users-button"
                          disabled={quotaBusy}
                          onClick={() => void runQuota(onSaveGpuQuota, user.id)}
                        >
                          {gpuQuotaSaving === user.id
                            ? "保存中…"
                            : "保存 GPU 配额"}
                        </button>
                      </div>
                    </section>
                    <section className="admin-user-quota" aria-labelledby={`${id}-reputation-${user.id}-heading`}>
                      <h3 id={`${id}-reputation-${user.id}-heading`}>信誉分（仅管理员可见）</h3>
                      <p className="admin-user-note">分数越高，申请期限限制越严格；最低为 0。</p>
                      <label className="admin-users-field" htmlFor={`${id}-reputation-${user.id}`}>
                        信誉分
                        <input
                          id={`${id}-reputation-${user.id}`}
                          type="number"
                          min="0"
                          max={2 ** 31 - 1}
                          step="1"
                          value={reputationDrafts[user.id] ?? String(user.reputation_score ?? 0)}
                          disabled={quotaBusy}
                          onChange={(e) => setReputationDrafts((current) => ({ ...current, [user.id]: e.target.value }))}
                        />
                      </label>
                      <div className="admin-user-actions">
                        <button type="button" className="admin-users-button" disabled={quotaBusy}
                          onClick={() => void runQuota((userId) => saveReputation(userId), user.id)}>
                          {reputationSaving === user.id ? "保存中…" : "保存信誉分"}
                        </button>
                        <button type="button" className="admin-users-button" disabled={quotaBusy}
                          onClick={() => void runQuota((userId) => saveReputation(userId, true), user.id)}>
                          清零
                        </button>
                        <button type="button" className="admin-users-button" disabled={quotaBusy}
                          aria-expanded={!!eventOpen[user.id]}
                          aria-controls={`${id}-reputation-events-${user.id}`}
                          onClick={() => eventOpen[user.id]
                            ? setEventOpen((current) => ({ ...current, [user.id]: false }))
                            : void runQuota(loadReputationEvents, user.id)}>
                          {eventBusy === user.id ? "加载中…" : eventOpen[user.id] ? "收起记录" : "查看变更记录"}
                        </button>
                      </div>
                      {eventOpen[user.id] && (
                        <div className="admin-reputation-events" id={`${id}-reputation-events-${user.id}`} aria-live="polite">
                          <div className="admin-user-actions">
                            <strong>信誉分变更记录</strong>
                            <button type="button" className="admin-users-button" disabled={quotaBusy}
                              onClick={() => void runQuota(loadReputationEvents, user.id)}>刷新记录</button>
                          </div>
                          {eventBusy === user.id ? <p className="admin-user-note">正在加载…</p>
                            : eventErrors[user.id] ? <p className="admin-users-error" role="alert">{eventErrors[user.id]}</p>
                            : !events[user.id]?.length ? <p className="admin-user-note">暂无信誉分变更记录</p>
                            : <ol className="admin-reputation-event-list">
                              {events[user.id].map((event, index) => {
                                const type = eventValue(event.event_type);
                                const delta = typeof event.delta === "number" && event.delta > 0 ? `+${event.delta}` : eventValue(event.delta);
                                return <li key={`${eventValue(event.id)}-${index}`}>
                                  <div className="admin-reputation-event-heading">
                                    <strong>{eventLabels[type] || type}</strong>
                                    <span>{delta} · {eventValue(event.score_before)} → {eventValue(event.score_after)}</span>
                                  </div>
                                  <p>{eventValue(event.reason)}</p>
                                  <small>{typeof event.created_at === "string" ? formatDate(event.created_at) : "时间未知"} · 来源 {event.source === "scheduler" ? "系统任务" : event.source === "admin" ? "管理员" : eventValue(event.source)} · 操作人 {event.actor_id == null ? "系统" : eventValue(event.actor_id)} · 容器 {eventValue(event.container_id)}</small>
                                </li>;
                              })}
                            </ol>}
                        </div>
                      )}
                    </section>
                    {!user.approved && (
                      <div className="admin-user-actions">
                        {approvalButton(user.id)}
                      </div>
                    )}
                    {user.role !== "admin" && (
                      <div className="admin-user-danger">
                        <div>
                          <strong>危险操作</strong>
                          <p>删除账号将同时销毁该用户的所有容器。</p>
                        </div>
                        <button
                          type="button"
                          className="admin-users-button admin-users-button-danger"
                          disabled={quotaBusy}
                          onClick={() => onDeleteUser(user.id, user.username)}
                        >
                          <Trash2 size={15} aria-hidden="true" />
                          删除用户
                        </button>
                      </div>
                    )}
                  </div>
                </details>
              );
            })}
          </div>
        )}
      </div>
      <div id={`${id}-pending`} hidden={view !== "pending"}>
        <div className="admin-users-section-heading">
          <h3>待审批申请</h3>
          <p>核对实名与联系方式后，为用户开通访问权限。</p>
        </div>
        {pendingLoading || pendingError || !pendingUsers.length ? (
          <LoadState
            loading={pendingLoading}
            error={pendingError}
            retry={onRetryPending}
            empty="暂无待审批申请"
          />
        ) : (
          <div className="admin-users-grid">
            {pendingUsers.map((user) => (
              <article className="admin-user-pending-card" key={user.id}>
                <header>
                  <div>
                    <strong>{user.real_name || user.username}</strong>
                    <span>
                      @{user.username} · ID {user.id}
                    </span>
                  </div>
                  <span className="admin-user-badge admin-user-badge-warning">
                    待审批
                  </span>
                </header>
                <dl className="admin-user-meta">
                  <div>
                    <dt>真实姓名</dt>
                    <dd>{user.real_name || "未填写"}</dd>
                  </div>
                  <div>
                    <dt>联系方式</dt>
                    <dd>{contact(user.contact_type, user.contact_value)}</dd>
                  </div>
                  <div>
                    <dt>注册时间</dt>
                    <dd>{formatDate(user.created_at)}</dd>
                  </div>
                </dl>
                <div className="admin-user-actions">
                  {approvalButton(user.id)}
                </div>
              </article>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
