import { useState, useEffect, useCallback } from "react";
import Editor, { loader } from "@monaco-editor/react";
import { fetcher, fetcherText } from "../api/client";
import "./Workspace.css";

// 预加载 Monaco 编辑器以减少首次打开等待时间
loader.init();

function getLanguage(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    js: "javascript", ts: "typescript", jsx: "javascript", tsx: "typescript",
    py: "python", json: "json", md: "markdown", yml: "yaml", yaml: "yaml",
    sh: "shell", bash: "shell", html: "html", css: "css", vue: "vue",
    env: "plaintext", txt: "plaintext", log: "plaintext", cfg: "ini", ini: "ini",
  };
  return map[ext] ?? "plaintext";
}

interface WorkspaceItem {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
}

interface Container {
  id: number;
  name: string;
  status: string;
  ssh_password?: string | null;
  extra_ports?: Record<string, number> | null;
  service_urls?: Record<string, string> | null;
  access_host?: string | null;
  ssh_host?: string | null;
  node_id?: string | null;
  node_name?: string | null;
}

interface QuotaStatus {
  quota_bytes: number;
  usage_bytes: number;
  over_quota: boolean;
  blocked: boolean;
  quota_exceeded_since?: string | null;
  grace_hours: number;
  grace_deadline?: string | null;
  scan_complete: boolean;
  quota_exempt: boolean;
}

const GIB = 1024 ** 3;

