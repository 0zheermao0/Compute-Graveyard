import { useMemo, useState } from "react";
import { fetcher } from "../api/client";
import type { DashboardNode } from "../pages/Dashboard";
import "./ApplyModal.css";

const SERVICE_LABELS: Record<string, string> = { 8888: "Jupyter", 6006: "TensorBoard", 8080: "Code Server" };

type PlacementMode = "auto" | "local" | "specific";

export interface GpuSharingRow {
  gpu_index: number;
  occupant_count: number;
  max_sharing: number;
  selectable: boolean;
}

interface ApplyModalProps {
  gpuSharing: GpuSharingRow[];
  nodes: DashboardNode[];
  onClose: () => void;
  onSuccess: () => void;
}

interface CreatedContainer {
  ssh_port: number;
  ssh_password: string;
  ssh_host?: string | null;
  access_host?: string | null;
  node_name?: string | null;
  extra_ports?: Record<string, number>;
  service_urls?: Record<string, string>;
}

interface ApplyApiContainer {
  ssh_port: number;
  ssh_password?: string | null;
  ssh_host?: string | null;
  access_host?: string | null;
  node_name?: string | null;
  extra_ports?: Record<string, number> | null;
  service_urls?: Record<string, string> | null;
}

interface ApplyApiResult {
  container: ApplyApiContainer;
  pending_share_approval: boolean;
  message?: string | null;
}

interface GpuChoice {
  index: number;
  label: string;
  detail: string;
  selectable: boolean;
}

