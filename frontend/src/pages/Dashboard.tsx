import { useState, useEffect, useRef } from "react";
import { fetcher } from "../api/client";
import ApplyModal from "../components/ApplyModal";
import GPUTwin from "../components/GPUTwin";
import "./Dashboard.css";

export interface GPUInfo {
  index: number;
  name: string;
  memory_used_mb: number | null;
  memory_total_mb: number | null;
  memory_percent: number | null;
  temperature?: number | null;
  utilization?: number | null;
}

interface Occupancy {
  gpu_index: number;
  container_name: string;
  username: string;
  display_name: string;
  real_name?: string | null;
  contact_type?: string | null;
  contact_value?: string | null;
  created_at?: string | null;
  duration_hours?: number | null;
  expires_at: string;
  ssh_port?: number;
}

interface RunningContainer {
  container_name: string;
  username: string;
  display_name: string;
  real_name?: string | null;
  contact_type?: string | null;
  contact_value?: string | null;
  gpu_ids: string;
  created_at?: string | null;
  duration_hours?: number | null;
  expires_at: string;
  ssh_port?: number;
}

interface UsageRankItem {
  rank: number;
  username: string;
  real_name?: string | null;
  total_hours: number;
}

interface DiskRankItem {
  rank: number;
  username: string;
  real_name?: string | null;
  usage_bytes: number;
}

interface GPUUtilRankItem {
  rank: number;
  username: string;
  real_name?: string | null;
  estimated_percent: number;
}

interface ReminderRankItem {
  username: string;
  real_name: string;
  unread_count: number;
}

type RankingCategory = "duration" | "disk" | "gpu";
const rankingCategories: RankingCategory[] = ["duration", "disk", "gpu"];

export interface GpuSharingStatus {
  gpu_index: number;
  occupant_count: number;
  unknown_occupant_count?: number;
  max_sharing: number;
  selectable: boolean;
  external_occupied?: boolean;
  worker_shareable?: boolean;
}

interface QuotaStatus {
  quota_bytes: number;
  usage_bytes: number;
  over_quota: boolean;
  blocked: boolean;
  scan_complete: boolean;
  quota_exempt: boolean;
}

const GIB = 1024 ** 3;

function formatGiB(bytes: number): string {
  return `${(bytes / GIB).toFixed(1)} GiB`;
}

function quotaPercent(quota: QuotaStatus): number {
  if (!quota.quota_bytes) return 0;
  return Math.min(100, quota.usage_bytes / quota.quota_bytes * 100);
}

interface SystemLoad {
  cpu_percent: number;
  memory_used_gb: number;
  memory_total_gb: number;
  memory_percent: number;
  disk_free_gb: number;
  disk_total_gb: number;
}

export interface DashboardNode {
  node_id: string;
  node_name: string;
  online: boolean;
  schedulable: boolean;
  public_host?: string | null;
  is_local: boolean;
  gpus: GPUInfo[];
  system_load?: SystemLoad | null;
  container_count: number;
  occupancies: Occupancy[];
  gpu_sharing: GpuSharingStatus[];
  error?: string | null;
}

interface DashboardData {
  gpus: GPUInfo[];
  system_load: SystemLoad;
  occupancies: Occupancy[];
  all_containers: RunningContainer[];
  weekly_ranking: UsageRankItem[];
  monthly_ranking: UsageRankItem[];
  disk_ranking: DiskRankItem[];
  weekly_gpu_ranking: GPUUtilRankItem[];
  monthly_gpu_ranking: GPUUtilRankItem[];
  reminder_ranking: ReminderRankItem[];
  gpu_sharing?: GpuSharingStatus[];
  max_gpu_sharing_users?: number;
  nodes?: DashboardNode[];
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState("");
  const [showApply, setShowApply] = useState(false);
  const [isMaster, setIsMaster] = useState(false);
  const [rankCategory, setRankCategory] = useState<RankingCategory>("duration");
  const [rankPeriod, setRankPeriod] = useState<"weekly" | "monthly">("weekly");
  const [rankHovered, setRankHovered] = useState(false);
  const [rankFocused, setRankFocused] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [quota, setQuota] = useState<QuotaStatus | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(true);
  const [quotaError, setQuotaError] = useState(false);
  const [remoteLoaded, setRemoteLoaded] = useState(false);
  const [remoteError, setRemoteError] = useState("");
  const localPending = useRef(false);
  const remotePending = useRef(false);
  const remoteLoadedRef = useRef(false);
  const mounted = useRef(false);
  const localRequestId = useRef(0);
  const appliedLocalRequestId = useRef(0);

