import { useRef } from "react";
import { motion, useAnimationFrame } from "motion/react";
import { User, Clock, Activity, Zap, Thermometer } from "lucide-react";
import "./GPUTwin.css";

export interface GPUInfo {
  index: number;
  name: string;
  memory_used_mb: number | null;
  memory_total_mb: number | null;
  memory_percent: number | null;
  temperature?: number | null;
  utilization?: number | null;
}

export interface Occupancy {
  gpu_index: number;
  container_name: string;
  username: string;
  display_name: string;
  real_name?: string | null;
  contact_type?: string | null;
  contact_value?: string | null;
  expires_at: string;
  duration_hours?: number | null;
  ssh_port?: number;
}

interface TwinGPUData {
  id: number;
  model: string;
  utilization: number;
  power: number;
  user: string;
  contact?: string;
  isOccupied: boolean;
  uptime: string;
  temp: number;
  memoryPercent: number;
}

function buildTwinData(gpus: GPUInfo[], occupancies: Occupancy[]): TwinGPUData[] {
  return gpus.map((gpu) => {
    const occ = occupancies.find((o) => o.gpu_index === gpu.index);
    const util = gpu.utilization ?? gpu.memory_percent ?? 0;
    const power = Math.round((util / 100) * 350);
    const contactInfo = occ?.contact_value
      ? `(${occ.contact_type === "wechat" ? "微信" : "手机"}: ${occ.contact_value})`
      : "";
    return {
      id: gpu.index,
      model: gpu.name,
      utilization: Math.min(100, util),
      power,
      user: occ ? occ.real_name || occ.display_name || occ.username : "空闲",
      contact: contactInfo,
      isOccupied: !!occ,
      uptime: occ?.duration_hours != null ? `${occ.duration_hours}h` : "-",
      temp: gpu.temperature ?? 0,
      memoryPercent: gpu.memory_percent ?? 0,
    };
  });
}

/* 风扇：用 useAnimationFrame 根据 utilization 控制转速，100 speed ≈ 1000 deg/sec */
const Fan = ({ speed }: { speed: number }) => {
  const ref = useRef<HTMLDivElement>(null);
  const rotationRef = useRef(0);

  useAnimationFrame((_, delta) => {
    if (!ref.current) return;
    const degreesPerSecond = speed * 10;
    rotationRef.current += degreesPerSecond * (delta / 1000);
    ref.current.style.transform = `rotate(${rotationRef.current}deg)`;
  });

  return (
    <div className="twin-fan-wrap">
      <div className="twin-fan-hub" />
      <div ref={ref} className="twin-fan-blades">
        <svg viewBox="0 0 100 100">
          {[0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => (
            <g key={i} transform={`rotate(${i * (360 / 9)} 50 50)`}>
              <path
                d="M50,50 C65,35 70,15 50,5 C40,15 45,35 50,50"
                fill="currentColor"
                stroke="rgba(255,255,255,0.05)"
                strokeWidth="0.5"
              />
            </g>
          ))}
        </svg>
      </div>
    </div>
  );
};

/* 风扇旋转 keyframes 在 CSS 中定义 */
const GPUVisual = ({ data }: { data: TwinGPUData }) => {
  const isIdle = !data.isOccupied;
  const powerColor = data.power > 300 ? "#ef4444" : data.power > 100 ? "#10b981" : "#00ffcc";

  return (
    <div className="twin-gpu-visual">
      <div className="twin-gpu-cables">
        <svg viewBox="0 0 100 50" preserveAspectRatio="none">
          <path d="M0,15 C40,15 60,25 100,25" stroke="#27272a" strokeWidth="6" fill="none" strokeLinecap="round" />
          <path d="M0,35 C40,35 60,25 100,25" stroke="#27272a" strokeWidth="6" fill="none" strokeLinecap="round" />
          {!isIdle && (
            <>
              <path
                d="M0,15 C40,15 60,25 100,25"
                stroke={powerColor}
                strokeWidth="2"
                fill="none"
                strokeDasharray="10 20"
                className="twin-cable-flow"
                style={{ animationDuration: `${Math.max(0.1, 100 / data.power)}s` }}
              />
              <path
                d="M0,35 C40,35 60,25 100,25"
                stroke={powerColor}
                strokeWidth="2"
                fill="none"
                strokeDasharray="10 20"
                className="twin-cable-flow"
                style={{ animationDuration: `${Math.max(0.1, 100 / data.power)}s` }}
              />
            </>
          )}
        </svg>
      </div>
      <div className="twin-gpu-heatsink" />
      <div className="twin-gpu-edge">{data.model}</div>
      <div className="twin-gpu-pcie" />
      <Fan speed={data.utilization} />
      <Fan speed={data.utilization} />
      <Fan speed={data.utilization} />
      <div
        className={`twin-gpu-led ${data.temp > 80 ? "critical" : !data.isOccupied ? "idle" : "active"}`}
      />
    </div>
  );
};

