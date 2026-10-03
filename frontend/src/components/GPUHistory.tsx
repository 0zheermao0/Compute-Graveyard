import { useEffect, useMemo, useState } from "react";
import { fetcher } from "../api/client";
import type { DashboardNode } from "../pages/Dashboard";
import "./GPUHistory.css";

interface HistoryPoint {
  timestamp: string;
  utilization: number | null;
  memory_percent: number | null;
  memory_used_mb: number | null;
  memory_total_mb: number | null;
}
interface HistorySeries {
  node_id: string;
  node_name: string;
  gpu_index: number;
  gpu_name: string;
  origin: string;
  username: string;
  display_name: string;
  real_name?: string | null;
  points: HistoryPoint[];
}
interface HistoryResponse {
  since: string;
  until: string;
  interval_minutes: number;
  series: HistorySeries[];
}
const DAY = 24 * 60 * 60 * 1000;
const GAP = 8 * 60 * 1000;
const LEFT = 22;
const RIGHT = 214;
const TOP = 8;
const BOTTOM = 76;
const percent = (value: number | null) => value != null && Number.isFinite(value) ? `${Math.round(value)}%` : "—";
const timeLabel = (time: number) => new Date(time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
const memory = (value: number | null) => value != null && Number.isFinite(value) ? `${Math.round(value)} MiB (${(value / 1024).toFixed(1)} GiB)` : "—";

function HistoryCard({ series, now }: { series: HistorySeries; now: number }) {
  const start = now - DAY;
  const [selectedTime, setSelectedTime] = useState<number | null>(null);
  const points = useMemo(() => series.points
    .map((point) => ({ ...point, time: Date.parse(point.timestamp) }))
    .filter((point) => Number.isFinite(point.time) && point.time >= start && point.time <= now)
    .sort((a, b) => a.time - b.time), [series.points, start, now]);
  const x = (time: number) => LEFT + (time - start) / DAY * (RIGHT - LEFT);
  const y = (value: number) => BOTTOM - Math.max(0, Math.min(100, value)) / 100 * (BOTTOM - TOP);
  const path = (metric: "utilization" | "memory_percent") => {
    let previous: number | null = null;
    return points.map((point) => {
      const value = point[metric];
      if (value == null || !Number.isFinite(value)) { previous = null; return ""; }
      const command = previous == null || point.time - previous > GAP ? "M" : "L";
      previous = point.time;
      // A tiny horizontal mark makes isolated samples visible without connecting gaps.
      const position = `${x(point.time).toFixed(2)},${y(value).toFixed(2)}`;
      return `${command}${position}${command === "M" ? ` l0.5,0 M${position}` : ""}`;
    }).join(" ");
  };
  const latest = points[points.length - 1];
  const latestValid = [...points].reverse().find((point) => point.utilization != null || point.memory_percent != null);
  const stale = !latestValid || now - latestValid.time > GAP;
  const nearest = selectedTime == null ? latest : points.reduce<typeof latest | undefined>((best, point) =>
    !best || Math.abs(point.time - selectedTime) < Math.abs(best.time - selectedTime) ? point : best, undefined);
  const selected = selectedTime != null && nearest && Math.abs(nearest.time - selectedTime) > GAP ? undefined : nearest;
  const name = series.real_name || series.display_name || series.username || "未知用户";
  const deviceDetails = `${series.node_name || series.node_id} (${series.node_id}) · ${series.gpu_name} · 来源 ${series.origin}`;
  const sampleDetails = selected
    ? `${new Date(selected.timestamp).toLocaleString()} · 利用率 ${percent(selected.utilization)} · 显存 ${percent(selected.memory_percent)} · ${memory(selected.memory_used_mb)} / ${memory(selected.memory_total_mb)}`
    : "此时段暂无采样";
  const compactMemory = selected?.memory_used_mb != null && selected.memory_total_mb != null
    ? `${(selected.memory_used_mb / 1024).toFixed(1)} / ${(selected.memory_total_mb / 1024).toFixed(1)} GiB`
    : "显存用量不可用";
  return (
    <article className="gpu-history-card">
      <div className="gpu-history-card-heading">
        <strong title={`${name} (${series.username}) · 来源 ${series.origin}`}>{name}</strong>
        <span className="gpu-history-gpu" title={deviceDetails}>GPU {series.gpu_index}</span>
        <span className={`gpu-history-status ${stale ? "gpu-history-stale" : "gpu-history-live"}`} role="img" aria-label={stale ? "采样过期或缺失" : "采样已更新"} title={stale ? "采样过期或缺失" : "采样已更新"} />
      </div>
      <div className="gpu-history-identity" title={deviceDetails}>{series.node_name || series.node_id}</div>
      <svg className="gpu-history-chart" viewBox="0 0 220 96" role="img" tabIndex={0}
        aria-label={`${name}，${series.node_name} GPU ${series.gpu_index}，过去24小时整卡利用率与显存占比。左右方向键查看采样。`}
        onPointerMove={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          const position = (event.clientX - box.left) / box.width * 220;
          setSelectedTime(start + Math.max(0, Math.min(1, (position - LEFT) / (RIGHT - LEFT))) * DAY);
        }}
        onPointerLeave={() => setSelectedTime(null)}
        onBlur={() => setSelectedTime(null)}
        onKeyDown={(event) => {
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
          event.preventDefault();
          const index = nearest ? points.indexOf(nearest) : points.length - 1;
          const next = points[Math.max(0, Math.min(points.length - 1, index + (event.key === "ArrowLeft" ? -1 : 1)))];
          if (next) setSelectedTime(next.time);
        }}>
        <title>{sampleDetails}</title>
        {[0, 50, 100].map((value) => <g key={value}><line x1={LEFT} x2={RIGHT} y1={y(value)} y2={y(value)} className="gpu-history-gridline" /><text x={LEFT - 4} y={y(value) + 3} textAnchor="end">{value}</text></g>)}
        <path d={path("utilization")} className="gpu-history-util-line" />
        <path d={path("memory_percent")} className="gpu-history-mem-line" />
        {selectedTime != null && selected && <line x1={x(selected.time)} x2={x(selected.time)} y1={TOP} y2={BOTTOM} className="gpu-history-cursor" />}
        <text x={LEFT} y="92">−24h</text><text x={(LEFT + RIGHT) / 2} y="92" textAnchor="middle">−12h</text><text x={RIGHT} y="92" textAnchor="end">现在</text>
      </svg>
      {selectedTime != null && (
        <div className="gpu-history-tooltip" role="status">
          {selected ? <><time dateTime={selected.timestamp}>{timeLabel(selected.time)}</time><span>{compactMemory}</span></> : "暂无采样"}
        </div>
      )}
      <div className="gpu-history-readout" title={sampleDetails}>
        <span className="gpu-history-util"><i aria-hidden />利用 <strong>{percent(selected?.utilization ?? null)}</strong></span>
        <span className="gpu-history-mem"><i aria-hidden />显存 <strong>{percent(selected?.memory_percent ?? null)}</strong></span>
      </div>
    </article>
  );
}

