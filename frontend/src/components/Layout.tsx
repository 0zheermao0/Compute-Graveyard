import { useState, useEffect } from "react";
import { Outlet, Link, useLocation, useNavigate } from "react-router-dom";
import "./Layout.css";
import { useAuth } from "../hooks/useAuth";
import InstructionModal from "./InstructionModal";
import { Bell, HelpCircle } from "lucide-react";
import { fetcher } from "../api/client";

const SLOGANS = [
  "正在消耗 GPU 的寿命，换取一份无法收敛的 Loss。",
  "这里不产生智能，只搬运热量。",
  "祝你的过拟合愉快。",
];

const BRAND_EN = "Compute Graveyard";
const BRAND_ZH = "算力坟场";

function SidebarLogoIcon() {
  return (
    <span className="sidebar-logo-icon" title="Stochastic Noise · 高维幻觉生成器">
      <svg viewBox="0 0 40 24" fill="none" className="logo-wave">
        {/* 冷酷直线：在无数噪音中什么也没找着 */}
        <line x1="0" y1="12" x2="40" y2="12" stroke="currentColor" strokeWidth="0.7" opacity="0.5" />
        {/* 混乱波形 */}
        <path d="M0,8 L5,16 L10,6 L15,18 L20,10 L25,14 L30,4 L35,12 L40,8" stroke="currentColor" strokeWidth="1" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M0,16 L4,10 L8,20 L14,8 L18,14 L22,6 L28,16 L34,10 L40,14" stroke="currentColor" strokeWidth="0.8" fill="none" strokeLinecap="round" strokeLinejoin="round" opacity="0.75" />
      </svg>
    </span>
  );
}

export default function Layout() {
  const { user, logout } = useAuth();
  const loc = useLocation();
  const navigate = useNavigate();
  const [sloganIndex, setSloganIndex] = useState(0);
  const [showInstructions, setShowInstructions] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);

  useEffect(() => {
    const t = setInterval(() => setSloganIndex((i) => (i + 1) % SLOGANS.length), 10000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const loadCount = async () => {
      try {
        const data = await fetcher<{ unread_count: number }>("/containers/notifications");
        setUnreadCount(data.unread_count || 0);
      } catch {
        setUnreadCount(0);
      }
    };
    loadCount();
    const id = setInterval(loadCount, 15000);
    return () => clearInterval(id);
  }, []);

  const handleLogout = () => {
    logout();
    navigate("/login", { replace: true });
  };

  const navItems = [
    { to: "/", label: "资源看板" },
    { to: "/my", label: "我的容器" },
    { to: "/workspace", label: "工作区" },
    { to: "/notifications", label: "通知" },
    { to: "/profile", label: "个人资料" },
    ...(user?.role === "admin" ? [{ to: "/admin", label: "管理" }] : []),
  ];

  return (
    <div className="layout">
      {/* 侧边栏 - 桌面端 */}
      <aside className="sidebar">
        <Link to="/" className="sidebar-logo">
          <SidebarLogoIcon />
          <span className="sidebar-logo-text">
            <span className="sidebar-logo-title">{BRAND_EN}</span>
            <span className="sidebar-logo-subtitle">{BRAND_ZH}</span>
          </span>
        </Link>
        <nav className="sidebar-nav">
          {navItems.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className={loc.pathname === item.to ? "active" : ""}
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="sidebar-footer">
          <a
            href="https://github.com/YuhangPei/Compute-Graveyard"
            target="_blank"
            rel="noopener noreferrer"
            className="sidebar-github-link"
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
              padding: "0.5rem 0.75rem",
              color: "rgba(255, 255, 255, 0.7)",
              textDecoration: "none",
              fontSize: "0.85rem",
              transition: "color 0.2s",
              marginBottom: "0.25rem"
            }}
            onMouseEnter={(e: any) => e.currentTarget.style.color = "#fff"}
            onMouseLeave={(e: any) => e.currentTarget.style.color = "rgba(255, 255, 255, 0.7)"}
          >
            <img src="/github_icon.svg" alt="GitHub" style={{ width: "18px", height: "18px" }} />
            <span>GitHub 源码</span>
          </a>
          <button
            className="sidebar-btn-ins"
            onClick={() => setShowInstructions(true)}
          >
            <HelpCircle size={18} />
            <span>使用说明</span>
          </button>
        </div>

        <div className="sidebar-user">
          <div className="sidebar-user-info">
            <span className="user-name">{user?.display_name || user?.username}</span>
            <span className="user-role">{user?.role === "admin" ? "管理员" : "学生"}</span>
          </div>
          <button onClick={handleLogout} className="btn btn-ghost" style={{ marginTop: "0.5rem", width: "100%" }}>
            退出登录
          </button>
        </div>
      </aside>

      {/* 移动端顶栏 */}
      <div className="layout-mobile-header">
        <div className="layout-mobile-header-top">
          <Link to="/" className="sidebar-logo">
            {BRAND_EN} · {BRAND_ZH}
          </Link>
          <span className="layout-mobile-username">
            {user?.display_name || user?.username}
          </span>
        </div>
        <p className="header-slogan header-slogan-mobile">{SLOGANS[sloganIndex]}</p>
        <nav className="layout-mobile-nav">
          {navItems.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className={loc.pathname === item.to ? "active" : ""}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <a
          href="https://github.com/YuhangPei/Compute-Graveyard"
          target="_blank"
          rel="noopener noreferrer"
          className="btn btn-ghost"
          style={{ padding: "0.35rem", display: "flex", alignItems: "center", marginRight: "0.5rem" }}
          title="GitHub 源码"
        >
          <img src="/github_icon.svg" alt="GitHub" style={{ width: "20px", height: "20px" }} />
        </a>
        <button
          className="btn btn-ghost"
          style={{ padding: "0.35rem 0.6rem", fontSize: "0.85rem", marginRight: "1rem" }}
          onClick={() => setShowInstructions(true)}
        >
          说明
        </button>
        <div className="layout-mobile-actions">
          <Link to="/notifications" className="btn btn-ghost" style={{ padding: "0.35rem 0.6rem", fontSize: "0.85rem", marginRight: "0.5rem" }}>
            通知{unreadCount > 0 ? `(${unreadCount > 99 ? "99+" : unreadCount})` : ""}
          </Link>
          <button onClick={handleLogout} className="btn btn-ghost" style={{ padding: "0.35rem 0.6rem", fontSize: "0.85rem" }}>
            退出
          </button>
        </div>
      </div>

      {/* 主内容 */}
      <div className="main-wrap">
        <header className="header">
          <div className="header-inner">
            <p className="header-slogan" title="Stochastic Noise">
              {SLOGANS[sloganIndex]}
            </p>
            <Link to="/notifications" className="header-bell" title="通知中心">
              <Bell size={18} />
              {unreadCount > 0 && <span className="header-bell-badge">{unreadCount > 99 ? "99+" : unreadCount}</span>}
            </Link>
            <Link to="/profile" className="header-user-link" title="个人资料">
              {user?.display_name || user?.username}
            </Link>
            <button onClick={handleLogout} className="btn btn-ghost">退出</button>
          </div>
        </header>
        <main className="main">
          <Outlet />
        </main>
      </div>

      {showInstructions && <InstructionModal onClose={() => setShowInstructions(false)} />}
    </div>
  );
}
