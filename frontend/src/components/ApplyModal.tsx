import { useState } from "react";
import { fetcher } from "../api/client";
import "./ApplyModal.css";

const SERVICE_LABELS: Record<number, string> = { 8888: "Jupyter", 6006: "TensorBoard", 8080: "Code Server" };

export interface GpuSharingRow {
  gpu_index: number;
  occupant_count: number;
  max_sharing: number;
  selectable: boolean;
}

interface ApplyModalProps {
  gpuSharing: GpuSharingRow[];
  onClose: () => void;
  onSuccess: () => void;
}

interface CreatedContainer {
  ssh_port: number;
  ssh_password: string;
  extra_ports?: Record<number, number>;
}

interface ApplyApiContainer {
  ssh_port: number;
  ssh_password?: string | null;
  extra_ports?: Record<number, number> | null;
}

interface ApplyApiResult {
  container: ApplyApiContainer;
  pending_share_approval: boolean;
  message?: string | null;
}

export default function ApplyModal({ gpuSharing, onClose, onSuccess }: ApplyModalProps) {
  const [cpuOnly, setCpuOnly] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [leaseDays, setLeaseDays] = useState(3);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<CreatedContainer | null>(null);
  const [pendingInfo, setPendingInfo] = useState<string | null>(null);

  const toggleGpu = (row: GpuSharingRow) => {
    if (!row.selectable) return;
    const id = row.gpu_index;
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
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
        }),
      });
      if (res.pending_share_approval) {
        setPendingInfo(res.message || "已发起共用申请，占用者全部同意后自动创建容器");
        return;
      }
      const cont = res.container;
      setCreated({
        ssh_port: cont.ssh_port,
        ssh_password: cont.ssh_password || "",
        extra_ports: cont.extra_ports ?? undefined,
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
          <div className="modal-header">
            <h2>已提交共用申请</h2>
            <button className="btn btn-ghost" onClick={handleDone}>×</button>
          </div>
          <div className="created-info">
            <p>{pendingInfo}</p>
            <p className="modal-hint">请在「我的容器」中查看各占用者的同意进度。</p>
          </div>
          <div className="modal-actions">
            <button type="button" className="btn btn-primary" onClick={handleDone}>完成</button>
          </div>
        </div>
      </div>
    );
  }

  if (created) {
    return (
      <div className="modal-overlay" onClick={handleDone}>
        <div className="modal modal-success" onClick={(e) => e.stopPropagation()}>
          <div className="modal-header">
            <h2>申请成功</h2>
            <button className="btn btn-ghost" onClick={handleDone}>×</button>
          </div>
          <div className="created-info">
            <p className="created-tagline">升华还是埋没，看自己的造化。</p>
            <p><strong>请妥善保存以下信息，关闭后可在「我的容器」中查看。</strong></p>
            <div className="created-row">
              <span>SSH 端口:</span>
              <code>{created.ssh_port}</code>
            </div>
            <div className="created-row">
              <span>SSH 密码:</span>
              <code>{created.ssh_password}</code>
              <button type="button" className="btn-copy" onClick={() => navigator.clipboard.writeText(created.ssh_password)}>
                复制
              </button>
            </div>
            {created.extra_ports && Object.keys(created.extra_ports).length > 0 && (
              <div className="extra-ports">
                <span className="label">服务端口映射:</span>
                {Object.entries(created.extra_ports).map(([cp, hp]) => (
                  <div key={cp} className="port-row">
                    {SERVICE_LABELS[Number(cp)] || cp}: 宿主机 <code>{hp}</code> → 容器 {cp}
                  </div>
                ))}
              </div>
            )}
            <p className="ssh-cmd">ssh -p {created.ssh_port} root@{window.location.hostname}</p>
          </div>
          <div className="modal-actions">
            <button type="button" className="btn btn-primary" onClick={handleDone}>完成</button>
          </div>
        </div>
      </div>
    );
  }

  const hasAnyGpu = gpuSharing.length > 0;
  const submitDisabled =
    loading ||
    (!cpuOnly && (!hasAnyGpu || !selected.some((id) => gpuSharing.find((g) => g.gpu_index === id && g.selectable))));

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>申请 GPU 容器</h2>
          <button className="btn btn-ghost" onClick={onClose}>
            ×
          </button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label className="checkbox-label">
              <input type="checkbox" checked={cpuOnly} onChange={(e) => setCpuOnly(e.target.checked)} />
              纯 CPU 容器（无 GPU）
            </label>
          </div>
          {!cpuOnly && (
            <div className="form-group">
              <label>选择 GPU（可多选）</label>
              <p className="modal-hint gpu-share-hint">
                显示全部显卡及占用人数；未满员即可勾选。若卡上已有他人容器，提交后将向对方发起共用同意流程。
              </p>
              <div className="gpu-checkboxes gpu-share-list">
                {!hasAnyGpu ? (
                  <p className="no-free">当前无法检测到 GPU 列表</p>
                ) : (
                  gpuSharing.map((row) => {
                    const idle = row.occupant_count === 0;
                    const label = idle
                      ? "空闲"
                      : `占用 ${row.occupant_count} 人 / 上限 ${row.max_sharing}`;
                    const disabled = !row.selectable;
                    return (
                      <label
                        key={row.gpu_index}
                        className={`checkbox-label gpu-share-row ${disabled ? "gpu-share-disabled" : ""}`}
                      >
                        <input
                          type="checkbox"
                          checked={selected.includes(row.gpu_index)}
                          disabled={disabled}
                          onChange={() => toggleGpu(row)}
                        />
                        <span className="gpu-share-label">
                          GPU {row.gpu_index}
                          <span className={`gpu-share-status ${idle ? "idle" : "busy"}`}>{label}</span>
                          {disabled && <span className="gpu-share-full">（已满）</span>}
                        </span>
                      </label>
                    );
                  })
                )}
              </div>
            </div>
          )}
          <div className="form-group">
            <label>租期（天）</label>
            <div className="lease-days-row" role="group" aria-label="选择租期">
              {[1, 2, 3, 4, 5, 6, 7].map((d) => (
                <button
                  key={d}
                  type="button"
                  className={`lease-day-btn ${leaseDays === d ? "active" : ""}`}
                  onClick={() => setLeaseDays(d)}
                >
                  {d} 天
                </button>
              ))}
            </div>
          </div>
          <p className="modal-hint">
            SSH 与常用端口(Jupyter/TensorBoard/Web) 随机映射，密码自动分配；个人目录挂载至 /workspace
          </p>
          {error && <div className="form-error">{error}</div>}
          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose}>
              取消
            </button>
            <button type="submit" className="btn btn-primary" disabled={submitDisabled}>
              {loading ? "申请中…" : "确认申请"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