function formatBytes(bytes: number): string {
  if (bytes >= GIB) return `${(bytes / GIB).toFixed(1)} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}

function quotaPercent(quota: QuotaStatus): number {
  if (!quota.quota_bytes) return 0;
  return Math.min(100, quota.usage_bytes / quota.quota_bytes * 100);
}

export default function Workspace() {
  const [currentPath, setCurrentPath] = useState("");
  const [items, setItems] = useState<WorkspaceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [quota, setQuota] = useState<QuotaStatus | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(true);
  const [quotaRefreshing, setQuotaRefreshing] = useState(false);
  const [quotaError, setQuotaError] = useState("");
  const [editPath, setEditPath] = useState<string | null>(null);
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [createType, setCreateType] = useState<"file" | "dir" | null>(null);
  const [createName, setCreateName] = useState("");
  const [deletePath, setDeletePath] = useState<string | null>(null);

  const [containers, setContainers] = useState<Container[]>([]);
  const [showContainerModal, setShowContainerModal] = useState(false);
  const [pendingVSCodePath, setPendingVSCodePath] = useState("");

  const loadList = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const q = currentPath ? `?path=${encodeURIComponent(currentPath)}` : "";
      const data = await fetcher<{ path: string; items: WorkspaceItem[] }>(`/workspace/list${q}`);
      setItems(data.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [currentPath]);

  const refreshQuota = useCallback(async (showError = false) => {
    setQuotaRefreshing(true);
    setQuotaError("");
    try {
      const data = await fetcher<QuotaStatus>("/workspace/usage/refresh", { method: "POST" });
      setQuota(data);
    } catch (e) {
      const message = e instanceof Error ? e.message : "容量统计失败";
      setQuota(null);
      setQuotaError(message);
      if (showError) alert(message);
    } finally {
      setQuotaLoading(false);
      setQuotaRefreshing(false);
    }
  }, []);

  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => { refreshQuota(); }, [refreshQuota]);

  const breadcrumbs = currentPath ? ["", ...currentPath.split("/").filter(Boolean)] : [""];

  const handleOpen = async (item: WorkspaceItem) => {
    if (item.is_dir) {
      setCurrentPath(item.path);
      return;
    }
    setEditPath(item.path);
    setEditContent("");
    try {
      const q = `?path=${encodeURIComponent(item.path)}`;
      const text = await fetcherText(`/workspace/file${q}`);
      setEditContent(text);
    } catch (e) {
      alert(e instanceof Error ? e.message : "无法读取文件（可能为二进制）");
      setEditPath(null);
    }
  };

  const handleSave = async () => {
    if (editPath == null) return;
    setSaving(true);
    try {
      await fetcher(`/workspace/file?path=${encodeURIComponent(editPath)}`, {
        method: "PUT",
        body: JSON.stringify({ content: editContent }),
      });
      setEditPath(null);
      await loadList();
      await refreshQuota();
    } catch (e) {
      alert(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleCreate = async () => {
    const name = createName.trim();
    const type = createType;
    if (!name || !type) return;
    setCreateType(null);
    setCreateName("");
    const newPath = currentPath ? `${currentPath}/${name}` : name;
    try {
      if (type === "dir") {
        await fetcher("/workspace/dir", { method: "POST", body: JSON.stringify({ path: newPath }) });
      } else {
        await fetcher(`/workspace/file?path=${encodeURIComponent(newPath)}`, {
          method: "PUT",
          body: JSON.stringify({ content: "" }),
        });
      }
      await loadList();
      await refreshQuota();
    } catch (e) {
      alert(e instanceof Error ? e.message : "创建失败");
    }
  };

  const handleDelete = async () => {
    if (deletePath == null) return;
    try {
      await fetcher(`/workspace?path=${encodeURIComponent(deletePath)}`, { method: "DELETE" });
      setDeletePath(null);
      const shouldResetPath = currentPath === deletePath || currentPath.startsWith(deletePath + "/");
      if (shouldResetPath) {
        setCurrentPath("");
      } else {
        await loadList();
      }
      await refreshQuota();
    } catch (e) {
      alert(e instanceof Error ? e.message : "删除失败");
    }
  };



  const handleOpenVSCode = async (path: string) => {
    try {
      const data = await fetcher<Container[]>("/containers/my");
      const running = data.filter((container) => container.status === "running" && (container.service_urls?.["8080"] || ((container.access_host || container.ssh_host) && container.extra_ports?.["8080"])));
      if (running.length === 0) {
        alert("没有正在运行的容器（需包含 Code Server）。请先到资源看板申请容器。");
        return;
      }
      if (running.length === 1) {
        openVSCodeWithContainer(running[0], path);
      } else {
        setContainers(running);
        setPendingVSCodePath(path);
        setShowContainerModal(true);
      }
    } catch (e) {
      alert("获取容器列表失败");
    }
  };

  const openVSCodeWithContainer = async (container: Container, path: string) => {
    const host = container.access_host || container.ssh_host;
    const serviceUrl = container.service_urls?.["8080"] || (host && container.extra_ports?.["8080"] ? `http://${host}:${container.extra_ports["8080"]}` : null);
    if (!serviceUrl) return;

    if (container.ssh_password) {
      try {
        await navigator.clipboard.writeText(container.ssh_password);
        alert(`密码 "${container.ssh_password}" 已复制，请在 VS Code 登录时使用`);
      } catch (err) {
        console.error("复制失败:", err);
      }
    }

    const folder = `/workspace/${path.replace(/\/+$/, "")}`;
    const url = new URL(serviceUrl);
    url.searchParams.set("folder", folder);
    window.open(url.toString(), "_blank");
    setShowContainerModal(false);
  };

  return (
    <div className="workspace-page workspace-twin">
      <div className="workspace-bg" aria-hidden />
      <div className="workspace-glow" aria-hidden />
      <div className="workspace-header">
        <h1>工作区文件</h1>
        <div className="workspace-toolbar">
          <button type="button" className="btn btn-frosted btn-sm" onClick={() => handleOpenVSCode(currentPath)}>在 VS Code 中打开</button>
          <button type="button" className="btn btn-frosted btn-sm" onClick={() => { setCreateType("file"); setCreateName(""); }}>新建文件</button>
          <button type="button" className="btn btn-frosted btn-sm" onClick={() => { setCreateType("dir"); setCreateName(""); }}>新建文件夹</button>
        </div>
      </div>
      <section className={`workspace-quota-card ${quota && (!quota.scan_complete || quota.blocked) ? "is-blocked" : ""}`}>
        <div className="workspace-quota-header">
          <div>
            <span className="workspace-quota-label">个人工作区容量</span>
            <strong>
              {quotaLoading && !quota ? "统计中…" : quota ? `${formatBytes(quota.usage_bytes)} / ${formatBytes(quota.quota_bytes)}` : "暂不可用"}
            </strong>
          </div>
          <button type="button" className="btn btn-frosted btn-sm" onClick={() => refreshQuota(true)} disabled={quotaRefreshing}>
            {quotaRefreshing ? "检测中…" : "重新检测"}
          </button>
        </div>
        {quota && !quota.quota_exempt && (
          <>
            <div className="workspace-quota-meter"><span style={{ width: `${quotaPercent(quota)}%` }} /></div>
            {!quota.scan_complete ? (
              <div className="workspace-quota-alert">
                <strong>暂时无法确认工作区容量，已暂停新容器申请</strong>
                <span>存储路径恢复后会自动重新检测，请稍后重试。</span>
              </div>
            ) : quota.over_quota ? (
              <div className="workspace-quota-alert">
                <strong>已超过磁盘配额，当前无法申请新容器</strong>
                <span>请在工作区删除文件；连续超限满 {quota.grace_hours} 小时后，运行中的容器会自动停止。</span>
                {quota.quota_exceeded_since && (
                  <span>本次超限开始于 {new Date(quota.quota_exceeded_since).toLocaleString()}。</span>
                )}
                {quota.grace_deadline && (
                  <span>宽限截止时间：{new Date(quota.grace_deadline).toLocaleString()}。</span>
                )}
              </div>
            ) : (
              <span className="workspace-quota-hint">清理到配额以下后即可正常申请容器。</span>
            )}
          </>
        )}
        {quota?.quota_exempt && <span className="workspace-quota-hint">管理员账号不受个人磁盘配额限制。</span>}
        {quotaError && <span className="workspace-quota-error">{quotaError}</span>}
      </section>
      <div className="workspace-breadcrumb">
        {breadcrumbs.map((part, i) => {
          const path = breadcrumbs.slice(0, i + 1).join("/");
          const label = i === 0 ? "根目录" : part;
          return (
            <span key={path || "root"}>
              <button type="button" className="breadcrumb-link" onClick={() => setCurrentPath(path)}>{label}</button>
              {i < breadcrumbs.length - 1 && <span className="breadcrumb-sep">/</span>}
            </span>
          );
        })}
      </div>
      {loading && <div className="loading">加载中...</div>}
      {error && <div className="workspace-error">{error}</div>}
      {!loading && !error && (
        <div className="workspace-table-wrap">
          <table className="workspace-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>类型</th>
                <th>大小</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 ? (
                <tr><td colSpan={4} className="workspace-empty">当前目录为空</td></tr>
              ) : (
                items.map((item: WorkspaceItem) => (
                  <tr key={item.path}>
                    <td>
                      <button type="button" className="row-name-btn" onClick={() => handleOpen(item)}>
                        {item.is_dir ? "📁 " : "📄 "}{item.name}
                      </button>
                    </td>
                    <td>{item.is_dir ? "目录" : "文件"}</td>
                    <td className="col-mono">{item.size != null ? `${(item.size / 1024).toFixed(1)} KB` : "-"}</td>
                    <td className="col-actions">
                      {item.is_dir && (
                        <button type="button" className="btn btn-frosted btn-sm" onClick={() => handleOpenVSCode(item.path)}>VS Code</button>
                      )}

                      <button type="button" className="btn btn-frosted btn-sm btn-danger" onClick={() => setDeletePath(item.path)}>删除</button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {createType && (
        <div className="modal-overlay" onClick={() => { setCreateType(null); setCreateName(""); }}>
          <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{createType === "dir" ? "新建文件夹" : "新建文件"}</h2>
              <button type="button" className="btn btn-ghost" onClick={() => { setCreateType(null); setCreateName(""); }}>×</button>
            </div>
            <div className="modal-body">
              <input
                type="text"
                className="workspace-input"
                placeholder={createType === "dir" ? "文件夹名" : "文件名"}
                value={createName}
                onChange={(e) => setCreateName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleCreate()}
                autoFocus
              />
            </div>
            <div className="modal-actions">
              <button type="button" className="btn btn-frosted" onClick={() => { setCreateType(null); setCreateName(""); }}>取消</button>
              <button type="button" className="btn btn-primary" onClick={handleCreate} disabled={!createName.trim()}>创建</button>
            </div>
          </div>
        </div>
      )}

      {editPath != null && (
        <div className="modal-overlay" onClick={() => setEditPath(null)}>
          <div className="modal modal-editor" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>编辑: {editPath}</h2>
              <button type="button" className="btn btn-ghost" onClick={() => setEditPath(null)}>×</button>
            </div>
            <div className="modal-body modal-body-editor">
              <Editor
                height="60vh"
                language={editPath ? getLanguage(editPath) : "plaintext"}
                value={editContent}
                onChange={(v) => setEditContent(v ?? "")}
                theme="vs-dark"
                options={{
                  minimap: { enabled: true }, // 既然窗口大了，可以开启小地图
                  fontSize: 14, // 稍微调大字体
                  scrollBeyondLastLine: false,
                  wordWrap: "on",
                  automaticLayout: true, // 解决窗口缩放导致编辑器布局错误的问题
                }}
                loading={<div className="editor-loading">正在下载/加载编辑器模块...</div>}
              />
            </div>
            <div className="modal-actions">
              <button type="button" className="btn btn-frosted" onClick={() => setEditPath(null)}>取消</button>
              <button type="button" className="btn btn-primary" onClick={handleSave} disabled={saving}>{saving ? "保存中…" : "保存"}</button>
            </div>
          </div>
        </div>
      )}

      {deletePath && (
        <div className="modal-overlay" onClick={() => setDeletePath(null)}>
          <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>确认删除</h2>
              <button type="button" className="btn btn-ghost" onClick={() => setDeletePath(null)}>×</button>
            </div>
            <div className="modal-body">
              <p>确定要删除 <code>{deletePath}</code> 吗？</p>
            </div>
            <div className="modal-actions">
              <button type="button" className="btn btn-frosted" onClick={() => setDeletePath(null)}>取消</button>
              <button type="button" className="btn btn-frosted btn-danger" onClick={handleDelete}>删除</button>
            </div>
          </div>
        </div>
      )}
      {showContainerModal && (
        <div className="modal-overlay" onClick={() => setShowContainerModal(false)}>
          <div className="modal modal-sm" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>选择容器</h2>
              <button type="button" className="btn btn-ghost" onClick={() => setShowContainerModal(false)}>×</button>
            </div>
            <div className="modal-body">
              <p style={{ marginBottom: '1rem', opacity: 0.8 }}>请选择用于打开该文件夹的容器：</p>
              <div className="container-select-list">
                {containers.map((c: Container) => (
                  <button
                    key={c.id}
                    type="button"
                    className="btn btn-frosted btn-block"
                    style={{
                      marginBottom: '0.65rem',
                      textAlign: 'left',
                      display: 'block',
                      width: '100%',
                      padding: '0.75rem 1rem'
                    }}
                    onClick={() => openVSCodeWithContainer(c, pendingVSCodePath)}
                  >
                    <span style={{ fontSize: '1.1rem', marginRight: '0.5rem' }}>🚀</span>
                    <strong>{c.name}</strong>
                    <span style={{ marginLeft: 'auto', opacity: 0.6, fontSize: '0.8rem', float: 'right' }}>{c.node_name || c.node_id || "本机"}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="modal-actions">
              <button type="button" className="btn btn-frosted" onClick={() => setShowContainerModal(false)}>取消</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