  const normalizeData = (d: DashboardData): DashboardData => ({
    ...d,
    weekly_ranking: d.weekly_ranking ?? [],
    monthly_ranking: d.monthly_ranking ?? [],
    disk_ranking: d.disk_ranking ?? [],
    weekly_gpu_ranking: d.weekly_gpu_ranking ?? [],
    monthly_gpu_ranking: d.monthly_gpu_ranking ?? [],
    reminder_ranking: d.reminder_ranking ?? [],
    gpu_sharing: d.gpu_sharing ?? [],
  });

  const loadLocal = async () => {
    if (localPending.current) return;
    localPending.current = true;
    const requestId = ++localRequestId.current;
    try {
      const local = normalizeData(await fetcher<DashboardData>("/dashboard?local_only=true"));
      if (!mounted.current || requestId < appliedLocalRequestId.current) return;
      appliedLocalRequestId.current = requestId;
      setData((previous) => {
        if (!previous || !remoteLoadedRef.current) return local;
        const localNode = local.nodes?.find((node) => node.is_local);
        return {
          ...previous,
          gpus: local.gpus,
          system_load: local.system_load,
          gpu_sharing: local.gpu_sharing,
          nodes: previous.nodes?.map((node) => node.is_local && localNode ? localNode : node),
        };
      });
      setError("");
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      localPending.current = false;
    }
  };

  const loadRemote = async () => {
    if (remotePending.current) return;
    remotePending.current = true;
    const requestId = ++localRequestId.current;
    try {
      const full = normalizeData(await fetcher<DashboardData>("/dashboard"));
      if (!mounted.current) return;
      const preserveLocal = requestId < appliedLocalRequestId.current;
      if (!preserveLocal) appliedLocalRequestId.current = requestId;
      setData((previous) => {
        const localNode = preserveLocal ? previous?.nodes?.find((node) => node.is_local) : undefined;
        return {
          ...full,
          gpus: localNode?.gpus ?? full.gpus,
          gpu_sharing: localNode?.gpu_sharing ?? full.gpu_sharing,
          system_load: localNode?.system_load ?? full.system_load,
          nodes: full.nodes?.map((node) => node.is_local && localNode ? localNode : node),
        };
      });
      remoteLoadedRef.current = true;
      setRemoteLoaded(true);
      setRemoteError("");
      setError("");
    } catch (e) {
      if (mounted.current) {
        setRemoteError(e instanceof Error ? e.message : "远端节点加载失败");
        remoteLoadedRef.current = false;
        setRemoteLoaded(false);
        setData((previous) => previous ? {
          ...previous,
          nodes: previous.nodes?.map((node) => node.is_local ? node : { ...node, online: false, gpus: [], gpu_sharing: [], occupancies: [] }),
        } : previous);
      }
    } finally {
      remotePending.current = false;
    }
  };

  const loadQuota = async (refresh = false) => {
    try {
      const value = await fetcher<QuotaStatus>(
        refresh ? "/workspace/usage/refresh" : "/workspace/usage",
        refresh ? { method: "POST" } : undefined,
      );
      setQuota(value);
      setQuotaError(false);
    } catch {
      setQuota(null);
      setQuotaError(true);
    } finally {
      setQuotaLoading(false);
    }
  };

