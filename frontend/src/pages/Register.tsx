import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { fetcher } from "../api/client";
import "./Register.css";

export default function Register() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [realName, setRealName] = useState("");
  const [contactType, setContactType] = useState<"phone" | "wechat">("phone");
  const [contactValue, setContactValue] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await fetcher("/auth/register", {
        method: "POST",
        body: JSON.stringify({
          username: username.trim().toLowerCase(),
          password,
          real_name: realName.trim(),
          contact_type: contactType,
          contact_value: contactValue.trim(),
        }),
      });
      setSuccess(true);
      setTimeout(() => navigate("/login"), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "注册失败");
    } finally {
      setLoading(false);
    }
  };

  if (success) {
    return (
      <div className="register-page">
        <div className="register-card">
          <h2>注册成功</h2>
          <p>请等待管理员审批通过后再登录使用。</p>
          <Link to="/login">返回登录</Link>
        </div>
      </div>
    );
  }

  return (
    <div className="register-page">
      <div className="register-card">
        <h1>用户注册</h1>
        <p className="register-subtitle">注册后需管理员审批通过才能使用</p>
        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>用户名（名字全拼）</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value.toLowerCase().replace(/[^a-z0-9\-]/g, ""))}
              placeholder="如 zhangsan、ouyang-xiao"
              required
            />
            <span className="form-hint">请使用小写字母，如 张三→zhangsan</span>
          </div>
          <div className="form-group">
            <label>密码</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={6}
            />
          </div>
          <div className="form-group">
            <label>实名</label>
            <input
              type="text"
              value={realName}
              onChange={(e) => setRealName(e.target.value)}
              placeholder="真实姓名"
              required
            />
          </div>
          <div className="form-group">
            <label>联系方式类型</label>
            <select value={contactType} onChange={(e) => setContactType(e.target.value as "phone" | "wechat")}>
              <option value="phone">手机号</option>
              <option value="wechat">微信号</option>
            </select>
          </div>
          <div className="form-group">
            <label>{contactType === "phone" ? "手机号" : "微信号"}</label>
            <input
              type="text"
              value={contactValue}
              onChange={(e) => setContactValue(e.target.value)}
              placeholder={contactType === "phone" ? "用于他人联系你" : "用于他人联系你"}
              required
            />
          </div>
          {error && <div className="form-error">{error}</div>}
          <button type="submit" className="btn btn-primary" disabled={loading}>
            {loading ? "注册中..." : "注册"}
          </button>
          <p className="register-login">
            已有账号？ <Link to="/login">登录</Link>
          </p>
        </form>
      </div>
    </div>
  );
}
