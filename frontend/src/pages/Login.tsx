import { useEffect, useRef, useState } from "react";
import "./Login.css";
import { useNavigate, Link } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { getPasskeyErrorMessage, getPasskeySupportError } from "../api/passkeys";

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [passkeyError, setPasskeyError] = useState("");
  const [passkeyLoading, setPasskeyLoading] = useState(false);
  const operationBusyRef = useRef(false);
  const passkeyAbortRef = useRef<AbortController | null>(null);
  const { login, loginWithPasskey } = useAuth();
  const navigate = useNavigate();
  const supportError = getPasskeySupportError();
  const busy = loading || passkeyLoading;

  useEffect(() => () => passkeyAbortRef.current?.abort(), []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (operationBusyRef.current) return;
    operationBusyRef.current = true;
    setError("");
    setPasskeyError("");
    setLoading(true);
    try {
      await login(username, password);
      navigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setLoading(false);
      operationBusyRef.current = false;
    }
  };

  const handlePasskeyLogin = async () => {
    if (operationBusyRef.current) return;
    setError("");
    setPasskeyError("");
    if (supportError) {
      setPasskeyError(supportError);
      return;
    }
    operationBusyRef.current = true;
    const controller = new AbortController();
    passkeyAbortRef.current = controller;
    setPasskeyLoading(true);
    try {
      await loginWithPasskey(controller.signal);
      if (!controller.signal.aborted) navigate("/");
    } catch (err) {
      if (!controller.signal.aborted) {
        setPasskeyError(getPasskeyErrorMessage(err, "Passkey 登录失败，请重试或使用密码登录。"));
      }
    } finally {
      passkeyAbortRef.current = null;
      if (!controller.signal.aborted) setPasskeyLoading(false);
      operationBusyRef.current = false;
    }
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <h1>Lab-GPU-Manager</h1>
        <p className="login-subtitle">实验室 GPU 资源管理平台</p>
        <form onSubmit={handleSubmit} aria-busy={loading}>
          <div className="form-group">
            <label>用户名</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              disabled={busy}
              required
            />
          </div>
          <div className="form-group">
            <label>密码</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              disabled={busy}
              required
            />
          </div>
          {error && <div className="form-error" role="alert">{error}</div>}
          <button type="submit" className="btn btn-primary login-submit" disabled={busy}>
            {loading ? "登录中..." : "密码登录"}
          </button>
        </form>
        <div className="login-passkey" aria-busy={passkeyLoading}>
          <div className="login-divider">或</div>
          <button type="button" className="btn btn-frosted login-submit" onClick={handlePasskeyLogin} disabled={busy || !!supportError} aria-describedby="passkey-login-hint">
            {passkeyLoading ? "正在验证 Passkey..." : "使用 Passkey 登录"}
          </button>
          <p id="passkey-login-hint" className="login-passkey-hint">{supportError || "使用已绑定的 Passkey，无需输入用户名或密码。"}</p>
          {passkeyError && <div className="form-error" role="alert">{passkeyError}</div>}
        </div>
        <p className="login-register">
          没有账号？ <Link to="/register">注册</Link>
        </p>
      </div>
    </div>
  );
}