  useEffect(() => {
    let active = true;
    mounted.current = true;
    fetcher<{ role: string }>("/health").then((health) => {
      if (active) setIsMaster(health.role === "master");
    }).catch(() => {
      if (active) setIsMaster(false);
    });
    loadLocal();
    loadRemote();
    loadQuota(true);
    const id = setInterval(() => {
      loadLocal();
      loadRemote();
      loadQuota();
    }, 10000);
    return () => {
      active = false;
      mounted.current = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const updatePreference = () => setReducedMotion(preference.matches);
    preference.addEventListener("change", updatePreference);
    updatePreference();
    return () => preference.removeEventListener("change", updatePreference);
  }, []);

  useEffect(() => {
    if (rankHovered || rankFocused || reducedMotion) return;
    const id = setInterval(() => {
      setRankCategory((category) => rankingCategories[(rankingCategories.indexOf(category) + 1) % rankingCategories.length]);
    }, 6000);
    return () => clearInterval(id);
  }, [rankHovered, rankFocused, rankCategory, reducedMotion]);

  const ranking = rankCategory === "disk"
    ? (data?.disk_ranking ?? [])
    : rankCategory === "gpu"
      ? (rankPeriod === "weekly" ? data?.weekly_gpu_ranking ?? [] : data?.monthly_gpu_ranking ?? [])
      : (rankPeriod === "weekly" ? data?.weekly_ranking ?? [] : data?.monthly_ranking ?? []);
  const dashboardNodes: DashboardNode[] = data
    ? data.nodes?.length
      ? data.nodes
      : [{
          node_id: "local",
          node_name: "本机节点",
          online: true,
          schedulable: true,
          is_local: true,
          gpus: data.gpus,
          system_load: data.system_load,
          container_count: data.all_containers.length,
          occupancies: data.occupancies,
          gpu_sharing: data.gpu_sharing ?? [],
        }]
    : [];
  const quotaBlocked = Boolean(quota && !quota.quota_exempt && (!quota.scan_complete || quota.blocked));
  const applyDisabled = quotaLoading || quotaError || quotaBlocked;

  if (error && !data) {
    return <div className="dashboard-error">加载看板失败: {error}</div>;
  }

  return (
    <div className="dashboard dashboard-twin">
      <div className="dashboard-twin-bg" aria-hidden />
      <div className="dashboard-twin-glow" aria-hidden />
      <div className="dashboard-header">
        <h1>资源看板</h1>
        <button
          className="btn btn-primary"
          onClick={() => setShowApply(true)}
          disabled={applyDisabled}
          title={quotaError ? "暂时无法确认工作区容量" : quotaBlocked ? "工作区超过磁盘配额，请先清理文件" : undefined}
        >
          {quotaError ? "容量不可用，无法申请" : quotaBlocked ? "空间超限，无法申请" : "申请 GPU 容器"}
        </button>
      </div>
      {(quotaError || quotaBlocked) && (
        <div className="dashboard-quota-alert">
          {quotaError || (quota && !quota.scan_complete)
            ? "暂时无法确认工作区容量，为安全起见已暂停新容器申请，请稍后重试。"
            : `工作区已使用 ${formatGiB(quota!.usage_bytes)} / ${formatGiB(quota!.quota_bytes)}，请前往工作区清理文件后再申请。`}
        </div>
      )}

      <div className="dashboard-body">
        <div className="dashboard-main">
          {quota && !quota.quota_exempt && (
            <section className="system-load">
              <h2>个人空间</h2>
              <div className="load-cards">
                <div className={`load-card disk-quota-card ${quotaBlocked ? "over" : ""}`}>
                  <span className="load-label">工作区用量</span>
                  <span className="load-value">{formatGiB(quota.usage_bytes)} / {formatGiB(quota.quota_bytes)}</span>
                  <span className="dashboard-quota-meter"><span style={{ width: `${quotaPercent(quota)}%` }} /></span>
                </div>
              </div>
            </section>
          )}

          <section className="dashboard-nodes">
            <h2>计算节点</h2>
            {dashboardNodes.map((node) => (
              <article className={`dashboard-node ${node.online ? "online" : "offline"}`} key={node.node_id}>
                <div className="dashboard-node-header">
                  <div>
                    <strong>{node.node_name}</strong>
                    <code>{node.node_id}</code>
                  </div>
                  <div className="dashboard-node-badges">
                     <span>{node.online ? "在线" : !node.is_local && !remoteLoaded && !remoteError ? "加载中" : "离线"}</span>
                    <span>{node.schedulable ? "可调度" : "不可调度"}</span>
                    <span>{node.container_count} 个容器</span>
                  </div>
                </div>
                {node.public_host && <div className="dashboard-node-host">{node.public_host}</div>}
                 {(node.error || (!node.is_local && !remoteLoaded && remoteError)) && <div className="dashboard-node-error">{node.error || remoteError}</div>}
                {node.system_load && (
                  <div className="load-cards dashboard-node-load">
                    <div className="load-card"><span className="load-label">CPU</span><span className="load-value">{node.system_load.cpu_percent}%</span></div>
                    <div className="load-card"><span className="load-label">内存</span><span className="load-value">{node.system_load.memory_used_gb} / {node.system_load.memory_total_gb} GB ({node.system_load.memory_percent}%)</span></div>
                    <div className="load-card"><span className="load-label">磁盘可用</span><span className="load-value">{node.system_load.disk_free_gb} / {node.system_load.disk_total_gb} GB</span></div>
                  </div>
                )}
                {node.online ? (
                  <GPUTwin gpus={node.gpus} occupancies={node.occupancies} gpuSharing={node.gpu_sharing} />
                ) : (
                   <div className="dashboard-node-unavailable">{!node.is_local && !remoteLoaded && !remoteError ? "正在加载节点资源…" : "节点资源暂不可用"}</div>
                )}
              </article>
            ))}
          </section>

          {/* 全部运行中容器及联系方式 */}
          {data?.all_containers && data.all_containers.length > 0 && (
            <section className="occupancy-table">
              <h2>当前占用（含联系方式，便于联系）</h2>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>类型</th>
                      <th>容器名</th>
                      <th>使用者</th>
                      <th>联系方式</th>
                      <th>开始时间</th>
                      <th>已用时长</th>
                      <th>到期时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.all_containers.map((c, i) => (
                      <tr key={`${c.container_name}-${i}`}>
                        <td>{c.gpu_ids === "CPU" ? "CPU" : `GPU ${c.gpu_ids}`}</td>
                        <td>{c.container_name}</td>
                        <td>{c.real_name || c.display_name || c.username}</td>
                        <td>
                          {c.contact_value ? (
                            <span>{c.contact_type === "wechat" ? "微信 " : "手机 "}{c.contact_value}</span>
                          ) : (
                            "-"
                          )}
                        </td>
                        <td>{c.created_at ? new Date(c.created_at).toLocaleString() : "-"}</td>
                        <td className="duration-cell">
                          {c.duration_hours != null ? `${c.duration_hours} 小时` : "-"}
                        </td>
                        <td>{new Date(c.expires_at).toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

        </div>

        <div className="dashboard-sidebar">
        <aside className="ranking-panel" onMouseEnter={() => setRankHovered(true)} onMouseLeave={() => setRankHovered(false)} onFocusCapture={() => setRankFocused(true)} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setRankFocused(false); }}>
          <h2>{rankCategory === "duration" ? "使用时长排行" : rankCategory === "disk" ? "磁盘使用排行" : "GPU 平均利用率排行 · 估算"}</h2>
          <div className="ranking-tabs" aria-label="排行类型">
            {rankingCategories.map((category) => (
              <button key={category} type="button" className={rankCategory === category ? "active" : ""} aria-pressed={rankCategory === category} onClick={() => setRankCategory(category)}>
                {category === "duration" ? "时长" : category === "disk" ? "磁盘" : "GPU"}
              </button>
            ))}
          </div>
          {rankCategory !== "disk" && (
            <div className="ranking-tabs" aria-label="排行周期">
              <button type="button" className={rankPeriod === "weekly" ? "active" : ""} aria-pressed={rankPeriod === "weekly"} onClick={() => setRankPeriod("weekly")}>本周</button>
              <button type="button" className={rankPeriod === "monthly" ? "active" : ""} aria-pressed={rankPeriod === "monthly"} onClick={() => setRankPeriod("monthly")}>本月</button>
            </div>
          )}
          <p className="ranking-note">{rankCategory === "disk" ? "当前已完成扫描的用户空间快照；非周/月累计" : rankCategory === "gpu" ? "估算：当前整卡利用率 × (0.7 + 0.3 × 显存占比)，按已验证运行占用人数均分，再按窗口内占用时长加权；非历史实测" : "容器累计使用时长"}</p>
          <ul className="ranking-list" key={`${rankCategory}-${rankCategory === "disk" ? "current" : rankPeriod}`}>
            {ranking.length ? (
              ranking.map((r) => (
                <li key={"estimated_percent" in r ? `${r.username}-${r.real_name}` : r.username}>
                  <span className="rank-num">{r.rank}</span>
                  <span className="rank-user">{r.real_name || r.username}</span>
                  <span className="rank-hours">{"usage_bytes" in r ? formatGiB(r.usage_bytes) : "estimated_percent" in r ? `${r.estimated_percent}%` : `${r.total_hours}h`}</span>
                </li>
              ))
            ) : (
              <li className="rank-empty">暂无数据</li>
            )}
          </ul>
        </aside>
        {!!data?.reminder_ranking?.length && (
          <aside className="ranking-panel reminder-panel" aria-labelledby="reminder-heading">
            <h2 id="reminder-heading">提醒ta</h2>
            <ul className="ranking-list">
              {data.reminder_ranking.map((user, index) => (
                <li key={user.username}>
                  <span className="rank-num">{index + 1}</span>
                  <span className="rank-user">{user.real_name || user.username}</span>
                  <span className="rank-hours" aria-label={`${user.unread_count} 条待处理通知`}>{user.unread_count} 条</span>
                </li>
              ))}
            </ul>
          </aside>
        )}
        </div>
      </div>

      {showApply && (
        <ApplyModal
          gpuSharing={data?.gpu_sharing ?? []}
          nodes={dashboardNodes}
          isMaster={isMaster}
          onClose={() => setShowApply(false)}
          onSuccess={() => {
            setShowApply(false);
            loadLocal();
            loadRemote();
          }}
        />
      )}
    </div>
  );
}
