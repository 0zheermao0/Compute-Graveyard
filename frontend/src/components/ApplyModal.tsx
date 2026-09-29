import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { fetcher } from "../api/client";
import type { DashboardNode } from "../pages/Dashboard";
import "./ApplyModal.css";

const SERVICE_LABELS: Record<string, string> = { 8888: "Jupyter", 6006: "TensorBoard", 8080: "Code Server" };

const gpuIds = (value: string) => (value.match(/\d+/g) ?? []).map(Number);
const displayExpiry = (value: string) => value.replace("T", " ").slice(0, 19);

type PlacementMode = "auto" | "local" | "specific";
type ApplyMode = "new" | "merge";

interface MergeTarget {
  id: number;
  name: string;
  status: string;
  container_id: string | null;
  node_id: string | null;
  node_name?: string | null;
  gpu_ids: string;
  expires_at: string;
}

export interface GpuSharingRow {
  gpu_index: number;
  occupant_count: number;
  max_sharing: number;
  selectable: boolean;
  external_occupied?: boolean;
  unknown_occupant_count?: number;
  worker_shareable?: boolean;
}

interface ApplyModalProps {
  gpuSharing: GpuSharingRow[];
  nodes: DashboardNode[];
  isMaster: boolean;
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

type GpuChoiceState = "free" | "shareable" | "full" | "external" | "unavailable" | "aggregate";

interface GpuChoice {
  index: number;
  label: string;
  detail: string;
  selectable: boolean;
  state: GpuChoiceState;
}

const sharingChoice = (row: GpuSharingRow | undefined, available: boolean, ownedFull = false, remoteSpecificNew = false): Pick<GpuChoice, "detail" | "selectable" | "state"> => {
  if (!available) return { detail: "节点离线或不可调度（不可选）", selectable: false, state: "unavailable" };
  if (!row) return { detail: "共享状态不可用", selectable: false, state: "unavailable" };
  const full = row.occupant_count >= row.max_sharing;
  if (full && ownedFull && !row.external_occupied) return { detail: "已满额（本人已占用，可申请合并）", selectable: true, state: "shareable" };
  if (full) return { detail: `已占满（${row.occupant_count} / ${row.max_sharing}，不可选）`, selectable: false, state: "full" };
  if (row.external_occupied && remoteSpecificNew && row.worker_shareable === true && row.unknown_occupant_count === 0 && row.occupant_count > 0 && row.max_sharing > 0) return { detail: `已占用，需在 Worker 系统同意（${row.occupant_count} / ${row.max_sharing}）`, selectable: true, state: "shareable" };
  if (row.external_occupied) return { detail: "其他系统或未知容器占用（不可选）", selectable: false, state: "external" };
  if (!row.selectable) return { detail: "当前不可选", selectable: false, state: "unavailable" };
  if (row.occupant_count > 0) return { detail: `已占用 ${row.occupant_count} 人 / 上限 ${row.max_sharing} · 可申请共用，需占用者同意`, selectable: true, state: "shareable" };
  return { detail: "空闲 · 可直接申请", selectable: true, state: "free" };
};

export default function ApplyModal({ gpuSharing, nodes, isMaster, onClose, onSuccess }: ApplyModalProps) {
  const localNode = nodes.find((node) => node.is_local);
  const defaultMode: PlacementMode = "local";
  const [applyMode, setApplyMode] = useState<ApplyMode>("new");
  const [targets, setTargets] = useState<MergeTarget[]>([]);
  const [targetsLoading, setTargetsLoading] = useState(true);
  const [targetsError, setTargetsError] = useState("");
  const [targetId, setTargetId] = useState<number | null>(null);
  const [cpuOnly, setCpuOnly] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [leaseDays, setLeaseDays] = useState(3);
  const [placementMode, setPlacementMode] = useState<PlacementMode>(defaultMode);
  const [nodeId, setNodeId] = useState("");
  const [modeExpanded, setModeExpanded] = useState(false);
  const [nodeExpanded, setNodeExpanded] = useState(false);
  const modeToggleRef = useRef<HTMLButtonElement>(null);
  const nodeToggleRef = useRef<HTMLButtonElement>(null);
  const focusAfterCollapse = useRef<"mode" | "node" | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<CreatedContainer | null>(null);
  const [pendingInfo, setPendingInfo] = useState<string | null>(null);
  const [submittedMode, setSubmittedMode] = useState<ApplyMode>("new");
  const dialogRef = useRef<HTMLDivElement>(null);
  const focusedDialog = useRef<HTMLDivElement | null>(null);
  const returnFocus = useRef<HTMLElement | null>(null);

  const restoreFocus = () => {
    const dialog = focusedDialog.current;
    if (returnFocus.current?.isConnected && dialog && (dialog.contains(document.activeElement) || (document.activeElement === document.body && !dialog.isConnected))) {
      returnFocus.current.focus();
    }
  };

  useLayoutEffect(() => {
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => restoreFocus();
  }, []);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    const previous = focusedDialog.current;
    if (dialog && (!previous || previous.contains(document.activeElement) || (document.activeElement === document.body && !previous.isConnected))) {
      dialog.querySelector<HTMLButtonElement>('.modal-header button')?.focus();
    }
    focusedDialog.current = dialog;
  }, [!!pendingInfo, !!created]);

  useLayoutEffect(() => {
    if (focusAfterCollapse.current === "mode" && !modeExpanded) modeToggleRef.current?.focus();
    if (focusAfterCollapse.current === "node" && !nodeExpanded) nodeToggleRef.current?.focus();
    focusAfterCollapse.current = null;
  }, [modeExpanded, nodeExpanded, applyMode, placementMode, targetId, nodeId]);

  useEffect(() => {
    let active = true;
    fetcher<MergeTarget[]>("/containers/my").then((containers) => {
      if (active) setTargets(containers);
    }).catch((e) => {
      if (active) setTargetsError(e instanceof Error ? e.message : "加载已有容器失败");
    }).finally(() => {
      if (active) setTargetsLoading(false);
    });
    return () => { active = false; };
  }, []);

  const eligibleTargets = targets.filter((container) => container.status === "running" && Boolean(container.container_id) && (isMaster || Boolean(localNode && (container.node_id || localNode.node_id) === localNode.node_id)));
  const target = eligibleTargets.find((container) => container.id === targetId);
  const targetNodeId = target?.node_id || localNode?.node_id;
  const targetNode = nodes.find((node) => node.node_id === targetNodeId);
  const existingGpuIds = new Set(gpuIds(target?.gpu_ids ?? ""));
  const ownedGpuIds = new Set(targets.filter((container) => container.status === "running" && (container.node_id || localNode?.node_id) === targetNodeId).flatMap((container) => gpuIds(container.gpu_ids)));
  const merging = applyMode === "merge";
  const effectivePlacement = merging ? "specific" : isMaster ? placementMode : "local";
  const localUnavailable = effectivePlacement === "local" && (!localNode?.online || !localNode.schedulable);
  const effectiveNodeId = merging ? targetNodeId : nodeId;

  const gpuChoices = useMemo<GpuChoice[]>(() => {
    if (merging) {
      const sharing = new Map((targetNode?.gpu_sharing ?? []).map((row) => [row.gpu_index, row]));
      return (targetNode?.gpus ?? []).filter((gpu) => !existingGpuIds.has(gpu.index)).map((gpu) => {
        const status = sharing.get(gpu.index);
        const occupiedByMe = ownedGpuIds.has(gpu.index);
        return {
          index: gpu.index,
          label: `GPU ${gpu.index} · ${gpu.name}`,
          ...sharingChoice(status, Boolean(targetNode?.online && targetNode.schedulable), occupiedByMe),
        };
      });
    }
    if (effectivePlacement === "local") {
      const rows = localNode?.gpu_sharing?.length ? localNode.gpu_sharing : gpuSharing;
      return rows.map((row) => ({
        index: row.gpu_index,
        label: `GPU ${row.gpu_index}`,
        ...sharingChoice(row, Boolean(localNode?.online && localNode.schedulable)),
      }));
    }
    if (effectivePlacement === "specific") {
      const node = nodes.find((item) => item.node_id === nodeId);
      const sharing = new Map((node?.gpu_sharing ?? []).map((row) => [row.gpu_index, row]));
      return (node?.gpus ?? []).map((gpu) => {
        const status = sharing.get(gpu.index);
        return {
          index: gpu.index,
          label: `GPU ${gpu.index} · ${gpu.name}`,
          ...sharingChoice(status, Boolean(node?.online && node.schedulable), false, Boolean(isMaster && node && !node.is_local)),
        };
      });
    }
    const eligibleNodes = nodes.filter((node) => node.online && node.schedulable);
    const allIndexes = new Set(eligibleNodes.flatMap((node) => node.gpus.map((gpu) => gpu.index)));
    return Array.from(allIndexes).sort((a, b) => a - b).map((index) => {
      const required = new Set([...selected, index]);
      const compatibleNodes = eligibleNodes.filter((node) => {
        const available = new Set(node.gpus.map((gpu) => gpu.index));
        const sharing = new Map(node.gpu_sharing.map((row) => [row.gpu_index, row]));
        return Array.from(required).every((gpuId) => {
          const status = sharing.get(gpuId);
          return available.has(gpuId) && (node.is_local
            ? Boolean(status?.selectable && !status.external_occupied)
            : Boolean(status && status.selectable && !status.external_occupied && status.occupant_count === 0));
        });
      });
      return {
        index,
        label: `GPU ${index}`,
        detail: `${compatibleNodes.length} 个可调度节点可满足当前组合（各节点占用情况不同；远端仅匹配空闲 GPU）`,
        selectable: compatibleNodes.length > 0,
        state: "aggregate" as const,
      };
    });
  }, [gpuSharing, localNode, nodeId, nodes, effectivePlacement, selected, merging, isMaster, targetNode, target?.gpu_ids, targetNodeId, targets]);

  const setMode = (mode: ApplyMode) => {
    if (mode !== applyMode) {
      setApplyMode(mode);
      setTargetId(null);
      setSelected([]);
      setCpuOnly(false);
      setError("");
    }
    focusAfterCollapse.current = "mode";
    setModeExpanded(false);
    setNodeExpanded(mode === "merge");
  };

  const setPlacement = (mode: PlacementMode) => {
    if (mode !== placementMode) {
      setPlacementMode(mode);
      if (mode === "specific") setNodeId("");
      setSelected([]);
      setError("");
    }
    if (mode !== "specific" || (mode === placementMode && nodeId)) {
      focusAfterCollapse.current = "node";
      setNodeExpanded(false);
    }
  };

  const toggleGpu = (choice: GpuChoice) => {
    if (!choice.selectable && !selected.includes(choice.index)) return;
    setSelected((current) => current.includes(choice.index) ? current.filter((id) => id !== choice.index) : [...current, choice.index]);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (localUnavailable) {
      setError(isMaster ? "本机节点当前不可用，请选择自动调度或其他节点" : "本机节点当前不可用，暂无法申请容器");
      return;
    }
    if (merging && (!target || !targetNode)) {
      setError("请选择运行中的已有容器及其节点");
      return;
    }
    if (effectivePlacement === "specific" && !effectiveNodeId) {
      setError("请选择目标节点");
      return;
    }
    if (merging && (!targetNode?.online || !targetNode.schedulable)) {
      setError(`节点「${targetNode?.node_name || target?.node_name || targetNodeId}」离线或不可调度，暂无法合并 GPU`);
      return;
    }
    if (!cpuOnly && selected.length === 0) {
      setError(merging ? "请选择至少一块尚未分配给该容器的 GPU" : "请选择至少一块 GPU，或勾选纯 CPU 容器");
      return;
    }
    if (merging && selected.some((id) => !gpuChoices.find((choice) => choice.index === id && choice.selectable))) {
      setError("所选 GPU 已不可用，请重新选择");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const res = await fetcher<ApplyApiResult>("/containers/apply", {
        method: "POST",
        body: JSON.stringify({
          cpu_only: merging ? false : cpuOnly,
          gpu_ids: merging ? selected : cpuOnly ? [] : selected,
          lease_days: merging ? 1 : leaseDays,
          placement_mode: effectivePlacement,
          node_id: effectivePlacement === "specific" ? effectiveNodeId : null,
          target_container_id: merging ? targetId : null,
        }),
      });
      setSubmittedMode(applyMode);
      if (res.pending_share_approval) {
        setPendingInfo(merging ? "已发起共用申请，占用者全部同意后将新增 GPU 合并至原容器；到期时间沿用原容器，合并时运行中的进程将停止。" : res.message || "已发起共用申请，占用者全部同意后自动创建容器");
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
    restoreFocus();
    setCreated(null);
    setPendingInfo(null);
    onSuccess();
  };

  const handleClose = () => {
    restoreFocus();
    onClose();
  };

  const handleDialogKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target instanceof Element && e.target.closest('[role="dialog"]') !== e.currentTarget) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      if (pendingInfo || created) handleDone();
      else if (!loading) handleClose();
      return;
    }
    if (e.key !== "Tab") return;
    const dialog = e.currentTarget;
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])')).filter((element) =>
      !element.matches(':disabled') && element.tabIndex >= 0 && element.getClientRects().length > 0 && getComputedStyle(element).visibility !== "hidden"
    );
    if (!focusable.length) {
      e.preventDefault();
      dialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
      e.preventDefault();
      first.focus();
    }
  };

  if (pendingInfo) {
    return (
      <div className="modal-overlay" onClick={handleDone}>
        <div className="modal modal-success apply-result" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="apply-result-title" onKeyDown={handleDialogKeyDown} onClick={(e) => e.stopPropagation()}>
          <div className="modal-header"><div><span className="apply-result-state">等待审批</span><h2 id="apply-result-title">已提交共用申请</h2></div><button type="button" className="btn btn-ghost" aria-label="关闭弹窗" onClick={handleDone}>×</button></div>
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
        <div className="modal modal-success apply-result" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="apply-result-title" onKeyDown={handleDialogKeyDown} onClick={(e) => e.stopPropagation()}>
          <div className="modal-header"><div><span className="apply-result-state">申请已完成</span><h2 id="apply-result-title">{submittedMode === "merge" ? "GPU 合并成功" : "申请成功"}</h2></div><button type="button" className="btn btn-ghost" aria-label="关闭弹窗" onClick={handleDone}>×</button></div>
          <div className="created-info">
            {submittedMode === "merge" ? <p className="modal-hint">新增 GPU 已合并至原容器，容器内文件及 /workspace 保留；原运行进程已停止，到期时间不变。</p> : <p className="created-tagline">升华还是埋没，看自己的造化。</p>}
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
  const submitDisabled = loading || localUnavailable || (merging && (targetsLoading || !target || !targetNode?.online || !targetNode.schedulable)) || (effectivePlacement === "specific" && !effectiveNodeId) || ((merging || !cpuOnly) && (!hasAnyGpu || selected.length === 0 || selected.some((id) => !gpuChoices.find((choice) => choice.index === id && choice.selectable))));
  const availableGpuCount = gpuChoices.filter((choice) => choice.selectable).length;
  const placementSummary = merging ? targetNode?.node_name || target?.node_name || "待选择容器" : effectivePlacement === "auto" ? "自动调度" : effectivePlacement === "local" ? localNode?.node_name || "本机节点" : nodes.find((node) => node.node_id === nodeId)?.node_name || "待选择节点";

  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div className="modal apply-modal" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="apply-title" onKeyDown={handleDialogKeyDown} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header"><div><span className="apply-eyebrow">COMPUTE / RESOURCE REQUEST</span><h2 id="apply-title">申请计算容器</h2><p>{isMaster ? "选择运行位置与计算资源，确认后提交申请。" : "选择本机计算资源，确认后提交申请。"}</p></div><button type="button" className="btn btn-ghost" aria-label="关闭弹窗" onClick={handleClose}>×</button></div>
        <form onSubmit={handleSubmit}>
          <section className="apply-section" aria-labelledby="apply-mode-title">
            <div className="apply-section-heading"><span className="apply-step">01</span><div className="apply-section-description"><h3 id="apply-mode-title">申请方式</h3><p>创建新环境，或为正在运行的容器增加 GPU。</p></div><button ref={modeToggleRef} type="button" className="apply-section-toggle" aria-expanded={modeExpanded} aria-controls="apply-mode-options" onClick={() => setModeExpanded((open) => !open)}>{modeExpanded ? "收起" : "展开修改"}</button></div>
            {!modeExpanded && <p className="apply-section-summary">已选：{merging ? "合并至已有容器" : "新建容器"}</p>}
            <div id="apply-mode-options" hidden={!modeExpanded} className="placement-options apply-mode-options" role="group" aria-label="申请方式">
              <button type="button" className={applyMode === "new" ? "active" : ""} aria-pressed={applyMode === "new"} onClick={() => setMode("new")}>新建容器<small>独立计算环境</small></button>
              <button type="button" className={merging ? "active" : ""} aria-pressed={merging} onClick={() => setMode("merge")}>合并至已有容器<small>追加 GPU 资源</small></button>
            </div>
          </section>
          {(merging || isMaster) && <section className="apply-section" aria-labelledby="apply-node-title">
            <div className="apply-section-heading"><span className="apply-step">02</span><div className="apply-section-description"><h3 id="apply-node-title">{merging ? "目标容器与节点" : "运行节点"}</h3><p>{merging ? "GPU 将添加至原容器所在节点。" : "选择由系统调度，或锁定某台节点。"}</p></div><button ref={nodeToggleRef} type="button" className="apply-section-toggle" aria-expanded={nodeExpanded} aria-controls="apply-node-options" disabled={nodeExpanded && (merging ? !targetId : placementMode === "specific" && !nodeId)} onClick={() => setNodeExpanded((open) => !open)}>{nodeExpanded ? "收起" : "展开修改"}</button></div>
            {!nodeExpanded && <p className="apply-section-summary">已选：{merging ? target ? `合并至 ${target.name} · ${placementSummary}` : "待选择目标容器" : placementMode === "auto" ? "自动调度" : placementMode === "local" ? `本机节点 · ${placementSummary}` : `指定节点 · ${placementSummary}`}</p>}
            {!nodeExpanded && localUnavailable && <p className="form-error" role="alert">本机节点当前不可用，无法提交申请。请展开修改，选择自动调度或其他节点。</p>}
            <div id="apply-node-options" hidden={!nodeExpanded}>
            {merging ? (
              <div className="form-group">
                <label htmlFor="merge-target">目标容器</label>
                <select id="merge-target" className="node-select" value={targetId ?? ""} onChange={(e) => { setTargetId(e.target.value ? Number(e.target.value) : null); setSelected([]); setError(""); if (e.target.value) { focusAfterCollapse.current = "node"; setNodeExpanded(false); } }} disabled={targetsLoading || !!targetsError}>
                  <option value="">{targetsLoading ? "正在加载容器…" : "请选择已有容器"}</option>
                  {eligibleTargets.map((container) => {
                    const node = nodes.find((item) => item.node_id === (container.node_id || localNode?.node_id));
                    return <option key={container.id} value={container.id} disabled={!node?.online || !node.schedulable}>{container.name} · {node?.node_name || container.node_name || container.node_id || "未知节点"} · {!node ? "节点不可用" : !node.online ? "离线" : !node.schedulable ? "不可调度" : `到期 ${displayExpiry(container.expires_at)}（服务器时间）`}</option>;
                  })}
                </select>
                {targetsError && <p className="form-error" role="alert">{targetsError}</p>}
                {!targetsLoading && !targetsError && eligibleTargets.length === 0 && <p className="modal-hint">暂无运行中且有 Docker ID 的可合并容器。</p>}
                {target && <p className="modal-hint">目标节点：{targetNode?.node_name || target.node_name || target.node_id || "未知节点"}；沿用原容器到期时间 {displayExpiry(target.expires_at)}（服务器时间）。合并允许短暂停机，容器内文件及 /workspace 保留，运行中的进程将停止。</p>}
              </div>
            ) : <>
              <div className="placement-options" role="group" aria-label="节点调度方式">
                <button type="button" className={placementMode === "auto" ? "active" : ""} aria-pressed={placementMode === "auto"} onClick={() => setPlacement("auto")}>自动调度<small>匹配可用节点</small></button>
                <button type="button" className={placementMode === "local" ? "active" : ""} aria-pressed={placementMode === "local"} onClick={() => setPlacement("local")} disabled={!localNode?.online || !localNode.schedulable}>本机节点<small>{!localNode?.online || !localNode.schedulable ? "当前不可用" : localNode.node_name}</small></button>
                <button type="button" className={placementMode === "specific" ? "active" : ""} aria-pressed={placementMode === "specific"} onClick={() => setPlacement("specific")}>指定节点<small>手动选择位置</small></button>
              </div>
              {placementMode === "specific" && <div className="form-group apply-node-field"><label htmlFor="apply-node-select">目标节点</label><select id="apply-node-select" className="node-select" value={nodeId} onChange={(e) => { setNodeId(e.target.value); setSelected([]); setError(""); if (e.target.value) { focusAfterCollapse.current = "node"; setNodeExpanded(false); } }}><option value="">请选择节点</option>{nodes.map((node) => <option value={node.node_id} key={node.node_id} disabled={!node.online || !node.schedulable}>{node.node_name} · {!node.online ? "离线" : node.schedulable ? `${node.gpus.length} GPU` : "不可调度"}</option>)}</select></div>}
            </>}
            </div>
          </section>}
          <section className="apply-section" aria-labelledby="apply-resource-title">
            <div className="apply-section-heading"><span className="apply-step">{merging || isMaster ? "03" : "02"}</span><div><h3 id="apply-resource-title">计算资源</h3><p>{merging ? "选择要追加的 GPU。" : "按需选择 GPU，也可使用纯 CPU 环境。"}</p></div></div>
            {!isMaster && localUnavailable && <p className="form-error" role="alert">本机节点当前不可用，暂无法申请容器。</p>}
            {!merging && <div className="form-group"><label className="checkbox-label"><input type="checkbox" checked={cpuOnly} onChange={(e) => setCpuOnly(e.target.checked)} />纯 CPU 容器（无 GPU）</label></div>}
            {(merging || !cpuOnly) && <div className="form-group">
              <div className="apply-field-heading"><span id="apply-gpu-label">选择 GPU（可多选）</span><span className="apply-count">已选 {selected.length} · 可选 {availableGpuCount}</span></div>
              <p className="modal-hint gpu-share-hint">{merging ? "可选择目标容器尚未分配的 GPU；满额但本人已占用的卡也可申请。共用 GPU 需等待占用者同意，实际可用性以提交结果为准。" : isMaster ? "自动调度仅选择远端空闲 GPU；指定远端节点可申请共用已验证且未满额的 Worker 本地容器占用 GPU，需在 Worker 系统同意；未知或混合占用不可选。" : "选择本机可用 GPU；已占用的卡可能需要等待占用者同意共用。"}</p>
              <div className="gpu-checkboxes gpu-share-list" role="group" aria-labelledby="apply-gpu-label">
                {!hasAnyGpu ? <p className="no-free">{merging ? target ? "目标节点没有可新增的 GPU" : "请先选择目标容器" : "当前选择下无法检测到 GPU"}</p> : gpuChoices.map((choice) => (
                  <label key={choice.index} className={`checkbox-label gpu-share-row apply-gpu-${choice.state} ${choice.selectable ? "" : "gpu-share-disabled"}`}>
                    <input type="checkbox" checked={selected.includes(choice.index)} disabled={!choice.selectable && !selected.includes(choice.index)} onChange={() => toggleGpu(choice)} />
                    <span className="gpu-share-label">{choice.label}<span className="gpu-share-status">{choice.detail}</span></span>
                  </label>
                ))}
              </div>
            </div>}
          </section>
          {!merging && <section className="apply-section" aria-labelledby="apply-lease-title"><div className="apply-section-heading"><span className="apply-step">{isMaster ? "04" : "03"}</span><div><h3 id="apply-lease-title">使用期限</h3><p>选择容器租期，最长 7 天。</p></div></div><div className="lease-days-row" role="group" aria-label="选择租期">{[1, 2, 3, 4, 5, 6, 7].map((day) => <button key={day} type="button" aria-pressed={leaseDays === day} className={`lease-day-btn ${leaseDays === day ? "active" : ""}`} onClick={() => setLeaseDays(day)}>{day} 天</button>)}</div></section>}
          <div className="apply-summary"><span>申请概览</span><strong>{placementSummary} · {merging || !cpuOnly ? `${selected.length} 块 GPU` : "纯 CPU"}{!merging && ` · ${leaseDays} 天`}</strong></div>
          {!merging && <p className="modal-hint apply-footnote">SSH 与常用端口随机映射，密码自动分配；个人目录挂载至 /workspace</p>}
          {error && <div className="form-error" role="alert">{error}</div>}
          <div className="modal-actions"><button type="button" className="btn" onClick={handleClose}>取消</button><button type="submit" className="btn btn-primary" disabled={submitDisabled} aria-busy={loading}>{loading ? "申请中…" : merging ? "确认合并 GPU" : "确认申请"}</button></div>
        </form>
      </div>
    </div>
  );
}
