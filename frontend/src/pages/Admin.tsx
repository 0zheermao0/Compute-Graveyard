import { useState, useEffect, useRef } from "react";
import {
  UsersRound,
  Boxes,
  Server,
  SlidersHorizontal,
  ShieldCheck,
  UserCheck,
  Activity,
  ChevronRight,
  Plus,
  RefreshCw,
  Search,
} from "lucide-react";
import { fetcher } from "../api/client";
import AdminUsersPanel from "../components/AdminUsersPanel";
import "./Admin.css";
import "./AdminWorkspace.css";

export interface User {
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
  max_gpus_per_user: number;
  reputation_score: number;
  disk_usage_bytes: number;
  disk_quota_blocked: boolean;
  disk_quota_exceeded_since?: string | null;
  scan_complete: boolean;
  usage_checked_at?: string | null;
  quota_exempt?: boolean;
}

export interface PendingUser {
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
  status: string;
  stop_reason?: string | null;
  expires_at: string;
  owner_username: string;
  node_id?: string | null;
  node_name?: string | null;
  access_host?: string | null;
}

interface SystemSettings {
  cpu_mem_gb: number;
  gpu_mem_gb_per_gpu: number;
  max_gpu_sharing_users: number;
  idle_gpu_reclaim_enabled: boolean;
  idle_gpu_dual_low_enabled: boolean;
  idle_gpu_memory_unchanged_enabled: boolean;
  idle_gpu_shrink_enabled: boolean;
  idle_gpu_util_threshold_percent: number;
  idle_gpu_memory_threshold_percent: number;
  idle_gpu_duration_hours: number;
  reputation_initial_score: number;
  reputation_idle_warning_points: number;
  reputation_idle_reclaim_points: number;
  reputation_idle_shrink_points: number;
  reputation_expiry_reward_points: number;
  reputation_tier1_threshold: number;
  reputation_tier1_max_days: number;
  reputation_tier2_threshold: number;
  reputation_tier2_max_days: number;
}

interface GPUInfo {
  index: number;
  name: string;
  memory_used_mb?: number | null;
  memory_total_mb?: number | null;
  memory_percent?: number | null;
  temperature?: number | null;
  utilization?: number | null;
}

interface SystemLoad {
  cpu_percent: number;
  memory_used_gb: number;
  memory_total_gb: number;
  memory_percent: number;
  disk_free_gb: number;
  disk_total_gb: number;
}

interface ComputeNode {
  id: string;
  name: string;
  base_url: string;
  public_host: string;
  enabled: boolean;
  schedulable: boolean;
  last_seen_at?: string | null;
  created_at: string;
  updated_at: string;
  has_agent_token: boolean;
  is_local: boolean;
}

interface NodeInventory {
  node_id: string;
  node_name: string;
  public_host: string;
  role: string;
  gpus: GPUInfo[];
  system_load: SystemLoad;
  containers: Array<{ status: string }>;
}

interface NodeInventoryResult {
  node: ComputeNode;
  online: boolean;
  inventory: NodeInventory | null;
  error?: string | null;
}

interface NodeDraft {
  id: string;
  name: string;
  base_url: string;
  public_host: string;
  agent_token: string;
  enabled: boolean;
  schedulable: boolean;
}

const emptyNodeDraft: NodeDraft = {
  id: "",
  name: "",
  base_url: "",
  public_host: "",
  agent_token: "",
  enabled: true,
  schedulable: true,
};

const defaultSettings: SystemSettings = {
  cpu_mem_gb: 8,
  gpu_mem_gb_per_gpu: 32,
  max_gpu_sharing_users: 4,
  idle_gpu_reclaim_enabled: true,
  idle_gpu_dual_low_enabled: true,
  idle_gpu_memory_unchanged_enabled: true,
  idle_gpu_shrink_enabled: true,
  idle_gpu_util_threshold_percent: 5,
  idle_gpu_memory_threshold_percent: 5,
  idle_gpu_duration_hours: 24,
  reputation_initial_score: 0,
  reputation_idle_warning_points: 1,
  reputation_idle_reclaim_points: 2,
  reputation_idle_shrink_points: 2,
  reputation_expiry_reward_points: 2,
  reputation_tier1_threshold: 5,
  reputation_tier1_max_days: 5,
  reputation_tier2_threshold: 10,
  reputation_tier2_max_days: 3,
};

const reputationFields = [
  { key: "reputation_initial_score", label: "新用户初始分", unit: "分" },
  { key: "reputation_idle_warning_points", label: "闲置预警加分", unit: "分" },
  { key: "reputation_idle_reclaim_points", label: "闲置回收加分", unit: "分" },
  { key: "reputation_idle_shrink_points", label: "闲置缩卡加分", unit: "分" },
  { key: "reputation_expiry_reward_points", label: "正常到期奖励减分", unit: "分" },
  { key: "reputation_tier1_threshold", label: "第一档分数阈值", unit: "分" },
  { key: "reputation_tier1_max_days", label: "第一档申请期限上限", unit: "天" },
  { key: "reputation_tier2_threshold", label: "第二档分数阈值", unit: "分" },
  { key: "reputation_tier2_max_days", label: "第二档申请期限上限", unit: "天" },
] as const;

const GIB = 1024 ** 3;

// ---- 通知组件 ----
type ToastType = "success" | "error" | "info";
interface ToastMsg {
  id: number;
  msg: string;
  type: ToastType;
}

let _toastId = 0;