const ConnectionLine = ({ data }: { data: TwinGPUData }) => {
  const isActive = data.isOccupied;
  const color = data.temp > 80 ? "#ef4444" : isActive ? "#10b981" : "#52525b";

  return (
    <div className="twin-conn">
      <svg viewBox="0 0 100 10" preserveAspectRatio="none">
        <path d="M0,5 L100,5" stroke={isActive ? `${color}40` : "#3f3f46"} strokeWidth="2" fill="none" />
        {isActive && (
          <path
            d="M0,5 L100,5"
            stroke={color}
            strokeWidth="2"
            fill="none"
            strokeDasharray="10 20"
            className="twin-conn-active"
            style={{ filter: `drop-shadow(0 0 4px ${color})` }}
          />
        )}
        <circle cx="0" cy="5" r="3" fill={color} />
        <circle cx="100" cy="5" r="3" fill={color} />
      </svg>
    </div>
  );
};

const InfoCard = ({ data }: { data: TwinGPUData }) => {
  const isActive = data.isOccupied;
  const isCritical = data.temp > 80;
  const statusClass = isCritical ? "critical" : isActive ? "active" : "idle";
  const statusText = isCritical ? "高温" : isActive ? "占用" : "空闲";

  return (
    <div className="twin-card glass-card">
      <div className={`twin-card-glow ${statusClass}`} />
      <div className="twin-card-header">
        <div>
          <div className="twin-card-model">{data.model}</div>
          <div className="twin-card-id">GPU-{data.id}</div>
        </div>
        <div className={`twin-card-badge ${statusClass}`}>{statusText}</div>
      </div>
      <div className="twin-card-row">
        <span><User size={12} /> 用户</span>
        <span title={data.user + " " + (data.contact || "")}>
          {data.user} <small style={{ opacity: 0.7, fontSize: '0.7rem' }}>{data.contact}</small>
        </span>
      </div>
      <div className="twin-card-row">
        <span><Clock size={12} /> 时长</span>
        <span>{data.uptime}</span>
      </div>
      <div className="twin-card-bar-wrap">
        <div className="twin-card-bar-label">
          <span><Activity size={12} /> 利用率</span>
          <span>{data.utilization}%</span>
        </div>
        <div className="twin-card-bar-bg">
          <motion.div
            className={`twin-card-bar-fill ${data.utilization > 90 ? "twin-bar-red" : "twin-bar-green"}`}
            initial={false}
            animate={{ width: `${data.utilization}%` }}
            transition={{ duration: 0.5 }}
          />
        </div>
      </div>
      <div className="twin-card-bar-wrap">
        <div className="twin-card-bar-label">
          <span><Zap size={12} /> 显存</span>
          <span>{data.memoryPercent}%</span>
        </div>
        <div className="twin-card-bar-bg">
          <motion.div
            className="twin-card-bar-fill twin-bar-cyan"
            initial={false}
            animate={{ width: `${data.memoryPercent}%` }}
            transition={{ duration: 0.5 }}
          />
        </div>
      </div>
      <div className={`twin-card-temp ${isCritical ? "critical" : ""}`}>
        <span><Thermometer size={12} /> 温度</span>
        <span>{data.temp > 0 ? `${data.temp}°C` : "-"}</span>
      </div>
    </div>
  );
};

/* GPUTwin.css 中需有 .twin-bar-green, .twin-bar-red, .twin-bar-cyan 和 @keyframes twin-flow-right */
export default function GPUTwin({
  gpus,
  occupancies,
}: {
  gpus: GPUInfo[];
  occupancies: Occupancy[];
}) {
  const list = buildTwinData(gpus, occupancies);

  if (!gpus.length) {
    return (
      <section className="twin-wrap">
        <h2 className="gpu-section">GPU 资源</h2>
        <div className="twin-no-gpu">暂无 GPU 信息（需宿主机支持 nvidia-smi）</div>
      </section>
    );
  }

  return (
    <section className="twin-wrap">
      <div className="twin-grid-bg" />
      <div className="twin-glow" />
      <div className="twin-motherboard">
        <svg preserveAspectRatio="none">
          <path d="M100,0 L100,1000 M500,0 L500,1000 M900,0 L900,1000" stroke="#00ffcc" strokeWidth="1" fill="none" strokeDasharray="4 4" />
        </svg>
      </div>
      <div className="twin-grid">
        {list.map((data) => (
          <div key={data.id} className="twin-row">
            <GPUVisual data={data} />
            <ConnectionLine data={data} />
            <InfoCard data={data} />
          </div>
        ))}
      </div>
    </section>
  );
}
