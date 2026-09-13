import { useState, useEffect } from "react";
import { fetcher } from "../api/client";
import "./MyContainers.css";

const PORT_LABELS: Record<string, string> = { 8888: "Jupyter", 6006: "TensorBoard", 8080: "Code Server" };

type ToastType = "success" | "error" | "info";
interface ToastMsg { id: number; msg: string; type: ToastType; }
let _tid = 0;

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

interface ConfirmState { visible: boolean; message: string; onConfirm: () => void; }
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
          >确认</button>
        </div>
      </div>
    </div>
  );
}

interface Container {
  id: number;
  name: string;
  container_id: string | null;
  gpu_ids: string;
  ssh_port: number;
  ssh_password?: string | null;
  extra_ports?: Record<string, number> | null;
  status: string;
  stop_reason?: string | null;
  expires_at: string;
  owner_username: string;
  created_at: string;
  share_approvers?: { user_id: number; username: string; approved: boolean; approved_at?: string | null }[] | null;
  pending_lease_days?: number | null;
  node_id?: string | null;
  node_name?: string | null;
  access_host?: string | null;
  ssh_host?: string | null;
  ssh_url?: string | null;
  service_urls?: Record<string, string> | null;
}

async function copyAndFeedback(text: string, button: HTMLButtonElement) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      // 降级方案：针对非 HTTPS 环境 (如通过 IP 访问)
      const textArea = document.createElement("textarea");
      textArea.value = text;
      textArea.style.position = "fixed";
      textArea.style.left = "-9999px";
      textArea.style.top = "0";
      document.body.appendChild(textArea);
      textArea.focus();
      textArea.select();
      const successful = document.execCommand('copy');
      document.body.removeChild(textArea);
      if (!successful) throw new Error("Fallback copy failed");
    }
    const orig = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => { button.textContent = orig; }, 800);
  } catch (err) {
    console.error("复制失败:", err);
    const orig = button.textContent;
    button.textContent = "复制失败";
    setTimeout(() => { button.textContent = orig; }, 1500);
  }
}