function Toast({
  toasts,
  remove,
}: {
  toasts: ToastMsg[];
  remove: (id: number) => void;
}) {
  return (
    <div className="toast-container">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`toast toast-${t.type}`}
          role={t.type === "error" ? "alert" : "status"}
        >
          <span>
            {t.type === "success" ? "✓" : t.type === "error" ? "✕" : "ℹ"}
          </span>
          <span>{t.msg}</span>
          <button
            type="button"
            aria-label="关闭通知"
            onClick={() => remove(t.id)}
          >
            ×
          </button>
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

function ConfirmDialog({
  state,
  onCancel,
}: {
  state: ConfirmState;
  onCancel: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!state.visible) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    dialogRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => {
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [state.visible]);
  if (!state.visible) return null;
  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div
        ref={dialogRef}
        className="modal modal-sm"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-confirm-title"
        aria-describedby="admin-confirm-message"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
          }
          if (event.key !== "Tab") return;
          const buttons =
            dialogRef.current?.querySelectorAll<HTMLButtonElement>(
              "button:not(:disabled)",
            );
          if (!buttons?.length) return;
          const first = buttons[0];
          const last = buttons[buttons.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }}
      >
        <div className="modal-header">
          <h2 id="admin-confirm-title">确认操作</h2>
          <button
            type="button"
            className="btn btn-ghost"
            aria-label="关闭确认对话框"
            onClick={onCancel}
          >
            ×
          </button>
        </div>
        <div className="modal-body">
          <p id="admin-confirm-message" style={{ lineHeight: 1.6 }}>
            {state.message}
          </p>
        </div>
        <div className="modal-actions">
          <button type="button" className="btn btn-frosted" onClick={onCancel}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-frosted btn-danger"
            onClick={() => {
              state.onConfirm();
              onCancel();
            }}
          >
            确认
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Admin() {
  const [activeModule, setActiveModule] = useState<
    "users" | "containers" | "nodes" | "policies"
  >("users");
  const [usersLoading, setUsersLoading] = useState(true);
  const [usersError, setUsersError] = useState("");
  const [pendingLoading, setPendingLoading] = useState(true);
  const [pendingError, setPendingError] = useState("");
  const [containersLoading, setContainersLoading] = useState(true);
  const [containersError, setContainersError] = useState("");
  const [nodesLoading, setNodesLoading] = useState(false);
  const [nodesError, setNodesError] = useState("");
  const [nodeFormOpen, setNodeFormOpen] = useState(false);
  const [containerActionId, setContainerActionId] = useState<number | null>(
    null,
  );
  const containerActionLock = useRef(false);
  const nodeFormRef = useRef<HTMLFormElement>(null);
  const [containerSearch, setContainerSearch] = useState("");
  const [containerStatus, setContainerStatus] = useState("all");
  const [containerNode, setContainerNode] = useState("all");
  const usersSnapshotRef = useRef<User[]>([]);
  const usersRequestRef = useRef(0);
  const [users, setUsers] = useState<User[]>([]);
  const [pendingUsers, setPendingUsers] = useState<PendingUser[]>([]);
  const [containers, setContainers] = useState<Container[]>([]);
  const [showRemovedContainers, setShowRemovedContainers] = useState(false);
  const [newUser, setNewUser] = useState({
    username: "",
    password: "",
    display_name: "",
  });
  const [loading, setLoading] = useState(false);
  const [createError, setCreateError] = useState("");
  const [quotaDrafts, setQuotaDrafts] = useState<Record<number, string>>({});
  const [gpuQuotaDrafts, setGpuQuotaDrafts] = useState<Record<number, string>>(
    {},
  );
  const [reputationDrafts, setReputationDrafts] = useState<Record<number, string>>({});
  const [reputationSaving, setReputationSaving] = useState<number | null>(null);
  const reputationLock = useRef(false);
  const [quotaSaving, setQuotaSaving] = useState<number | null>(null);
  const [gpuQuotaSaving, setGpuQuotaSaving] = useState<number | null>(null);
  const [quotaRefreshing, setQuotaRefreshing] = useState<number | null>(null);
  const [isMaster, setIsMaster] = useState(false);
  const [nodes, setNodes] = useState<ComputeNode[]>([]);
  const [nodeInventories, setNodeInventories] = useState<
    Record<string, NodeInventoryResult>
  >({});
  const [nodeDraft, setNodeDraft] = useState<NodeDraft>(emptyNodeDraft);
  const [editingNodeId, setEditingNodeId] = useState<string | null>(null);
  const [nodeSaving, setNodeSaving] = useState(false);
  const [nodeTesting, setNodeTesting] = useState<string | null>(null);
  const [nodeMutatingId, setNodeMutatingId] = useState<string | null>(null);
  const nodeOperationLock = useRef(false);
  const nodeBusy =
    nodeSaving || nodeTesting !== null || nodeMutatingId !== null;

  // 资源配额
  const [settings, setSettings] = useState<SystemSettings>(defaultSettings);
  const [settingsDraft, setSettingsDraft] =
    useState<SystemSettings>(defaultSettings);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [settingsError, setSettingsError] = useState("");

  // toast 通知
  const [toasts, setToasts] = useState<ToastMsg[]>([]);
  const pushToast = (msg: string, type: ToastType = "info") => {
    const id = ++_toastId;
    setToasts((prev) => [...prev, { id, msg, type }].slice(-3));
    setTimeout(
      () => setToasts((prev) => prev.filter((t) => t.id !== id)),
      3500,
    );
  };
  const removeToast = (id: number) =>
    setToasts((prev) => prev.filter((t) => t.id !== id));

  // 确认对话框
  const [confirm, setConfirm] = useState<ConfirmState>({
    visible: false,
    message: "",
    onConfirm: () => {},
  });
  const askConfirm = (message: string, onConfirm: () => void) =>
    setConfirm({ visible: true, message, onConfirm });
  const closeConfirm = () => setConfirm((s) => ({ ...s, visible: false }));

  const loadUsers = async (reset?: {
    diskUserId?: number;
    gpuUserId?: number;
    reputationUserId?: number;
  }) => {
    const requestId = ++usersRequestRef.current;
    setUsersLoading(true);
    setUsersError("");
    try {
      const data = await fetcher<User[]>("/admin/users");
      if (requestId !== usersRequestRef.current) return;
      const previous = new Map(
        usersSnapshotRef.current.map((user) => [user.id, user]),
      );
      usersSnapshotRef.current = data;
      setUsers(data);
      setReputationDrafts((current) =>
        data.reduce<Record<number, string>>((drafts, user) => {
          const oldUser = previous.get(user.id);
          const dirty = oldUser && current[user.id] !== undefined &&
            current[user.id] !== String(oldUser.reputation_score ?? 0);
          drafts[user.id] = dirty && reset?.reputationUserId !== user.id
            ? current[user.id]
            : String(user.reputation_score ?? 0);
          return drafts;
        }, {}),
      );
      // 刷新一位用户后，保留其他用户尚未保存的配额草稿。
      setQuotaDrafts((current) =>
        data.reduce<Record<number, string>>((drafts, user) => {
          const oldUser = previous.get(user.id);
          const dirty =
            oldUser &&
            current[user.id] !== (oldUser.disk_quota_bytes / GIB).toString();
          drafts[user.id] =
            dirty && reset?.diskUserId !== user.id
              ? current[user.id]
              : (user.disk_quota_bytes / GIB).toString();
          return drafts;
        }, {}),
      );
      setGpuQuotaDrafts((current) =>
        data.reduce<Record<number, string>>((drafts, user) => {
          const oldUser = previous.get(user.id);
          const dirty =
            oldUser &&
            current[user.id] !== oldUser.max_gpus_per_user.toString();
          drafts[user.id] =
            dirty && reset?.gpuUserId !== user.id
              ? current[user.id]
              : user.max_gpus_per_user.toString();
          return drafts;
        }, {}),
      );
    } catch (error) {
      if (requestId === usersRequestRef.current) {
        setUsersError(
          error instanceof Error ? error.message : "用户列表加载失败",
        );
      }
    } finally {
      if (requestId === usersRequestRef.current) setUsersLoading(false);
    }
  };

  const loadPendingUsers = async () => {
    setPendingLoading(true);
    setPendingError("");
    try {
      setPendingUsers(await fetcher<PendingUser[]>("/admin/users/pending"));
    } catch (error) {
      setPendingError(
        error instanceof Error ? error.message : "审批列表加载失败",
      );
    } finally {
      setPendingLoading(false);
    }
  };

  const loadContainers = async () => {
    setContainersLoading(true);
    setContainersError("");
    try {
      const data = await fetcher<Container[]>("/admin/containers");
      setContainers(
        data.sort(
          (a, b) =>
            new Date(b.expires_at).getTime() - new Date(a.expires_at).getTime(),
        ),
      );
    } catch (error) {
      setContainersError(
        error instanceof Error ? error.message : "容器列表加载失败",
      );
    } finally {
      setContainersLoading(false);
    }
  };

  const loadNodes = async () => {
    setNodesLoading(true);
    setNodesError("");
    try {
      const [nodeRows, inventoryRows] = await Promise.all([
        fetcher<ComputeNode[]>("/admin/nodes"),
        fetcher<NodeInventoryResult[]>("/admin/nodes/inventory"),
      ]);
      setNodes(nodeRows);
      setNodeInventories(
        inventoryRows.reduce<Record<string, NodeInventoryResult>>(
          (result, row) => {
            result[row.node.id] = row;
            return result;
          },
          {},
        ),
      );
    } catch (error) {
      setNodesError(
        error instanceof Error ? error.message : "节点列表加载失败",
      );
    } finally {
      setNodesLoading(false);
    }
  };

  const loadSettings = async () => {
    setSettingsError("");
    try {
      const data = await fetcher<SystemSettings>("/admin/settings");
      const normalized = { ...defaultSettings, ...data };
      setSettings(normalized);
      setSettingsDraft(normalized);
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
    let active = true;
    fetcher<{ role: string }>("/health")
      .then((health) => {
        if (!active || health.role !== "master") return;
        setIsMaster(true);
        loadNodes().catch((e) => {
          if (active)
            pushToast(e instanceof Error ? e.message : "节点加载失败", "error");
        });
      })
      .catch(() => {
        if (active) setIsMaster(false);
      });
    loadSettings();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (nodeFormOpen && activeModule === "nodes") {
      nodeFormRef.current
        ?.querySelector<HTMLInputElement>("input:not(:disabled)")
        ?.focus();
    }
  }, [nodeFormOpen, editingNodeId, activeModule]);

  const handleSaveNode = async (e: React.FormEvent) => {
    e.preventDefault();
    if (nodeOperationLock.current) return;
    nodeOperationLock.current = true;
    setNodeSaving(true);
    try {
      const payload = {
        name: nodeDraft.name,
        base_url: nodeDraft.base_url,
        public_host: nodeDraft.public_host,
        enabled: nodeDraft.enabled,
        schedulable: nodeDraft.schedulable,
        ...(nodeDraft.agent_token
          ? { agent_token: nodeDraft.agent_token }
          : {}),
      };
      if (editingNodeId) {
        await fetcher(`/admin/nodes/${encodeURIComponent(editingNodeId)}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
      } else {
        await fetcher("/admin/nodes", {
          method: "POST",
          body: JSON.stringify({ id: nodeDraft.id, ...payload }),
        });
      }
      setNodeDraft(emptyNodeDraft);
      setEditingNodeId(null);
      setNodeFormOpen(false);
      await loadNodes();
      pushToast(editingNodeId ? "节点已更新" : "节点已添加", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "节点保存失败", "error");
    } finally {
      nodeOperationLock.current = false;
      setNodeSaving(false);
    }
  };

  const handleEditNode = (node: ComputeNode) => {
    if (nodeOperationLock.current) return;
    setNodeFormOpen(true);
    setEditingNodeId(node.id);
    setNodeDraft({
      id: node.id,
      name: node.name,
      base_url: node.base_url,
      public_host: node.public_host,
      agent_token: "",
      enabled: node.enabled,
      schedulable: node.schedulable,
    });
  };

  const handleToggleNode = async (
    node: ComputeNode,
    field: "enabled" | "schedulable",
    value: boolean,
  ) => {
    if (nodeOperationLock.current) return;
    nodeOperationLock.current = true;
    setNodeMutatingId(node.id);
    try {
      await fetcher(`/admin/nodes/${encodeURIComponent(node.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ [field]: value }),
      });
      await loadNodes();
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "节点状态更新失败", "error");
    } finally {
      nodeOperationLock.current = false;
      setNodeMutatingId(null);
    }
  };

  const handleTestNode = async (nodeId: string) => {
    if (nodeOperationLock.current) return;
    nodeOperationLock.current = true;
    setNodeTesting(nodeId);
    try {
      const result = await fetcher<{
        ok: boolean;
        node: ComputeNode;
        inventory: NodeInventory;
      }>(`/admin/nodes/${encodeURIComponent(nodeId)}/test`, { method: "POST" });
      setNodeInventories((current) => ({
        ...current,
        [nodeId]: {
          node: result.node,
          online: result.ok,
          inventory: result.inventory,
          error: null,
        },
      }));
      setNodes((current) =>
        current.map((node) => (node.id === nodeId ? result.node : node)),
      );
      pushToast("节点连接正常", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "节点连接失败", "error");
    } finally {
      nodeOperationLock.current = false;
      setNodeTesting(null);
    }
  };

  const handleDeleteNode = (node: ComputeNode) => {
    askConfirm(`确定删除节点 "${node.name}" 吗？`, async () => {
      if (nodeOperationLock.current) return;
      nodeOperationLock.current = true;
      setNodeMutatingId(node.id);
      try {
        await fetcher(`/admin/nodes/${encodeURIComponent(node.id)}`, {
          method: "DELETE",
        });
        await loadNodes();
        pushToast("节点已删除", "success");
      } catch (e) {
        pushToast(e instanceof Error ? e.message : "节点删除失败", "error");
      } finally {
        nodeOperationLock.current = false;
        setNodeMutatingId(null);
      }
    });
  };

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

  const runContainerAction = async (
    id: number,
    action: "force-stop" | "force-remove",
  ) => {
    if (containerActionLock.current) return;
    containerActionLock.current = true;
    setContainerActionId(id);
    try {
      await fetcher(`/admin/containers/${id}/${action}`, { method: "POST" });
      await loadContainers();
      pushToast(
        action === "force-stop" ? "已强制停止容器" : "容器已清理",
        "success",
      );
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "操作失败", "error");
    } finally {
      containerActionLock.current = false;
      setContainerActionId(null);
    }
  };

  const forceStop = (id: number) => {
    const name =
      containers.find((container) => container.id === id)?.name || String(id);
    askConfirm(
      `确定强制停止容器「${name}」吗？正在运行的实验将被中断。`,
      () => void runContainerAction(id, "force-stop"),
    );
  };

  const forceRemove = (id: number) => {
    const name =
      containers.find((container) => container.id === id)?.name || String(id);
    askConfirm(
      `确定清理容器「${name}」吗？该容器将被销毁，个人目录会保留。`,
      () => void runContainerAction(id, "force-remove"),
    );
  };

  const handleDeleteUser = (id: number, username: string) => {
    askConfirm(
      `确定要彻底删除用户 "${username}" 吗？该用户的所有容器也会被销毁！`,
      async () => {
        try {
          await fetcher(`/admin/users/${id}`, { method: "DELETE" });
          await loadUsers();
          await loadContainers();
          pushToast(`用户 "${username}" 已删除`, "success");
        } catch (e) {
          pushToast(e instanceof Error ? e.message : "删除失败", "error");
        }
      },
    );
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
      await loadUsers({ diskUserId: userId });
      pushToast("磁盘配额已更新", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "磁盘配额更新失败", "error");
    } finally {
      setQuotaSaving(null);
    }
  };

  const handleSaveGpuQuota = async (userId: number) => {
    const value = gpuQuotaDrafts[userId];
    const limit = Number(value);
    if (
      value === undefined ||
      value.trim() === "" ||
      !Number.isSafeInteger(limit) ||
      limit < 0 ||
      limit > 2147483647
    ) {
      pushToast("GPU 配额必须是非负整数", "error");
      return;
    }
    setGpuQuotaSaving(userId);
    try {
      await fetcher(`/admin/users/${userId}/gpu-quota`, {
        method: "PUT",
        body: JSON.stringify({ max_gpus_per_user: limit }),
      });
      await loadUsers({ gpuUserId: userId });
      pushToast("GPU 配额已更新", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "GPU 配额更新失败", "error");
    } finally {
      setGpuQuotaSaving(null);
    }
  };

  const handleSaveReputation = async (userId: number, reset = false) => {
    if (reputationLock.current || quotaSaving !== null || gpuQuotaSaving !== null || quotaRefreshing !== null) return;
    const value = reset ? "0" : reputationDrafts[userId];
    const score = Number(value);
    if (value === undefined || value.trim() === "" || !Number.isSafeInteger(score) || score < 0 || score > 2 ** 31 - 1) {
      throw new Error("信誉分必须是 0–2147483647 之间的整数");
    }
    reputationLock.current = true;
    setReputationSaving(userId);
    try {
      await fetcher(`/admin/users/${userId}/reputation-score`, {
        method: "PUT",
        body: JSON.stringify({ reputation_score: score }),
      });
      await loadUsers({ reputationUserId: userId });
      pushToast(reset ? "信誉分已清零" : "信誉分已更新", "success");
    } catch (error) {
      throw new Error(error instanceof Error ? error.message : "信誉分更新失败");
    } finally {
      reputationLock.current = false;
      setReputationSaving(null);
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
    if (reputationFields.some(({ key }) => !Number.isSafeInteger(settingsDraft[key]) || settingsDraft[key] < 0 || settingsDraft[key] > 2 ** 31 - 1)) {
      pushToast("信誉分策略必须填写 0–2147483647 之间的整数", "error");
      return;
    }
    if (settingsDraft.reputation_tier2_threshold <= settingsDraft.reputation_tier1_threshold) {
      pushToast("第二档阈值必须严格大于第一档阈值", "error");
      return;
    }
    const days1 = settingsDraft.reputation_tier1_max_days;
    const days2 = settingsDraft.reputation_tier2_max_days;
    if (days1 < 1 || days1 > 7 || days2 < 1 || days2 > 7 || days2 > days1) {
      pushToast("申请期限须为 1–7 天，第二档不得大于第一档", "error");
      return;
    }
    setSettingsSaving(true);
    try {
      const saved = await fetcher<SystemSettings>("/admin/settings", {
        method: "PUT",
        body: JSON.stringify(settingsDraft),
      });
      const normalized = { ...defaultSettings, ...saved };
      setSettings(normalized);
      setSettingsDraft(normalized);
      pushToast("系统设置已保存", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "保存失败", "error");
    } finally {
      setSettingsSaving(false);
    }
  };

  const settingsDirty = Object.entries(settingsDraft).some(
    ([key, value]) => value !== settings[key as keyof SystemSettings],
  );
  const runningContainers = containers.filter(
    (container) => container.status === "running",
  ).length;
  const onlineNodes = nodes.filter(
    (node) => nodeInventories[node.id]?.online,
  ).length;
  const containerStatusLabels: Record<string, string> = {
    running: "运行中",
    stopped: "已停止",
    removed: "已清理",
    provisioning: "创建中",
    merging: "合并中",
    pending_share_approval: "等待共享审批",
    share_rejected: "共享已拒绝",
    share_uncertain: "共享状态待确认",
  };
  const visibleContainers = containers.filter((container) => {
    const query = containerSearch.trim().toLowerCase();
    return (
      (showRemovedContainers || container.status !== "removed") &&
      (containerStatus === "all" || container.status === containerStatus) &&
      (containerNode === "all" ||
        (container.node_id || "__local") === containerNode) &&
      (!query ||
        [
          container.name,
          container.owner_username,
          container.node_name,
          container.node_id,
          container.access_host,
          String(container.id),
        ].some((value) => value?.toLowerCase().includes(query)))
    );
  });
  // 保留已选但当前无记录的选项，避免下拉框显示与实际筛选条件不一致。
  const containerStatuses = Array.from(
    new Set([
      ...containers
        .filter(
          (container) =>
            showRemovedContainers || container.status !== "removed",
        )
        .map((container) => container.status),
      ...(containerStatus === "all" ? [] : [containerStatus]),
    ]),
  );
  const containerNodeMap = new Map(
    containers.map((container) => [
      container.node_id || "__local",
      container.node_name || container.node_id || "本机",
    ]),
  );
  if (containerNode !== "all" && !containerNodeMap.has(containerNode)) {
    containerNodeMap.set(
      containerNode,
      containerNode === "__local" ? "本机" : containerNode,
    );
  }
  const containerNodes = Array.from(containerNodeMap.entries());
  const modules = [
    {
      id: "users",
      title: "用户管理",
      description: "账号、审批与个人配额",
      icon: UsersRound,
    },
    {
      id: "containers",
      title: "容器运维",
      description: "运行状态与生命周期",
      icon: Boxes,
    },
    ...(isMaster
      ? [
          {
            id: "nodes",
            title: "计算节点",
            description: "连接健康与调度",
            icon: Server,
          },
        ]
      : []),
    {
      id: "policies",
      title: "资源策略",
      description: "内存、共享与闲置回收",
      icon: SlidersHorizontal,
    },
  ] as Array<{
    id: typeof activeModule;
    title: string;
    description: string;
    icon: typeof UsersRound;
  }>;

  return (
    <div className="admin-page admin-workspace">
      <Toast toasts={toasts} remove={removeToast} />
      <ConfirmDialog state={confirm} onCancel={closeConfirm} />
      <header className="admin-workspace-header">
        <div>
          <span className="admin-eyebrow">ADMIN WORKSPACE</span>
          <h1>管理中心</h1>
          <p>集中管理用户、计算资源与运行策略。</p>
        </div>
        <span className="admin-access-badge">
          <ShieldCheck size={15} aria-hidden="true" />
          管理员工作台
        </span>
      </header>
      <div className="admin-overview" aria-label="管理摘要">
        <div>
          <UsersRound size={20} aria-hidden="true" />
          <span>
            用户总数
            <strong>{usersLoading || usersError ? "—" : users.length}</strong>
          </span>
          <small>账号与个人配额</small>
        </div>
        <div className={pendingUsers.length ? "needs-attention" : ""}>
          <UserCheck size={20} aria-hidden="true" />
          <span>
            待审批
            <strong>
              {pendingLoading || pendingError ? "—" : pendingUsers.length}
            </strong>
          </span>
          <small>新用户注册申请</small>
        </div>
        <div>
          <Activity size={20} aria-hidden="true" />
          <span>
            运行中容器
            <strong>
              {containersLoading || containersError ? "—" : runningContainers}
            </strong>
          </span>
          <small>当前资源使用</small>
        </div>
        <div>
          <Server size={20} aria-hidden="true" />
          <span>
            {isMaster ? "在线节点" : "已停止容器"}
            <strong>
              {isMaster
                ? nodesLoading || nodesError
                  ? "—"
                  : `${onlineNodes} / ${nodes.length}`
                : containersLoading || containersError
                  ? "—"
                  : containers.filter(
                      (container) => container.status === "stopped",
                    ).length}
            </strong>
          </span>
          <small>{isMaster ? "连接健康快照" : "可清理的运行记录"}</small>
        </div>
      </div>
      <nav className="admin-module-nav" aria-label="管理中心模块">
        {modules.map(({ id, title, description, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className={activeModule === id ? "active" : ""}
            aria-pressed={activeModule === id}
            aria-controls={`admin-${id}`}
            onClick={() => setActiveModule(id)}
          >
            <Icon size={20} aria-hidden="true" />
            <span>
              <strong>{title}</strong>
              <small>{description}</small>
            </span>
            <ChevronRight size={16} aria-hidden="true" />
          </button>
        ))}
      </nav>
      <div className="admin-module-content">
        <div id="admin-nodes" hidden={activeModule !== "nodes" || !isMaster}>
          {isMaster && (
            <section className="admin-section">
              <div className="admin-module-heading">
                <div>
                  <h2>计算节点</h2>
                  <p>查看节点资源与连接健康，控制节点是否参与调度。</p>
                </div>
                <div className="admin-heading-actions">
                  <button
                    type="button"
                    className="btn btn-frosted btn-sm"
                    disabled={nodesLoading}
                    onClick={() => void loadNodes()}
                  >
                    <RefreshCw size={15} aria-hidden="true" />
                    {nodesLoading ? "刷新中…" : "刷新"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    aria-expanded={nodeFormOpen}
                    aria-controls="admin-node-form"
                    disabled={nodeBusy}
                    onClick={() => setNodeFormOpen(!nodeFormOpen)}
                  >
                    <Plus size={15} aria-hidden="true" />
                    {nodeFormOpen
                      ? "收起表单"
                      : editingNodeId
                        ? "继续编辑"
                        : "添加节点"}
                  </button>
                </div>
              </div>
              {nodesError && (
                <div className="admin-load-error" role="alert">
                  {nodesError}，请点击刷新重试。
                </div>
              )}
              <form
                id="admin-node-form"
                ref={nodeFormRef}
                hidden={!nodeFormOpen}
                onSubmit={handleSaveNode}
                className="node-form"
              >
                <h3 className="admin-form-title">
                  {editingNodeId
                    ? `编辑节点 · ${editingNodeId}`
                    : "添加计算节点"}
                </h3>
                <label className="admin-node-field">
                  <span>节点 ID</span>
                  <input
                    placeholder="唯一节点标识"
                    value={nodeDraft.id}
                    disabled={Boolean(editingNodeId) || nodeBusy}
                    onChange={(e) =>
                      setNodeDraft((draft) => ({
                        ...draft,
                        id: e.target.value,
                      }))
                    }
                    required
                  />
                </label>
                <label className="admin-node-field">
                  <span>节点名称</span>
                  <input
                    placeholder="例如：GPU 工作节点 01"
                    value={nodeDraft.name}
                    disabled={nodeBusy}
                    onChange={(e) =>
                      setNodeDraft((draft) => ({
                        ...draft,
                        name: e.target.value,
                      }))
                    }
                    required
                  />
                </label>
                <label className="admin-node-field">
                  <span>Agent 地址</span>
                  <input
                    placeholder="http://10.0.0.2:9000"
                    value={nodeDraft.base_url}
                    disabled={nodeBusy}
                    onChange={(e) =>
                      setNodeDraft((draft) => ({
                        ...draft,
                        base_url: e.target.value,
                      }))
                    }
                  />
                </label>
                <label className="admin-node-field">
                  <span>公开访问主机名 / IP</span>
                  <input
                    placeholder="用户连接容器的访问地址"
                    value={nodeDraft.public_host}
                    disabled={nodeBusy}
                    onChange={(e) =>
                      setNodeDraft((draft) => ({
                        ...draft,
                        public_host: e.target.value,
                      }))
                    }
                  />
                </label>
                <label className="admin-node-field admin-node-token">
                  <span>Agent 令牌</span>
                  <input
                    type="password"
                    autoComplete="new-password"
                    placeholder={
                      editingNodeId ? "留空保留原令牌" : "填写节点的 Agent 令牌"
                    }
                    value={nodeDraft.agent_token}
                    disabled={nodeBusy}
                    onChange={(e) =>
                      setNodeDraft((draft) => ({
                        ...draft,
                        agent_token: e.target.value,
                      }))
                    }
                  />
                </label>
                <label className="node-check">
                  <input
                    type="checkbox"
                    disabled={nodeBusy}
                    checked={nodeDraft.enabled}
                    onChange={(e) =>
                      setNodeDraft((draft) => ({
                        ...draft,
                        enabled: e.target.checked,
                      }))
                    }
                  />
                  启用
                </label>
                <label className="node-check">
                  <input
                    type="checkbox"
                    disabled={nodeBusy}
                    checked={nodeDraft.schedulable}
                    onChange={(e) =>
                      setNodeDraft((draft) => ({
                        ...draft,
                        schedulable: e.target.checked,
                      }))
                    }
                  />
                  可调度
                </label>
                <div className="node-form-actions">
                  <button
                    type="submit"
                    className="btn btn-primary"
                    disabled={nodeBusy}
                  >
                    {nodeSaving
                      ? "保存中…"
                      : editingNodeId
                        ? "保存修改"
                        : "添加节点"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-frosted"
                    disabled={nodeBusy}
                    onClick={() => {
                      setEditingNodeId(null);
                      setNodeDraft(emptyNodeDraft);
                      setNodeFormOpen(false);
                    }}
                  >
                    取消并清空
                  </button>
                </div>
              </form>
              {nodesLoading && (
                <p className="admin-loading-note" role="status">
                  正在获取节点与资源快照…
                </p>
              )}
              {nodes.length === 0 ? (
                !nodesLoading &&
                !nodesError && (
                  <p className="admin-empty">
                    暂无计算节点，添加节点后即可管理调度与连接。
                  </p>
                )
              ) : (
                <div className="node-grid">
                  {nodes.map((node) => {
                    const result = nodeInventories[node.id];
                    const inventory = result?.inventory;
                    return (
                      <article className="node-card" key={node.id}>
                        <div className="node-card-header">
                          <div>
                            <strong>{node.name}</strong>
                            <code>{node.id}</code>
                          </div>
                          <span
                            className={`node-online ${result?.online ? "online" : "offline"}`}
                          >
                            {result?.online
                              ? "在线"
                              : result
                                ? "离线"
                                : node.enabled
                                  ? "未检测"
                                  : "已禁用"}
                          </span>
                        </div>
                        <dl className="node-meta">
                          <div>
                            <dt>Agent</dt>
                            <dd>
                              {node.is_local ? "本机" : node.base_url || "-"}
                            </dd>
                          </div>
                          <div>
                            <dt>访问地址</dt>
                            <dd>{node.public_host || "-"}</dd>
                          </div>
                          <div>
                            <dt>最近连接</dt>
                            <dd>
                              {node.last_seen_at
                                ? new Date(node.last_seen_at).toLocaleString()
                                : "-"}
                            </dd>
                          </div>
                        </dl>
                        <div className="node-resource-summary">
                          <span>
                            GPU <b>{inventory?.gpus.length ?? "-"}</b>
                          </span>
                          <span>
                            容器 <b>{inventory?.containers.length ?? "-"}</b>
                          </span>
                          <span>
                            CPU{" "}
                            <b>
                              {inventory
                                ? `${inventory.system_load.cpu_percent}%`
                                : "-"}
                            </b>
                          </span>
                          <span>
                            内存{" "}
                            <b>
                              {inventory
                                ? `${inventory.system_load.memory_used_gb}/${inventory.system_load.memory_total_gb} GB`
                                : "-"}
                            </b>
                          </span>
                        </div>
                        {result?.error && (
                          <div className="node-error">{result.error}</div>
                        )}
                        {inventory?.gpus.length ? (
                          <div className="node-gpu-list">
                            {inventory.gpus.map((gpu) => (
                              <span key={gpu.index}>
                                GPU {gpu.index} · {gpu.name} ·{" "}
                                {gpu.memory_percent ?? 0}%
                              </span>
                            ))}
                          </div>
                        ) : null}
                        <div className="node-controls">
                          <label>
                            <input
                              type="checkbox"
                              disabled={
                                nodeBusy ||
                                (nodeFormOpen && editingNodeId === node.id)
                              }
                              checked={node.enabled}
                              onChange={(e) =>
                                handleToggleNode(
                                  node,
                                  "enabled",
                                  e.target.checked,
                                )
                              }
                            />
                            启用
                          </label>
                          <label>
                            <input
                              type="checkbox"
                              disabled={
                                nodeBusy ||
                                (nodeFormOpen && editingNodeId === node.id)
                              }
                              checked={node.schedulable}
                              onChange={(e) =>
                                handleToggleNode(
                                  node,
                                  "schedulable",
                                  e.target.checked,
                                )
                              }
                            />
                            可调度
                          </label>
                          <button
                            type="button"
                            className="btn btn-small"
                            onClick={() => handleTestNode(node.id)}
                            disabled={nodeBusy}
                          >
                            {nodeTesting === node.id ? "测试中" : "连接测试"}
                          </button>
                          <button
                            type="button"
                            className="btn btn-small"
                            disabled={nodeBusy}
                            onClick={() => handleEditNode(node)}
                          >
                            编辑
                          </button>
                          {!node.is_local && (
                            <button
                              type="button"
                              className="btn btn-small btn-danger"
                              disabled={nodeBusy}
                              onClick={() => handleDeleteNode(node)}
                            >
                              删除
                            </button>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
            </section>
          )}
        </div>

        <div id="admin-policies" hidden={activeModule !== "policies"}>
          <section className="admin-section">
            <div className="admin-module-heading">
              <div>
                <h2>资源策略</h2>
                <p>配置全局分配规则。个人磁盘和 GPU 上限请在用户管理中调整。</p>
              </div>
              <span
                className={`admin-draft-badge ${settingsDirty ? "dirty" : ""}`}
              >
                {!settingsLoaded
                  ? "尚未加载"
                  : settingsDirty
                    ? "有未保存的更改"
                    : "已同步"}
              </span>
            </div>
            {!settingsLoaded && !settingsError && (
              <p className="admin-loading-note" role="status">
                正在加载全局策略…
              </p>
            )}
            {settingsError && (
              <div className="admin-load-error" role="alert">
                {settingsError}{" "}
                <button
                  type="button"
                  className="btn btn-frosted btn-sm"
                  onClick={() => void loadSettings()}
                >
                  重新加载
                </button>
              </div>
            )}
            <form onSubmit={handleSaveSettings} className="admin-settings-grid">
              <fieldset
                className="admin-policy-fields"
                aria-label="全局资源策略"
                disabled={!settingsLoaded || settingsSaving}
              >
                <div className="setting-group-title admin-policy-title">
                  <SlidersHorizontal size={18} aria-hidden="true" />
                  <div>
                    内存与 GPU 共享<span>统一的容器资源分配标准</span>
                  </div>
                </div>
                <div className="setting-item">
                  <label htmlFor="admin-cpu-memory">
                    <strong>CPU 容器内存配额</strong>
                    <span className="setting-desc">
                      每个纯 CPU 容器可使用的最大内存
                    </span>
                  </label>
                  <div className="setting-input-wrap">
                    <input
                      type="number"
                      min={1}
                      max={512}
                      id="admin-cpu-memory"
                      required
                      value={settingsDraft.cpu_mem_gb}
                      onChange={(e) =>
                        setSettingsDraft((s) => ({
                          ...s,
                          cpu_mem_gb: Number(e.target.value),
                        }))
                      }
                    />
                    <span>GB</span>
                  </div>
                </div>
                <div className="setting-item">
                  <label htmlFor="admin-gpu-memory">
                    <strong>GPU 容器每卡内存配额</strong>
                    <span className="setting-desc">
                      每选 1 张 GPU 分配的内存量（总量 = 张数 × 此值）
                    </span>
                  </label>
                  <div className="setting-input-wrap">
                    <input
                      type="number"
                      min={1}
                      max={512}
                      id="admin-gpu-memory"
                      required
                      value={settingsDraft.gpu_mem_gb_per_gpu}
                      onChange={(e) =>
                        setSettingsDraft((s) => ({
                          ...s,
                          gpu_mem_gb_per_gpu: Number(e.target.value),
                        }))
                      }
                    />
                    <span>GB / 卡</span>
                  </div>
                </div>
                <div className="setting-item setting-item-preview">
                  <label>
                    <strong>示例预览</strong>
                    <span className="setting-desc">
                      当前配置下申请场景的内存分配
                    </span>
                  </label>
                  <div className="setting-preview">
                    <div>
                      纯 CPU 容器 → <b>{settingsDraft.cpu_mem_gb} GB</b>
                    </div>
                    <div>
                      选 1 张 GPU → <b>{settingsDraft.gpu_mem_gb_per_gpu} GB</b>
                    </div>
                    <div>
                      选 2 张 GPU →{" "}
                      <b>{settingsDraft.gpu_mem_gb_per_gpu * 2} GB</b>
                    </div>
                    <div>
                      选 4 张 GPU →{" "}
                      <b>{settingsDraft.gpu_mem_gb_per_gpu * 4} GB</b>
                    </div>
                  </div>
                </div>
                <div className="setting-item">
                  <label htmlFor="admin-gpu-sharing">
                    <strong>单卡最多共用人数</strong>
                    <span className="setting-desc">
                      同一块 GPU 上允许并行使用的不同用户上限
                    </span>
                  </label>
                  <div className="setting-input-wrap">
                    <input
                      type="number"
                      min={1}
                      max={20}
                      id="admin-gpu-sharing"
                      required
                      value={settingsDraft.max_gpu_sharing_users}
                      onChange={(e) =>
                        setSettingsDraft((s) => ({
                          ...s,
                          max_gpu_sharing_users: Number(e.target.value),
                        }))
                      }
                    />
                    <span>人 / 卡</span>
                  </div>
                </div>
                <div className="setting-group-title admin-policy-title">
                  <Activity size={18} aria-hidden="true" />
                  <div>
                    GPU 闲置回收
                    <span>按条件与持续时间判断，减少长期闲置占用</span>
                  </div>
                </div>
                <div className="setting-item setting-item-wide">
                  <label className="setting-toggle-row">
                    <span>
                      <strong>启用自动回收</strong>
                    </span>
                    <input
                      type="checkbox"
                      checked={settingsDraft.idle_gpu_reclaim_enabled}
                      onChange={(e) =>
                        setSettingsDraft((s) => ({
                          ...s,
                          idle_gpu_reclaim_enabled: e.target.checked,
                        }))
                      }
                    />
                  </label>
                </div>
                <div className="setting-item">
                  <label className="setting-toggle-row">
                    <strong>双低分支风控判断</strong>
                    <input
                      type="checkbox"
                      disabled={!settingsDraft.idle_gpu_reclaim_enabled}
                      checked={settingsDraft.idle_gpu_dual_low_enabled}
                      onChange={(e) =>
                        setSettingsDraft((s) => ({
                          ...s,
                          idle_gpu_dual_low_enabled: e.target.checked,
                        }))
                      }
                    />
                  </label>
                </div>
                <div className="setting-item">
                  <label className="setting-toggle-row">
                    <strong>显存不变分支风控判断</strong>
                    <input
                      type="checkbox"
                      disabled={!settingsDraft.idle_gpu_reclaim_enabled}
                      checked={settingsDraft.idle_gpu_memory_unchanged_enabled}
                      onChange={(e) =>
                        setSettingsDraft((s) => ({
                          ...s,
                          idle_gpu_memory_unchanged_enabled: e.target.checked,
                        }))
                      }
                    />
                  </label>
                </div>
                <div className="setting-item">
                  <label className="setting-toggle-row">
                    <strong>部分 GPU 长期闲置自动缩卡</strong>
                    <input
                      type="checkbox"
                      disabled={!settingsDraft.idle_gpu_reclaim_enabled}
                      checked={settingsDraft.idle_gpu_shrink_enabled}
                      onChange={(e) =>
                        setSettingsDraft((s) => ({
                          ...s,
                          idle_gpu_shrink_enabled: e.target.checked,
                        }))
                      }
                    />
                  </label>
                </div>
                <div className="setting-item-wide idle-reclaim-conditions">
                  <div className="setting-item">
                    <label htmlFor="idle-gpu-util-threshold">
                      <strong>GPU 利用率阈值</strong>
                    </label>
                    <div className="setting-input-wrap">
                      <input
                        id="idle-gpu-util-threshold"
                        type="number"
                        min={0}
                        max={100}
                        disabled={
                          !settingsDraft.idle_gpu_reclaim_enabled ||
                          !settingsDraft.idle_gpu_dual_low_enabled
                        }
                        value={settingsDraft.idle_gpu_util_threshold_percent}
                        onChange={(e) =>
                          setSettingsDraft((s) => ({
                            ...s,
                            idle_gpu_util_threshold_percent: Number(
                              e.target.value,
                            ),
                          }))
                        }
                      />
                      <span>%</span>
                    </div>
                  </div>
                  <span className="idle-condition-and" aria-label="且">
                    &amp;
                  </span>
                  <div className="setting-item">
                    <label htmlFor="idle-gpu-memory-threshold">
                      <strong>显存占用阈值</strong>
                    </label>
                    <div className="setting-input-wrap">
                      <input
                        id="idle-gpu-memory-threshold"
                        type="number"
                        min={0}
                        max={100}
                        disabled={
                          !settingsDraft.idle_gpu_reclaim_enabled ||
                          !settingsDraft.idle_gpu_dual_low_enabled
                        }
                        value={settingsDraft.idle_gpu_memory_threshold_percent}
                        onChange={(e) =>
                          setSettingsDraft((s) => ({
                            ...s,
                            idle_gpu_memory_threshold_percent: Number(
                              e.target.value,
                            ),
                          }))
                        }
                      />
                      <span>%</span>
                    </div>
                  </div>
                  <span className="idle-condition-and" aria-label="且">
                    &amp;
                  </span>
                  <div className="setting-item">
                    <label htmlFor="idle-gpu-duration">
                      <strong>连续低利用时长</strong>
                    </label>
                    <div className="setting-input-wrap">
                      <input
                        id="idle-gpu-duration"
                        type="number"
                        min={1}
                        max={8760}
                        disabled={!settingsDraft.idle_gpu_reclaim_enabled}
                        value={settingsDraft.idle_gpu_duration_hours}
                        onChange={(e) =>
                          setSettingsDraft((s) => ({
                            ...s,
                            idle_gpu_duration_hours: Number(e.target.value),
                          }))
                        }
                      />
                      <span>小时</span>
                    </div>
                  </div>
                </div>
                <div className="setting-group-title admin-policy-title">
                  <ShieldCheck size={18} aria-hidden="true" />
                  <div>
                    信誉分与申请期限策略
                    <span>仅管理员可见；分数越高，申请期限限制越严格</span>
                  </div>
                </div>
                <p className="setting-item-wide admin-reputation-policy-note">
                  分数严格大于阈值时触发对应限制，第二档优先；未触发时沿用最长 7 天。
                  初始分仅用于新用户，不改变已有用户分数。奖励减分最低降至 0。
                </p>
                {reputationFields.map(({ key, label, unit }) => (
                  <div className="setting-item" key={key}>
                    <label htmlFor={`policy-${key}`}><strong>{label}</strong></label>
                    <div className="setting-input-wrap">
                      <input
                        id={`policy-${key}`}
                        type="number"
                        required
                        min={unit === "天" ? 1 : 0}
                        max={unit === "天" ? 7 : 2 ** 31 - 1}
                        step={1}
                        value={Number.isNaN(settingsDraft[key]) ? "" : settingsDraft[key]}
                        onChange={(e) => setSettingsDraft((s) => ({
                          ...s,
                          [key]: e.target.value === "" ? NaN : Number(e.target.value),
                        }))}
                      />
                      <span>{unit}</span>
                    </div>
                  </div>
                ))}
              </fieldset>
              <div className="setting-item setting-item-action">
                <span className="admin-policy-save-note">
                  {!settingsLoaded
                    ? "加载策略后才能保存配置"
                    : settingsDirty
                      ? "切换模块会保留草稿，保存后才会生效"
                      : "所有策略已同步"}
                </span>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={settingsSaving || !settingsLoaded || !settingsDirty}
                >
                  {settingsSaving ? "保存中…" : "保存配置"}
                </button>
                <button
                  type="button"
                  className="btn btn-frosted"
                  onClick={() => setSettingsDraft(settings)}
                  disabled={settingsSaving || !settingsLoaded}
                >
                  撤销未保存更改
                </button>
              </div>
            </form>
          </section>
        </div>

        <div id="admin-users" hidden={activeModule !== "users"}>
          <div className="admin-module-heading">
            <div>
              <h2>用户管理</h2>
              <p>审批注册申请、维护账号与个人资源配额。</p>
            </div>
            <div className="admin-heading-actions">
              <button
                type="button"
                className="btn btn-frosted btn-sm"
                disabled={
                  usersLoading ||
                  pendingLoading ||
                  quotaSaving !== null ||
                  gpuQuotaSaving !== null ||
                  reputationSaving !== null ||
                  quotaRefreshing !== null
                }
                onClick={() =>
                  void Promise.all([loadUsers(), loadPendingUsers()])
                }
              >
                <RefreshCw size={15} aria-hidden="true" />
                刷新列表
              </button>
            </div>
          </div>
          <AdminUsersPanel
            users={users}
            pendingUsers={pendingUsers}
            newUser={newUser}
            setNewUser={setNewUser}
            loading={loading}
            createError={createError}
            quotaDrafts={quotaDrafts}
            setQuotaDrafts={setQuotaDrafts}
            gpuQuotaDrafts={gpuQuotaDrafts}
            setGpuQuotaDrafts={setGpuQuotaDrafts}
            quotaSaving={quotaSaving}
            gpuQuotaSaving={gpuQuotaSaving}
            reputationDrafts={reputationDrafts}
            setReputationDrafts={setReputationDrafts}
            reputationSaving={reputationSaving}
            onSaveReputation={handleSaveReputation}
            quotaRefreshing={quotaRefreshing}
            onCreateUser={handleCreateUser}
            onApprove={handleApprove}
            onDeleteUser={handleDeleteUser}
            onSaveQuota={handleSaveQuota}
            onSaveGpuQuota={handleSaveGpuQuota}
            onRefreshQuota={handleRefreshQuota}
            usersLoading={usersLoading}
            usersError={usersError}
            pendingLoading={pendingLoading}
            pendingError={pendingError}
            onRetryUsers={() => void loadUsers()}
            onRetryPending={() => void loadPendingUsers()}
          />
        </div>

        <div id="admin-containers" hidden={activeModule !== "containers"}>
          <section className="admin-section">
            <div className="admin-module-heading">
              <div>
                <h2>容器运维</h2>
                <p>按用户、节点和状态查找容器，停止或清理前将再次确认。</p>
              </div>
              <div className="admin-heading-actions">
                <button
                  type="button"
                  className="btn btn-frosted btn-sm"
                  disabled={containersLoading}
                  onClick={() => void loadContainers()}
                >
                  <RefreshCw size={15} aria-hidden="true" />
                  {containersLoading ? "刷新中…" : "刷新列表"}
                </button>
              </div>
            </div>
            <div className="admin-container-toolbar">
              <label className="admin-container-search">
                <Search size={17} aria-hidden="true" />
                <input
                  aria-label="搜索容器名称、用户或节点"
                  placeholder="搜索容器、用户或节点…"
                  value={containerSearch}
                  onChange={(e) => setContainerSearch(e.target.value)}
                />
              </label>
              <select
                aria-label="按容器状态筛选"
                value={containerStatus}
                onChange={(e) => setContainerStatus(e.target.value)}
              >
                <option value="all">全部状态</option>
                {containerStatuses.map((status) => (
                  <option key={status} value={status}>
                    {containerStatusLabels[status] || status}
                  </option>
                ))}
              </select>
              <select
                aria-label="按计算节点筛选"
                value={containerNode}
                onChange={(e) => setContainerNode(e.target.value)}
              >
                <option value="all">全部节点</option>
                {containerNodes.map(([id, name]) => (
                  <option key={id} value={id}>
                    {name}
                  </option>
                ))}
              </select>
              <label className="node-check">
                <input
                  type="checkbox"
                  checked={showRemovedContainers}
                  onChange={(e) => {
                    setShowRemovedContainers(e.target.checked);
                    if (!e.target.checked && containerStatus === "removed")
                      setContainerStatus("all");
                  }}
                />
                包含已清理记录
              </label>
            </div>
            <div className="admin-container-results">
              <span>
                {containersLoading
                  ? "正在加载容器…"
                  : containersError
                    ? "列表未同步"
                    : `${visibleContainers.length} 条结果`}
              </span>
              {(containerSearch ||
                containerStatus !== "all" ||
                containerNode !== "all" ||
                showRemovedContainers) && (
                <button
                  type="button"
                  onClick={() => {
                    setContainerSearch("");
                    setContainerStatus("all");
                    setContainerNode("all");
                    setShowRemovedContainers(false);
                  }}
                >
                  重置筛选
                </button>
              )}
            </div>
            {containersError && (
              <div className="admin-load-error" role="alert">
                {containersError}，请点击刷新列表重试。
              </div>
            )}
            {!containersLoading &&
              !containersError &&
              visibleContainers.length === 0 && (
                <p className="admin-empty">
                  {containers.length === 0
                    ? "暂无容器记录"
                    : "没有符合当前筛选条件的容器"}
                </p>
              )}
            {visibleContainers.length > 0 && (
              <div className="admin-table-wrap">
                <table
                  className="admin-table admin-container-table"
                  aria-label="容器运行与管理列表"
                  aria-busy={containersLoading}
                >
                  <thead>
                    <tr>
                      <th scope="col">容器 / 节点</th>
                      <th scope="col">资源</th>
                      <th scope="col">连接与端口</th>
                      <th scope="col">状态</th>
                      <th scope="col">用户</th>
                      <th scope="col">到期时间</th>
                      <th scope="col">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleContainers.map((container) => (
                      <tr key={container.id}>
                        <td className="admin-container-name" data-label="容器">
                          <div>
                            <strong>{container.name}</strong>
                            <small>
                              {container.node_name ||
                                container.node_id ||
                                "本机"}{" "}
                              · #{container.id}
                            </small>
                          </div>
                        </td>
                        <td data-label="资源">
                          <div>
                            {container.gpu_ids
                              ? `GPU ${container.gpu_ids}`
                              : "CPU"}
                          </div>
                        </td>
                        <td
                          className="admin-container-connection"
                          data-label="连接"
                        >
                          <div>
                            {container.access_host
                              ? `${container.access_host}:${container.ssh_port}`
                              : `SSH ${container.ssh_port}`}
                            <small>
                              服务端口：
                              {container.extra_ports &&
                              Object.keys(container.extra_ports).length
                                ? Object.entries(container.extra_ports)
                                    .map(([key, value]) => `${key} → ${value}`)
                                    .join(" · ")
                                : "无"}
                            </small>
                          </div>
                        </td>
                        <td data-label="状态">
                          <div>
                            <span
                              className={`admin-container-state ${container.status}`}
                            >
                              {containerStatusLabels[container.status] ||
                                container.status}
                            </span>
                            {container.stop_reason === "disk_quota" && (
                              <small>空间超限</small>
                            )}
                          </div>
                        </td>
                        <td data-label="用户">
                          <div>{container.owner_username}</div>
                        </td>
                        <td data-label="到期">
                          <div>
                            {new Date(container.expires_at).toLocaleString()}
                          </div>
                        </td>
                        <td data-label="操作">
                          <div className="admin-container-actions">
                            {container.status === "running" && (
                              <button
                                type="button"
                                className="btn btn-small"
                                disabled={
                                  containerActionId !== null ||
                                  containersLoading
                                }
                                aria-label={`停止容器 ${container.name}`}
                                onClick={() => forceStop(container.id)}
                              >
                                {containerActionId === container.id
                                  ? "处理中…"
                                  : "停止"}
                              </button>
                            )}
                            {(container.status === "stopped" ||
                              container.status === "running") && (
                              <button
                                type="button"
                                className="btn btn-small btn-danger"
                                disabled={
                                  containerActionId !== null ||
                                  containersLoading
                                }
                                aria-label={`清理容器 ${container.name}`}
                                onClick={() => forceRemove(container.id)}
                              >
                                {containerActionId === container.id
                                  ? "处理中…"
                                  : "清理"}
                              </button>
                            )}
                            {container.status !== "stopped" &&
                              container.status !== "running" && (
                                <span className="admin-loading-note">—</span>
                              )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
