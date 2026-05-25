import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetcher } from "../api/client";
import "./MyContainers.css";
import "./Admin.css";

type NotificationType = "share_approval_request" | "lease_renew_reminder_1d" | "share_waiting_for_others";

interface NotificationItem {
  id: string;
  type: NotificationType;
  title: string;
  message: string;
  created_at: string;
  container_id?: number | null;
  container_name?: string | null;
  gpu_ids?: string | null;
}

interface NotificationResponse {
  unread_count: number;
  items: NotificationItem[];
}

export default function Notifications() {
  const navigate = useNavigate();
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = async () => {
    try {
      const data = await fetcher<NotificationResponse>("/containers/notifications");
      setItems(data.items || []);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, []);

  const grouped = useMemo(() => {
    return items.map((item) => {
      if (item.type === "share_approval_request") return { ...item, category: "审批", priority: 1 };
      if (item.type === "lease_renew_reminder_1d") return { ...item, category: "续租", priority: 2 };
      return { ...item, category: "进度", priority: 3 };
    }).sort((a, b) => {
      if (a.priority !== b.priority) return a.priority - b.priority;
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });
  }, [items]);

  const handleApprove = async (item: NotificationItem) => {
    if (!item.container_id) return;
    setBusyId(item.id);
    try {
      await fetcher(`/containers/${item.container_id}/approve-share`, { method: "POST", body: "{}" });
      await load();
    } catch (e) {
      alert(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  };

  const handleReject = async (item: NotificationItem) => {
    if (!item.container_id) return;
    if (!confirm("确定拒绝该 GPU 共用申请？")) return;
    setBusyId(item.id);
    try {
      await fetcher(`/containers/${item.container_id}/reject-share`, { method: "POST", body: "{}" });
      await load();
    } catch (e) {
      alert(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  };

  if (loading) return <div className="loading">加载中...</div>;
  if (error) return <div className="my-error">通知加载失败: {error}</div>;

  return (
    <div className="my-containers my-containers-twin">
      <div className="my-containers-bg" aria-hidden />
      <div className="my-containers-glow" aria-hidden />
      <h1>通知中心</h1>

      {items.length === 0 ? (
        <p className="empty">暂无通知</p>
      ) : (
        <section className="admin-section">
          <h2>通知列表</h2>
          <table className="admin-table">
            <thead>
              <tr>
                <th>类型</th>
                <th>标题</th>
                <th>内容</th>
                <th>时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {grouped.map((n) => (
                <tr key={n.id}>
                  <td>{n.category}</td>
                  <td>{n.title}</td>
                  <td style={{ maxWidth: 460 }}>{n.message}</td>
                  <td>{new Date(n.created_at).toLocaleString()}</td>
                  <td>
                    {n.type === "share_approval_request" ? (
                      <>
                        <button className="btn btn-primary btn-sm" disabled={busyId === n.id} onClick={() => handleApprove(n)}>
                          {busyId === n.id ? "处理中..." : "同意"}
                        </button>
                        <button
                          className="btn btn-frosted btn-sm"
                          style={{ marginLeft: "0.5rem" }}
                          disabled={busyId === n.id}
                          onClick={() => handleReject(n)}
                        >
                          拒绝
                        </button>
                      </>
                    ) : (
                      <button className="btn btn-frosted btn-sm" onClick={() => navigate("/my")}>
                        前往我的容器
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