interface GPUHistoryProps {
  visible?: boolean;
  // Only nodes with a successful live snapshot may remove stale associations.
  activeNodes?: DashboardNode[];
}

export default function GPUHistory({ visible = true, activeNodes }: GPUHistoryProps) {
  const [data, setData] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    let active = true;
    let pending = false;
    const controller = new AbortController();
    const load = async () => {
      setNow(Date.now());
      if (pending) return;
      pending = true;
      try {
        const result = await fetcher<HistoryResponse>("/dashboard/gpu-history", { signal: controller.signal });
        if (active) { setData(result); setError(""); setNow(Date.now()); }
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : "加载失败");
      } finally { pending = false; }
    };
    if (visible) void load();
    const timer = setInterval(() => { void load(); }, 10000);
    return () => { active = false; controller.abort(); clearInterval(timer); };
  }, [visible]);
  const series = useMemo(() => {
    const nodes = new Map(activeNodes?.map((node) => [node.node_id, node]));
    return (data?.series ?? []).filter((item) => {
      const node = nodes.get(item.node_id);
      // Unloaded/offline remote nodes are not evidence that a container was released.
      if (!node) return true;
      return node.occupancies.some((occupancy) =>
        occupancy.username === item.username && occupancy.gpu_index === item.gpu_index
        && (occupancy.origin == null || occupancy.origin === item.origin));
    });
  }, [data, activeNodes]);
  return (
    <div className="gpu-history">
      {error && <p className="gpu-history-error" role="status">刷新失败{data ? "，保留上次数据" : ""}：{error}</p>}
      {!data && !error && <p className="gpu-history-empty" role="status">正在加载 GPU 历史…</p>}
      {data && !series.length && <p className="gpu-history-empty">暂无活跃 GPU 使用记录</p>}
      {!!series.length && <div className="gpu-history-list" tabIndex={0} aria-label="GPU 历史图列表，滚动查看更多">{series.map((item) => <HistoryCard key={JSON.stringify([item.username, item.node_id, item.gpu_index, item.origin])} series={item} now={now} />)}</div>}
    </div>
  );
}
