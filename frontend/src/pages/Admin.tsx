import { useState, useEffect } from "react";
import { fetcher } from "../api/client";
import "./Admin.css";

interface User {
  id: number;
  username: string;
  display_name: string;
  real_name?: string;
  contact_type?: string;
  contact_value?: string;
  approved: boolean;
  role: string;
  created_at: string;
  disk_quota_bytes: number;
  disk_usage_bytes: number;
  disk_quota_blocked: boolean;
  disk_quota_exceeded_since?: string | null;
  scan_complete: boolean;
  usage_checked_at?: string | null;
  quota_exempt?: boolean;
}

interface PendingUser {
  id: number;
  username: string;
  real_name: string;
  contact_type: string;
  contact_value: string;
  created_at: string;
}

interface Container {
  id: number;
  name: string;
  gpu_ids: string;
  ssh_port: number;
  extra_ports?: Record<string, number> | null;
  ssh_password?: string | null;
  status: string;
  stop_reason?: string | null;
  expires_at: string;
  owner_username: string;
}

interface SystemSettings {
  cpu_mem_gb: number;
  gpu_mem_gb_per_gpu: number;
  max_gpu_sharing_users: number;
  idle_gpu_reclaim_enabled: boolean;
  idle_gpu_util_threshold_percent: number;
  idle_gpu_memory_threshold_percent: number;
  idle_gpu_duration_hours: number;
}

const defaultSettings: SystemSettings = {
  cpu_mem_gb: 8,
  gpu_mem_gb_per_gpu: 32,
  max_gpu_sharing_users: 4,
  idle_gpu_reclaim_enabled: true,
  idle_gpu_util_threshold_percent: 5,
  idle_gpu_memory_threshold_percent: 5,
  idle_gpu_duration_hours: 24,
};

const GIB = 1024 ** 3;

