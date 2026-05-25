import { useState, useEffect, FormEvent } from "react";
import { fetcher } from "../api/client";
import { useAuth } from "../hooks/useAuth";
import "./Profile.css";

export default function Profile() {
  const { user, refresh } = useAuth();

  // 个人信息状态
  const [displayName, setDisplayName] = useState("");
  const [realName, setRealName] = useState("");
  const [contactType, setContactType] = useState<"phone" | "wechat">("wechat");
  const [contactValue, setContactValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  // 修改密码状态
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [pwSaving, setPwSaving] = useState(false);
  const [pwMessage, setPwMessage] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    if (user) {
      setDisplayName(user.display_name || "");
      setRealName(user.real_name ?? "");
      setContactType((user.contact_type === "phone" ? "phone" : "wechat") as "phone" | "wechat");
      setContactValue(user.contact_value ?? "");
    }
  }, [user]);

  const handleProfileSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setMessage(null);
    setSaving(true);
    try {
      await fetcher("/auth/me", {
        method: "PATCH",
        body: JSON.stringify({
          display_name: displayName.trim() || undefined,
          real_name: realName.trim() || undefined,
          contact_type: contactType,
          contact_value: contactValue.trim() || undefined,
        }),
      });
      await refresh();
      setMessage({ type: "ok", text: "个人资料已更新" });
    } catch (err) {
      setMessage({ type: "err", text: err instanceof Error ? err.message : "更新失败" });
    } finally {
      setSaving(false);
    }
  };

  const handlePasswordSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setPwMessage(null);

    if (newPassword !== confirmPassword) {
      setPwMessage({ type: "err", text: "两次输入的新密码不一致" });
      return;
    }
    if (newPassword.length < 6) {
      setPwMessage({ type: "err", text: "新密码长度至少为 6 位" });
      return;
    }

    setPwSaving(true);
    try {
      await fetcher("/auth/password", {
        method: "POST",
        body: JSON.stringify({
          old_password: oldPassword,
          new_password: newPassword,
        }),
      });
      setPwMessage({ type: "ok", text: "密码修改成功" });
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setPwMessage({ type: "err", text: err instanceof Error ? err.message : "修改失败" });
    } finally {
      setPwSaving(false);
    }
  };

  if (!user) return null;

  return (
    <div className="profile-page">
      <h1>个人资料</h1>

      <div className="profile-container">
        {/* 基本信息部分 */}
        <section className="profile-section">
          <h2>基本信息</h2>
          <form className="profile-form" onSubmit={handleProfileSubmit}>
            <div className="profile-field">
              <label>用户名</label>
              <input type="text" value={user.username} disabled className="profile-input disabled" />
              <span className="profile-hint">用户名由系统通过拼音生成，不可修改</span>
            </div>
            <div className="profile-field">
              <label>显示名称</label>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="用于界面展示的名称"
                className="profile-input"
                maxLength={64}
              />
            </div>
            <div className="profile-field">
              <label>实名</label>
              <input
                type="text"
                value={realName}
                onChange={(e) => setRealName(e.target.value)}
                placeholder="真实姓名"
                className="profile-input"
                maxLength={64}
              />
            </div>
            <div className="profile-field">
              <label>联系方式类型</label>
              <select
                value={contactType}
                onChange={(e) => setContactType(e.target.value as "phone" | "wechat")}
                className="profile-input"
              >
                <option value="wechat">微信</option>
                <option value="phone">手机号</option>
              </select>
            </div>
            <div className="profile-field">
              <label>{contactType === "wechat" ? "微信号" : "手机号"}</label>
              <input
                type="text"
                value={contactValue}
                onChange={(e) => setContactValue(e.target.value)}
                placeholder={contactType === "wechat" ? "微信号" : "手机号"}
                className="profile-input"
                maxLength={64}
              />
            </div>
            {message && (
              <div className={`profile-message ${message.type}`}>{message.text}</div>
            )}
            <button type="submit" className="btn btn-primary profile-submit" disabled={saving}>
              {saving ? "正在更新..." : "更新资料"}
            </button>
          </form>
        </section>

        {/* 修改密码部分 */}
        <section className="profile-section">
          <h2>修改密码</h2>
          <form className="profile-form" onSubmit={handlePasswordSubmit}>
            <div className="profile-field">
              <label>原密码</label>
              <input
                type="password"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                placeholder="请输入当前密码"
                className="profile-input"
                required
              />
            </div>
            <div className="profile-field">
              <label>新密码</label>
              <input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                placeholder="请输入新密码（至少6位）"
                className="profile-input"
                required
              />
            </div>
            <div className="profile-field">
              <label>确认新密码</label>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="请再次输入新密码"
                className="profile-input"
                required
              />
            </div>
            {pwMessage && (
              <div className={`profile-message ${pwMessage.type}`}>{pwMessage.text}</div>
            )}
            <button type="submit" className="btn btn-frosted profile-submit" disabled={pwSaving}>
              {pwSaving ? "正在修改..." : "修改密码"}
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}