export default function MyContainers() {
  const [containers, setContainers] = useState<Container[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [renewing, setRenewing] = useState<number | null>(null);

  const [toasts, setToasts] = useState<ToastMsg[]>([]);
  const pushToast = (msg: string, type: ToastType = "info") => {
    const id = ++_tid;
    setToasts(prev => [...prev, { id, msg, type }]);
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 3500);
  };
  const removeToast = (id: number) => setToasts(prev => prev.filter(t => t.id !== id));

  const [confirm, setConfirm] = useState<ConfirmState>({ visible: false, message: "", onConfirm: () => { } });
  const askConfirm = (message: string, onConfirm: () => void) =>
    setConfirm({ visible: true, message, onConfirm });
  const closeConfirm = () => setConfirm(s => ({ ...s, visible: false }));

  const load = async () => {
    try {
      const data = await fetcher<Container[]>("/containers/my");
      setContainers(data);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const canRenew = (c: Container) => {
    if (c.status !== "running") return false;
    const delta = (new Date(c.expires_at).getTime() - Date.now()) / (1000 * 3600);
    return delta > 0 && delta <= 24;
  };

  const handleRenew = async (id: number) => {
    setRenewing(id);
    try {
      await fetcher(`/leases/renew/${id}`, { method: "POST", body: JSON.stringify({ lease_days: 3 }) });
      await load();
      pushToast("续租成功！已延长 3 天", "success");
    } catch (e) {
      pushToast(e instanceof Error ? e.message : "续租失败", "error");
    } finally {
      setRenewing(null);
    }
  };

  const handleDelete = (id: number, name: string) => {
    askConfirm(
      `确定要提前停止并销毁容器 "${name}" 吗？此操作不可逆，容器内未保存到 /workspace 的数据将丢失！`,
      async () => {
        try {
          await fetcher(`/containers/${id}`, { method: "DELETE" });
          await load();
          pushToast("容器已销毁", "success");
        } catch (e) {
          pushToast(e instanceof Error ? e.message : "销毁失败", "error");
        }
      }
    );
  };

  const statusText = (container: Container) => {
    if (container.status === "running") return "运行中";
    if (container.status === "pending_share_approval") return "待使用者同意";
    if (container.status === "share_rejected") return "共用申请被拒绝";
    if (container.status === "stopped" && container.stop_reason === "disk_quota") return "空间超限停止";
    if (container.status === "stopped") return "已停止";
    return "已清理";
  };

  if (loading) return <div className="loading">加载中...</div>;
  if (error) return <div className="my-error">加载失败: {error}</div>;

  return (
    <div className="my-containers my-containers-twin">
      <Toast toasts={toasts} remove={removeToast} />
      <ConfirmDialog state={confirm} onCancel={closeConfirm} />
      <div className="my-containers-bg" aria-hidden />
      <div className="my-containers-glow" aria-hidden />
      <h1>我的容器</h1>
      {containers.length === 0 ? (
        <p className="empty">暂无容器，请到资源看板申请</p>
      ) : (
        <div className="container-table-wrap">
          <table className="container-table">
            <thead>
              <tr>
                <th>容器名</th>
                <th>节点</th>
                <th>状态</th>
                <th>类型</th>
                <th>SSH 地址</th>
                <th>服务端口</th>
                <th>到期时间</th>
                <th>SSH 密码</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {containers.map((c) => (
                <tr key={c.id} className={`status-${c.status}`}>
                  <td className="col-name">{c.name}</td>
                  <td>{c.node_name || c.node_id || "本机"}</td>
                  <td>
                    <span className={`status-badge ${c.status} ${c.stop_reason === "disk_quota" ? "quota-stopped" : ""}`}>
                      {statusText(c)}
                    </span>
                    {c.stop_reason === "disk_quota" && (
                      <small className="quota-stop-hint">请清理工作区后再申请</small>
                    )}
                  </td>
                  <td>{c.gpu_ids ? `GPU ${c.gpu_ids}` : "纯 CPU"}</td>
                  <td className="col-mono">{c.ssh_host || c.access_host ? `${c.ssh_host || c.access_host}:${c.ssh_port}` : "-"}</td>
                  <td className="col-ports">
                    {c.service_urls && Object.keys(c.service_urls).length > 0
                      ? Object.entries(c.service_urls).map(([port, url]) => (
                        <a key={port} className="port-item" href={url} target="_blank" rel="noreferrer">{PORT_LABELS[port] || port}</a>
                      ))
                      : c.extra_ports && Object.keys(c.extra_ports).length > 0
                        ? Object.entries(c.extra_ports).map(([port, hostPort]) => <span key={port} className="port-item">{PORT_LABELS[port] || port}:{hostPort}</span>)
                        : "-"}
                  </td>
                  <td className="col-mono col-date">{new Date(c.expires_at).toLocaleString()}</td>
                  <td>
                    {c.ssh_password ? (
                      <span className="ssh-cell">
                        <code>{c.ssh_password}</code>
                        <button
                          type="button"
                          className="btn btn-frosted btn-copy"
                          onClick={(e) => copyAndFeedback(c.ssh_password!, e.currentTarget)}
                        >
                          复制
                        </button>
                      </span>
                    ) : "-"}
                  </td>
                  <td className="col-actions">
                    {c.status === "pending_share_approval" && (
                      <div style={{ minWidth: 220, textAlign: "left", lineHeight: 1.7 }}>
                        <div><strong>审批进度：</strong></div>
                        {(c.share_approvers && c.share_approvers.length > 0) ? (
                          c.share_approvers.map((a) => (
                            <div key={`${c.id}-${a.user_id}`}>
                              {a.username}：{a.approved ? "已同意" : "待同意"}
                            </div>
                          ))
                        ) : (
                          <div>暂无审批人信息</div>
                        )}
                      </div>
                    )}
                    {c.status === "running" && (c.service_urls?.["8080"] || ((c.access_host || c.ssh_host) && c.extra_ports?.["8080"])) && (
                      <button
                        type="button"
                        className="btn btn-frosted btn-sm"
                        onClick={async (e: React.MouseEvent<HTMLButtonElement>) => {
                          if (c.ssh_password) {
                            await copyAndFeedback(c.ssh_password, e.currentTarget);
                            pushToast(`密码 "${c.ssh_password}" 已复制，请在 VS Code 登录时使用`, "info");
                          }
                          const host = c.access_host || c.ssh_host;
                          const url = c.service_urls?.["8080"] || (host && c.extra_ports?.["8080"] ? `http://${host}:${c.extra_ports["8080"]}` : null);
                          if (url) window.open(url, "_blank");
                        }}
                      >
                        打开 Code
                      </button>
                    )}
                    {c.status === "running" && (c.ssh_host || c.access_host) && (
                      <button
                        type="button"
                        className="btn btn-frosted btn-sm"
                        onClick={(e) => copyAndFeedback(`ssh -p ${c.ssh_port} root@${c.ssh_host || c.access_host}`, e.currentTarget)}
                      >
                        复制 SSH
                      </button>
                    )}
                    {canRenew(c) && (
                      <button
                        className="btn btn-frosted btn-sm renew-btn"
                        onClick={() => handleRenew(c.id)}
                        disabled={renewing === c.id}
                      >
                        {renewing === c.id ? "续租中…" : "续租 3 天"}
                      </button>
                    )}
                    <button
                      className="btn btn-frosted btn-sm btn-danger-soft"
                      onClick={() => handleDelete(c.id, c.name)}
                      style={{ marginLeft: "0.5rem" }}
                    >
                      停止并销毁
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