function formatBytes(bytes: number): string {
  if (bytes >= GIB) return `${(bytes / GIB).toFixed(1)} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}

function usagePercent(user: User): number {
  if (!user.disk_quota_bytes) return 0;
  return Math.min(100, (user.disk_usage_bytes / user.disk_quota_bytes) * 100);
}

// ---- 通知组件 ----
type ToastType = "success" | "error" | "info";
interface ToastMsg { id: number; msg: string; type: ToastType; }

let _toastId = 0;

function Toast({ toasts, remove }: { toasts: ToastMsg[]; remove: (id: number) => void }) {
  return (
    <div className="toast-container">
      {toasts.map(t => (
        <div key={t.id} className={`toast toast-${t.type}`}>
          <span>{t.type === "success" ? "✓" : t.type === "error" ? "✕" : "ℹ"}</span>
          <span>{t.msg}</span>
          <button type="button" onClick={() => remove(t.id)}>×</button>
        </div>
      ))}
    </div>
  );
}

// ---- 确认对话框 ----
interface ConfirmState {
  visible: boolean;
  message: string;
  onConfirm: () => void;
}

function ConfirmDialog({ state, onCancel }: { state: ConfirmState; onCancel: () => void }) {
  if (!state.visible) return null;
  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>确认操作</h2>
          <button type="button" className="btn btn-ghost" onClick={onCancel}>×</button>
        </div>
        <div className="modal-body">
          <p style={{ lineHeight: 1.6 }}>{state.message}</p>
        </div>
        <div className="modal-actions">
          <button type="button" className="btn btn-frosted" onClick={onCancel}>取消</button>
          <button
            type="button"
            className="btn btn-frosted btn-danger"
            onClick={() => { state.onConfirm(); onCancel(); }}
          >
            确认
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Admin() {
  const [users, setUsers] = useState<User[]>([]);
  const [pendingUsers, setPendingUsers] = useState<PendingUser[]>([]);
  const [containers, setContainers] = useState<Container[]>([]);
  const [newUser, setNewUser] = useState({ username: "", password: "", display_name: "" });
  const [loading, setLoading] = useState(false);
  const [createError, setCreateError] = useState("");
  const [quotaDrafts, setQuotaDrafts] = useState<Record<number, string>>({});
  const [quotaSaving, setQuotaSaving] = useState<number | null>(null);
  const [quotaRefreshing, setQuotaRefreshing] = useState<number | null>(null);

  // 资源配额
  const [settings, setSettings] = useState<SystemSettings>(defaultSettings);
  const [settingsDraft, setSettingsDraft] = useState<SystemSettings>(defaultSettings);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [settingsError, setSettingsError] = useState("");

  // toast 通知
  const [toasts, setToasts] = useState<ToastMsg[]>([]);
  const pushToast = (msg: string, type: ToastType = "info") => {
    const id = ++_toastId;
    setToasts(prev => [...prev, { id, msg, type }]);
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 3500);
  };
  const removeToast = (id: number) => setToasts(prev => prev.filter(t => t.id !== id));

  // 确认对话框
  const [confirm, setConfirm] = useState<ConfirmState>({ visible: false, message: "", onConfirm: () => { } });
  const askConfirm = (message: string, onConfirm: () => void) =>
    setConfirm({ visible: true, message, onConfirm });
  const closeConfirm = () => setConfirm(s => ({ ...s, visible: false }));

  const loadUsers = async () => {
    const data = await fetcher<User[]>("/admin/users");
    setUsers(data);
    setQuotaDrafts(data.reduce<Record<number, string>>((drafts, user) => {
      drafts[user.id] = (user.disk_quota_bytes / GIB).toString();
      return drafts;
    }, {}));
  };

  const loadPendingUsers = async () => {
    const data = await fetcher<PendingUser[]>("/admin/users/pending");
    setPendingUsers(data);
  };

  const loadContainers = async () => {
    const data = await fetcher<Container[]>("/admin/containers");
    setContainers(data);
  };

  const loadSettings = async () => {
    setSettingsError("");
    try {
      const data = await fetcher<SystemSettings>("/admin/settings");
      setSettings(data);
      setSettingsDraft(data);
      setSettingsLoaded(true);
    } catch (e) {
      const message = e instanceof Error ? e.message : "系统设置加载失败";
      setSettingsLoaded(false);
      setSettingsError(message);
      pushToast(message, "error");
    }
  };

  useEffect(() => {
    loadUsers();
    loadPendingUsers();
    loadContainers();
    loadSettings();
  }, []);

  const handleApprove = async (userId: number) => {
    try {
      await fetcher(`/admin/users/${userId}/approve`, { method: "POST" });
      await loadUsers();
      await loadPendingUsers();
      pushToast("已通过审批", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "操作失败", "error");
    }
  };

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setCreateError("");
    try {
      await fetcher("/admin/users", {
        method: "POST",
        body: JSON.stringify(newUser),
      });
      setNewUser({ username: "", password: "", display_name: "" });
      await loadUsers();
      pushToast(`用户 "${newUser.username}" 创建成功`, "success");
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : "创建失败");
    } finally {
      setLoading(false);
    }
  };

  const forceStop = (id: number) => {
    askConfirm("确定要强制停止该容器吗？", async () => {
      try {
        await fetcher(`/admin/containers/${id}/force-stop`, { method: "POST" });
        await loadContainers();
        pushToast("已强制停止容器", "success");
      } catch (e) {
        pushToast(e instanceof Error ? e.message : "操作失败", "error");
      }
    });
  };

  const forceRemove = (id: number) => {
    askConfirm("确定要清理该容器吗？个人目录会保留。", async () => {
      try {
        await fetcher(`/admin/containers/${id}/force-remove`, { method: "POST" });
        await loadContainers();
        pushToast("容器已清理", "success");
      } catch (e) {
        pushToast(e instanceof Error ? e.message : "操作失败", "error");
      }
    });
  };

  const handleDeleteUser = (id: number, username: string) => {
    askConfirm(`确定要彻底删除用户 "${username}" 吗？该用户的所有容器也会被销毁！`, async () => {
      try {
        await fetcher(`/admin/users/${id}`, { method: "DELETE" });
        await loadUsers();
        await loadContainers();
        pushToast(`用户 "${username}" 已删除`, "success");
      } catch (e) {
        pushToast(e instanceof Error ? e.message : "删除失败", "error");
      }
    });
  };

  const handleSaveQuota = async (userId: number) => {
    const quotaGib = Number(quotaDrafts[userId]);
    if (!Number.isFinite(quotaGib) || quotaGib <= 0) {
      pushToast("磁盘配额必须是大于 0 的数字", "error");
      return;
    }
    setQuotaSaving(userId);
    try {
      await fetcher(`/admin/users/${userId}/quota`, {
        method: "PUT",
        body: JSON.stringify({ quota_gib: quotaGib }),
      });
      await loadUsers();
      pushToast("磁盘配额已更新", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "磁盘配额更新失败", "error");
    } finally {
      setQuotaSaving(null);
    }
  };

  const handleRefreshQuota = async (userId: number) => {
    setQuotaRefreshing(userId);
    try {
      await fetcher(`/admin/users/${userId}/quota/refresh`, { method: "POST" });
      await loadUsers();
      pushToast("磁盘使用量已刷新", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "磁盘使用量刷新失败", "error");
    } finally {
      setQuotaRefreshing(null);
    }
  };

  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settingsLoaded) {
      pushToast("系统设置尚未成功加载，已阻止保存", "error");
      return;
    }
    setSettingsSaving(true);
    try {
      const saved = await fetcher<SystemSettings>("/admin/settings", {
        method: "PUT",
        body: JSON.stringify(settingsDraft),
      });
      setSettings(saved);
      setSettingsDraft(saved);
      pushToast("系统设置已保存", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "保存失败", "error");
    } finally {
      setSettingsSaving(false);
    }
  };

  return (
    <div className="admin-page">
      <Toast toasts={toasts} remove={removeToast} />
      <ConfirmDialog state={confirm} onCancel={closeConfirm} />
      <h1>管理后台</h1>

      {/* 资源配额设置 */}
      <section className="admin-section">
        <h2>资源配额设置</h2>
        <form onSubmit={handleSaveSettings} className="admin-settings-grid">
          <div className="setting-item">
            <label>
              <strong>CPU 容器内存配额</strong>
              <span className="setting-desc">每个纯 CPU 容器可使用的最大内存</span>
            </label>
            <div className="setting-input-wrap">
              <input
                type="number"
                min={1}
                max={512}
                value={settingsDraft.cpu_mem_gb}
                onChange={(e) => setSettingsDraft(s => ({ ...s, cpu_mem_gb: Number(e.target.value) }))}
              />
              <span>GB</span>
            </div>
          </div>
          <div className="setting-item">
            <label>
              <strong>GPU 容器每卡内存配额</strong>
              <span className="setting-desc">每选 1 张 GPU 分配的内存量（总量 = 张数 × 此值）</span>
            </label>
            <div className="setting-input-wrap">
              <input
                type="number"
                min={1}
                max={512}
                value={settingsDraft.gpu_mem_gb_per_gpu}
                onChange={(e) => setSettingsDraft(s => ({ ...s, gpu_mem_gb_per_gpu: Number(e.target.value) }))}
              />
              <span>GB / 卡</span>
            </div>
          </div>
          <div className="setting-item setting-item-preview">
            <label>
              <strong>示例预览</strong>
              <span className="setting-desc">当前配置下申请场景的内存分配</span>
            </label>
            <div className="setting-preview">
              <div>纯 CPU 容器 → <b>{settingsDraft.cpu_mem_gb} GB</b></div>
              <div>选 1 张 GPU → <b>{settingsDraft.gpu_mem_gb_per_gpu} GB</b></div>
              <div>选 2 张 GPU → <b>{settingsDraft.gpu_mem_gb_per_gpu * 2} GB</b></div>
              <div>选 4 张 GPU → <b>{settingsDraft.gpu_mem_gb_per_gpu * 4} GB</b></div>
            </div>
          </div>
          <div className="setting-item">
            <label>
              <strong>单卡最多共用人数</strong>
              <span className="setting-desc">同一块 GPU 上允许并行使用的不同用户上限</span>
            </label>
            <div className="setting-input-wrap">
              <input
                type="number"
                min={1}
                max={20}
                value={settingsDraft.max_gpu_sharing_users}
                onChange={(e) => setSettingsDraft(s => ({ ...s, max_gpu_sharing_users: Number(e.target.value) }))}
              />
              <span>人 / 卡</span>
            </div>
          </div>
          <div className="setting-group-title">GPU 低利用自动回收</div>
          <div className="setting-item setting-item-wide">
            <label className="setting-toggle-row">
              <span>
                <strong>启用自动回收</strong>
                <span className="setting-desc">达到连续低利用条件后立即停止并销毁 Docker 容器，宿主机 /workspace 保留</span>
              </span>
              <input
                type="checkbox"
                checked={settingsDraft.idle_gpu_reclaim_enabled}
                onChange={(e) => setSettingsDraft(s => ({ ...s, idle_gpu_reclaim_enabled: e.target.checked }))}
              />
            </label>
          </div>
          <div className="setting-item">
            <label>
              <strong>GPU 利用率阈值</strong>
              <span className="setting-desc">整张物理卡利用率必须严格低于该值</span>
            </label>
            <div className="setting-input-wrap">
              <input type="number" min={0} max={100} disabled={!settingsDraft.idle_gpu_reclaim_enabled} value={settingsDraft.idle_gpu_util_threshold_percent} onChange={(e) => setSettingsDraft(s => ({ ...s, idle_gpu_util_threshold_percent: Number(e.target.value) }))} />
              <span>%</span>
            </div>
          </div>
          <div className="setting-item">
            <label>
              <strong>显存占用阈值</strong>
              <span className="setting-desc">整张物理卡显存占用必须严格低于该值</span>
            </label>
            <div className="setting-input-wrap">
              <input type="number" min={0} max={100} disabled={!settingsDraft.idle_gpu_reclaim_enabled} value={settingsDraft.idle_gpu_memory_threshold_percent} onChange={(e) => setSettingsDraft(s => ({ ...s, idle_gpu_memory_threshold_percent: Number(e.target.value) }))} />
              <span>%</span>
            </div>
          </div>
          <div className="setting-item">
            <label>
              <strong>连续低利用时长</strong>
              <span className="setting-desc">多 GPU 容器的全部所选 GPU 必须同时持续低于两个阈值</span>
            </label>
            <div className="setting-input-wrap">
              <input type="number" min={1} max={8760} disabled={!settingsDraft.idle_gpu_reclaim_enabled} value={settingsDraft.idle_gpu_duration_hours} onChange={(e) => setSettingsDraft(s => ({ ...s, idle_gpu_duration_hours: Number(e.target.value) }))} />
              <span>小时</span>
            </div>
          </div>
          <div className="setting-item setting-item-wide setting-note">
            GPU 共享时，各容器使用同一张物理卡的统一指标；每个容器仍按其完整 GPU 集合独立判断。
          </div>
          {settingsError && <div className="form-error setting-item-wide">{settingsError}</div>}
          <div className="setting-item setting-item-action">
            <button type="submit" className="btn btn-primary" disabled={settingsSaving || !settingsLoaded}>
              {settingsSaving ? "保存中…" : "保存配置"}
            </button>
            <button
              type="button"
              className="btn btn-frosted"
              onClick={() => setSettingsDraft(settings)}
              disabled={settingsSaving || !settingsLoaded}
            >
              重置
            </button>
          </div>
        </form>
      </section>

      {/* 创建用户 */}
      <section className="admin-section">
        <h2>创建用户</h2>
        <form onSubmit={handleCreateUser} className="admin-form">
          <input
            placeholder="用户名"
            value={newUser.username}
            onChange={(e) => setNewUser({ ...newUser, username: e.target.value })}
            required
          />
          <input
            type="password"
            placeholder="密码"
            value={newUser.password}
            onChange={(e) => setNewUser({ ...newUser, password: e.target.value })}
            required
          />
          <input
            placeholder="显示名称（可选）"
            value={newUser.display_name}
            onChange={(e) => setNewUser({ ...newUser, display_name: e.target.value })}
          />
          <button type="submit" className="btn btn-primary" disabled={loading}>
            {loading ? "创建中..." : "创建"}
          </button>
        </form>
        {createError && <div className="form-error">{createError}</div>}
      </section>

      {/* 待审批用户 */}
      <section className="admin-section">
        <h2>待审批用户</h2>
        {pendingUsers.length === 0 ? (
          <p className="admin-empty">暂无待审批用户</p>
        ) : (
          <table className="admin-table">
            <thead>
              <tr>
                <th>用户名</th>
                <th>实名</th>
                <th>联系方式</th>
                <th>注册时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {pendingUsers.map((u) => (
                <tr key={u.id}>
                  <td>{u.username}</td>
                  <td>{u.real_name}</td>
                  <td>{u.contact_type === "wechat" ? "微信 " : "手机 "}{u.contact_value}</td>
                  <td>{new Date(u.created_at).toLocaleString()}</td>
                  <td>
                    <button className="btn btn-small" onClick={() => handleApprove(u.id)}>通过</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* 用户列表 */}
      <section className="admin-section">
        <h2>用户列表</h2>
        <table className="admin-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>用户名</th>
              <th>实名</th>
              <th>联系方式</th>
              <th>审批状态</th>
              <th>磁盘使用</th>
              <th>磁盘配额</th>
              <th>配额状态</th>
              <th>角色</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>{u.id}</td>
                <td>{u.username}</td>
                <td>{u.real_name ?? "-"}</td>
                <td>{u.contact_value ? (u.contact_type === "wechat" ? "微信 " : "手机 ") + u.contact_value : "-"}</td>
                <td>{u.approved ? "已通过" : "待审批"}</td>
                <td>
                  <div className="quota-usage-cell">
                    <span>{formatBytes(u.disk_usage_bytes)} / {formatBytes(u.disk_quota_bytes)}</span>
                    {!u.quota_exempt && (
                      <span className="quota-meter"><span style={{ width: `${usagePercent(u)}%` }} /></span>
                    )}
                  </div>
                </td>
                <td>
                  {u.quota_exempt ? (
                    "不受限"
                  ) : (
                    <div className="quota-editor">
                      <input
                        type="number"
                        min="0.1"
                        step="0.1"
                        value={quotaDrafts[u.id] ?? ""}
                        onChange={(e) => setQuotaDrafts((drafts) => ({ ...drafts, [u.id]: e.target.value }))}
                      />
                      <span>GiB</span>
                      <button
                        type="button"
                        className="btn btn-small"
                        onClick={() => handleSaveQuota(u.id)}
                        disabled={quotaSaving === u.id || quotaRefreshing === u.id}
                      >
                        {quotaSaving === u.id ? "保存中" : "保存"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-small"
                        onClick={() => handleRefreshQuota(u.id)}
                        disabled={quotaSaving === u.id || quotaRefreshing === u.id}
                      >
                        {quotaRefreshing === u.id ? "检测中" : "刷新"}
                      </button>
                    </div>
                  )}
                </td>
                <td>
                  <span className={`quota-status ${u.disk_quota_blocked ? "blocked" : "normal"}`}>
                    {u.quota_exempt ? "管理员豁免" : !u.scan_complete ? "检测失败，已暂停申请" : u.disk_quota_blocked ? "已禁止申请" : "正常"}
                  </span>
                  {!u.quota_exempt && u.usage_checked_at && (
                    <small className="quota-since">检测于 {new Date(u.usage_checked_at).toLocaleString()}</small>
                  )}
                  {u.disk_quota_exceeded_since && !u.quota_exempt && (
                    <small className="quota-since">{new Date(u.disk_quota_exceeded_since).toLocaleString()} 起超限</small>
                  )}
                </td>
                <td>{u.role}</td>
                <td>
                  {u.role !== "admin" && (
                    <button
                      className="btn btn-small btn-danger"
                      onClick={() => handleDeleteUser(u.id, u.username)}
                      style={{ color: "#ef4444" }}
                    >
                      删除
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* 所有容器 */}
      <section className="admin-section">
        <h2>全部容器</h2>
        <table className="admin-table">
          <thead>
            <tr>
              <th>名称</th>
              <th>GPU</th>
              <th>SSH</th>
              <th>服务端口</th>
              <th>密码</th>
              <th>状态</th>
              <th>用户</th>
              <th>到期</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {containers.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td>{c.gpu_ids || "CPU"}</td>
                <td>{c.ssh_port}</td>
                <td>
                  {c.extra_ports
                    ? Object.entries(c.extra_ports).map(([k, v]) => `${k}→${v}`).join(" ")
                    : "-"}
                </td>
                <td>
                  {c.ssh_password ? (
                    <code title="点击复制" onClick={() => navigator.clipboard.writeText(c.ssh_password!)} style={{ cursor: "pointer" }}>
                      {c.ssh_password}
                    </code>
                  ) : (
                    "-"
                  )}
                </td>
                <td>
                  {c.status}
                  {c.stop_reason === "disk_quota" && <small className="quota-since">空间超限</small>}
                </td>
                <td>{c.owner_username}</td>
                <td>{new Date(c.expires_at).toLocaleString()}</td>
                <td>
                  {c.status === "running" && (
                    <button className="btn btn-small" onClick={() => forceStop(c.id)}>
                      停止
                    </button>
                  )}
                  {(c.status === "stopped" || c.status === "running") && (
                    <button className="btn btn-small" onClick={() => forceRemove(c.id)}>
                      清理
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
