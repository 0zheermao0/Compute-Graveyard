import { useState, useEffect } from "react";
import { fetcher } from "../api/client";
import ApplyModal from "../components/ApplyModal";
import GPUTwin from "../components/GPUTwin";
import "./Dashboard.css";

interface GPUInfo {
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

interface GpuSharingStatus {
  gpu_index: number;
  occupant_count: number;
  max_sharing: number;
  selectable: boolean;
}

interface DashboardData {
  gpus: GPUInfo[];
  system_load: {
    cpu_percent: number;
    memory_used_gb: number;
    memory_total_gb: number;
    memory_percent: number;
    disk_free_gb: number;
    disk_total_gb: number;
  };
  occupancies: Occupancy[];
  all_containers: RunningContainer[];
  weekly_ranking: UsageRankItem[];
  monthly_ranking: UsageRankItem[];
  gpu_sharing?: GpuSharingStatus[];
  max_gpu_sharing_users?: number;
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState("");
  const [showApply, setShowApply] = useState(false);
  const [rankMode, setRankMode] = useState<"weekly" | "monthly">("weekly");

  const load = async () => {
    try {
      const d = await fetcher<DashboardData>("/dashboard");
      setData({
        ...d,
        weekly_ranking: d.weekly_ranking ?? [],
        monthly_ranking: d.monthly_ranking ?? [],
        gpu_sharing: d.gpu_sharing ?? [],
      });
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  const ranking = rankMode === "weekly" ? (data?.weekly_ranking ?? []) : (data?.monthly_ranking ?? []);

  if (error && !data) {
    return <div className="dashboard-error">加载看板失败: {error}</div>;
  }

  return (
    <div className="dashboard dashboard-twin">
      <div className="dashboard-twin-bg" aria-hidden />
      <div className="dashboard-twin-glow" aria-hidden />
      <div className="dashboard-header">
        <h1>资源看板</h1>
        <button className="btn btn-primary" onClick={() => setShowApply(true)}>
          申请 GPU 容器
        </button>
      </div>

      <div className="dashboard-body">
        <div className="dashboard-main">
          {/* 系统负载 */}
          {data?.system_load && (
            <section className="system-load">
              <h2>系统负载</h2>
              <div className="load-cards">
                <div className="load-card">
                  <span className="load-label">CPU</span>
                  <span className="load-value">{data.system_load.cpu_percent}%</span>
                </div>
                <div className="load-card">
                  <span className="load-label">内存</span>
                  <span className="load-value">
                    {data.system_load.memory_used_gb} / {data.system_load.memory_total_gb} GB ({data.system_load.memory_percent}%)
                  </span>
                </div>
                <div className="load-card">
                  <span className="load-label">工作区可用</span>
                  <span className="load-value">{data.system_load.disk_free_gb} GB</span>
                </div>
              </div>
            </section>
          )}

          {/* GPU 数字孪生 */}
          <GPUTwin gpus={data?.gpus ?? []} occupancies={data?.occupancies ?? []} />

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

        <aside className="ranking-panel">
          <h2>使用时长排行</h2>
          <div className="ranking-tabs">
            <button
              className={rankMode === "weekly" ? "active" : ""}
              onClick={() => setRankMode("weekly")}
            >
              本周
            </button>
            <button
              className={rankMode === "monthly" ? "active" : ""}
              onClick={() => setRankMode("monthly")}
            >
              本月
            </button>
          </div>
          <ul className="ranking-list">
            {ranking.length ? (
              ranking.map((r) => (
                <li key={r.username}>
                  <span className="rank-num">{r.rank}</span>
                  <span className="rank-user">{r.real_name || r.username}</span>
                  <span className="rank-hours">{r.total_hours}h</span>
                </li>
              ))
            ) : (
              <li className="rank-empty">暂无数据</li>
            )}
          </ul>
        </aside>
      </div>

      {showApply && (
        <ApplyModal
          gpuSharing={data?.gpu_sharing ?? []}
          onClose={() => setShowApply(false)}
          onSuccess={() => {
            setShowApply(false);
            load();
          }}
        />
      )}
    </div>
  );
}