export default function ApplyModal({ gpuSharing, nodes, onClose, onSuccess }: ApplyModalProps) {
  const localNode = nodes.find((node) => node.is_local);
  const defaultMode: PlacementMode = nodes.length > 1 ? "auto" : localNode ? "local" : "specific";
  const [cpuOnly, setCpuOnly] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [leaseDays, setLeaseDays] = useState(3);
  const [placementMode, setPlacementMode] = useState<PlacementMode>(defaultMode);
  const [nodeId, setNodeId] = useState(nodes.find((node) => node.online && node.schedulable)?.node_id ?? "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<CreatedContainer | null>(null);
  const [pendingInfo, setPendingInfo] = useState<string | null>(null);

  const gpuChoices = useMemo<GpuChoice[]>(() => {
    if (placementMode === "local") {
      const rows = localNode?.gpu_sharing?.length ? localNode.gpu_sharing : gpuSharing;
      return rows.map((row) => ({
        index: row.gpu_index,
        label: `GPU ${row.gpu_index}`,
        detail: row.occupant_count === 0 ? "空闲" : `占用 ${row.occupant_count} 人 / 上限 ${row.max_sharing}`,
        selectable: Boolean(localNode?.online && localNode.schedulable && row.selectable),
      }));
    }
    if (placementMode === "specific") {
      const node = nodes.find((item) => item.node_id === nodeId);
      const sharing = new Map((node?.gpu_sharing ?? []).map((row) => [row.gpu_index, row]));
      return (node?.gpus ?? []).map((gpu) => {
        const status = sharing.get(gpu.index);
        return {
          index: gpu.index,
          label: `GPU ${gpu.index} · ${gpu.name}`,
          detail: status ? `占用 ${status.occupant_count} 人 / 上限 ${status.max_sharing}` : `显存 ${gpu.memory_used_mb ?? 0} / ${gpu.memory_total_mb ?? 0} MB`,
          selectable: Boolean(node?.online && node.schedulable && status?.selectable !== false),
        };
      });
    }
    const eligibleNodes = nodes.filter((node) => node.online && node.schedulable);
    const allIndexes = new Set(eligibleNodes.flatMap((node) => node.gpus.map((gpu) => gpu.index)));
    return Array.from(allIndexes).sort((a, b) => a - b).map((index) => {
      const required = new Set([...selected, index]);
      const compatibleNodes = eligibleNodes.filter((node) => {
        const available = new Set(node.gpus.map((gpu) => gpu.index));
        const sharing = new Map(node.gpu_sharing.map((row) => [row.gpu_index, row.selectable]));
        return Array.from(required).every((gpuId) => available.has(gpuId) && sharing.get(gpuId) !== false);
      });
      return {
        index,
        label: `GPU ${index}`,
        detail: `${compatibleNodes.length} 个可调度节点可满足当前组合`,
        selectable: selected.includes(index) || compatibleNodes.length > 0,
      };
    });
  }, [gpuSharing, localNode, nodeId, nodes, placementMode, selected]);

  const setPlacement = (mode: PlacementMode) => {
    setPlacementMode(mode);
    setSelected([]);
    setError("");
  };

  const toggleGpu = (choice: GpuChoice) => {
    if (!choice.selectable) return;
    setSelected((current) => current.includes(choice.index) ? current.filter((id) => id !== choice.index) : [...current, choice.index]);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (placementMode === "specific" && !nodeId) {
      setError("请选择目标节点");
      return;
    }
    if (!cpuOnly && selected.length === 0) {
      setError("请选择至少一块 GPU，或勾选纯 CPU 容器");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const res = await fetcher<ApplyApiResult>("/containers/apply", {
        method: "POST",
        body: JSON.stringify({
          cpu_only: cpuOnly,
          gpu_ids: cpuOnly ? [] : selected,
          lease_days: leaseDays,
          placement_mode: placementMode,
          node_id: placementMode === "specific" ? nodeId : null,
        }),
      });
      if (res.pending_share_approval) {
        setPendingInfo(res.message || "已发起共用申请，占用者全部同意后自动创建容器");
        return;
      }
      const container = res.container;
      setCreated({
        ssh_port: container.ssh_port,
        ssh_password: container.ssh_password || "",
        ssh_host: container.ssh_host,
        access_host: container.access_host,
        node_name: container.node_name,
        extra_ports: container.extra_ports ?? undefined,
        service_urls: container.service_urls ?? undefined,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "申请失败");
    } finally {
      setLoading(false);
    }
  };

  const handleDone = () => {
    setCreated(null);
    setPendingInfo(null);
    onSuccess();
  };

  if (pendingInfo) {
    return (
      <div className="modal-overlay" onClick={handleDone}>
        <div className="modal modal-success" onClick={(e) => e.stopPropagation()}>
          <div className="modal-header"><h2>已提交共用申请</h2><button className="btn btn-ghost" onClick={handleDone}>×</button></div>
          <div className="created-info"><p>{pendingInfo}</p><p className="modal-hint">请在「我的容器」中查看各占用者的同意进度。</p></div>
          <div className="modal-actions"><button type="button" className="btn btn-primary" onClick={handleDone}>完成</button></div>
        </div>
      </div>
    );
  }

  if (created) {
    const sshHost = created.ssh_host || created.access_host;
    return (
      <div className="modal-overlay" onClick={handleDone}>
        <div className="modal modal-success" onClick={(e) => e.stopPropagation()}>
          <div className="modal-header"><h2>申请成功</h2><button className="btn btn-ghost" onClick={handleDone}>×</button></div>
          <div className="created-info">
            <p className="created-tagline">升华还是埋没，看自己的造化。</p>
            <p><strong>请妥善保存以下信息，关闭后可在「我的容器」中查看。</strong></p>
            {created.node_name && <div className="created-row"><span>计算节点:</span><code>{created.node_name}</code></div>}
            <div className="created-row"><span>SSH 端口:</span><code>{created.ssh_port}</code></div>
            <div className="created-row"><span>SSH 密码:</span><code>{created.ssh_password}</code><button type="button" className="btn-copy" onClick={() => navigator.clipboard.writeText(created.ssh_password)}>复制</button></div>
            {created.extra_ports && Object.keys(created.extra_ports).length > 0 && (
              <div className="extra-ports">
                <span className="label">服务端口映射:</span>
                {Object.entries(created.extra_ports).map(([containerPort, hostPort]) => (
                  <div key={containerPort} className="port-row">{SERVICE_LABELS[containerPort] || containerPort}: 宿主机 <code>{hostPort}</code> → 容器 {containerPort}</div>
                ))}
              </div>
            )}
            {created.service_urls && Object.entries(created.service_urls).map(([port, url]) => <a className="created-service-link" href={url} target="_blank" rel="noreferrer" key={port}>{SERVICE_LABELS[port] || port}</a>)}
            {sshHost && <p className="ssh-cmd">ssh -p {created.ssh_port} root@{sshHost}</p>}
          </div>
          <div className="modal-actions"><button type="button" className="btn btn-primary" onClick={handleDone}>完成</button></div>
        </div>
      </div>
    );
  }

  const hasAnyGpu = gpuChoices.length > 0;
  const submitDisabled = loading || (placementMode === "specific" && !nodeId) || (!cpuOnly && (!hasAnyGpu || selected.length === 0));

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header"><h2>申请计算容器</h2><button className="btn btn-ghost" onClick={onClose}>×</button></div>
        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>节点调度方式</label>
            <div className="placement-options">
              <button type="button" className={placementMode === "auto" ? "active" : ""} onClick={() => setPlacement("auto")}>自动调度</button>
              <button type="button" className={placementMode === "local" ? "active" : ""} onClick={() => setPlacement("local")} disabled={!localNode?.online || !localNode.schedulable}>本机节点</button>
              <button type="button" className={placementMode === "specific" ? "active" : ""} onClick={() => setPlacement("specific")}>指定节点</button>
            </div>
          </div>
          {placementMode === "specific" && (
            <div className="form-group">
              <label>目标节点</label>
              <select className="node-select" value={nodeId} onChange={(e) => { setNodeId(e.target.value); setSelected([]); }}>
                <option value="">请选择节点</option>
                {nodes.map((node) => <option value={node.node_id} key={node.node_id} disabled={!node.online || !node.schedulable}>{node.node_name} · {!node.online ? "离线" : node.schedulable ? `${node.gpus.length} GPU` : "不可调度"}</option>)}
              </select>
            </div>
          )}
          <div className="form-group"><label className="checkbox-label"><input type="checkbox" checked={cpuOnly} onChange={(e) => setCpuOnly(e.target.checked)} />纯 CPU 容器（无 GPU）</label></div>
          {!cpuOnly && (
            <div className="form-group">
              <label>选择 GPU（可多选）</label>
              <p className="modal-hint gpu-share-hint">自动调度会从在线且可调度的节点中选择；指定节点时仅展示该节点 GPU。</p>
              <div className="gpu-checkboxes gpu-share-list">
                {!hasAnyGpu ? <p className="no-free">当前选择下无法检测到 GPU</p> : gpuChoices.map((choice) => (
                  <label key={choice.index} className={`checkbox-label gpu-share-row ${choice.selectable ? "" : "gpu-share-disabled"}`}>
                    <input type="checkbox" checked={selected.includes(choice.index)} disabled={!choice.selectable} onChange={() => toggleGpu(choice)} />
                    <span className="gpu-share-label">{choice.label}<span className="gpu-share-status">{choice.detail}</span></span>
                  </label>
                ))}
              </div>
            </div>
          )}
          <div className="form-group">
            <label>租期（天）</label>
            <div className="lease-days-row" role="group" aria-label="选择租期">{[1, 2, 3, 4, 5, 6, 7].map((day) => <button key={day} type="button" className={`lease-day-btn ${leaseDays === day ? "active" : ""}`} onClick={() => setLeaseDays(day)}>{day} 天</button>)}</div>
          </div>
          <p className="modal-hint">SSH 与常用端口随机映射，密码自动分配；个人目录挂载至 /workspace</p>
          {error && <div className="form-error">{error}</div>}
          <div className="modal-actions"><button type="button" className="btn" onClick={onClose}>取消</button><button type="submit" className="btn btn-primary" disabled={submitDisabled}>{loading ? "申请中…" : "确认申请"}</button></div>
        </form>
      </div>
    </div>
  );
}
