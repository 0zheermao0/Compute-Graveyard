import { useState, useEffect, FormEvent } from "react";
import { fetcher } from "../api/client";
import { useAuth } from "../hooks/useAuth";
import "./Profile.css";

type PersonalToken = {
  id: number;
  name: string;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
};

type CreatedToken = PersonalToken & { token: string };

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
  const [tokens, setTokens] = useState<PersonalToken[]>([]);
  const [tokenName, setTokenName] = useState("");
  const [expiresInDays, setExpiresInDays] = useState(30);
  const [createdToken, setCreatedToken] = useState<CreatedToken | null>(null);
  const [tokenSaving, setTokenSaving] = useState(false);
  const [tokenMessage, setTokenMessage] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    if (!user) return;
    fetcher<PersonalToken[]>("/personal/tokens")
      .then(setTokens)
      .catch((err) => setTokenMessage({ type: "err", text: err instanceof Error ? err.message : "加载令牌失败" }));
  }, [user?.id]);

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

  const handleTokenCreate = async (e: FormEvent) => {
    e.preventDefault();
    setTokenMessage(null);
    setCreatedToken(null);
    setTokenSaving(true);
    try {
      const created = await fetcher<CreatedToken>("/personal/tokens", {
        method: "POST",
        body: JSON.stringify({ name: tokenName.trim(), expires_in_days: expiresInDays }),
      });
      setCreatedToken(created);
      setTokens((current) => [{ id: created.id, name: created.name, created_at: created.created_at, expires_at: created.expires_at, revoked_at: created.revoked_at }, ...current]);
      setTokenName("");
    } catch (err) {
      setTokenMessage({ type: "err", text: err instanceof Error ? err.message : "创建令牌失败" });
    } finally {
      setTokenSaving(false);
    }
  };

  const handleTokenRevoke = async (token: PersonalToken) => {
    if (!window.confirm(`确定撤销令牌「${token.name}」？撤销后使用该令牌的实验监控将立即失效。`)) return;
    setTokenMessage(null);
    try {
      await fetcher(`/personal/tokens/${token.id}`, { method: "DELETE" });
      setTokens((current) => current.map((item) => item.id === token.id ? { ...item, revoked_at: new Date().toISOString() } : item));
      if (createdToken?.id === token.id) setCreatedToken(null);
    } catch (err) {
      setTokenMessage({ type: "err", text: err instanceof Error ? err.message : "撤销令牌失败" });
    }
  };

  const agentInstructions = createdToken ? `先检查当前实验目录的 AGENTS.md 是否被 Git 跟踪或忽略：只有确认文件未被跟踪且已被 Git 忽略时，才能追加以下内容（不要覆盖原有内容）；否则先提醒我，改用不入库的私有文件或环境变量保存 Token，不要写入 AGENTS.md：

## 实验资源监控
- API 地址：${window.location.origin}/api/personal/containers
- 个人 API Token：${createdToken.token}
- 请求方式：GET，设置请求头 Authorization: Bearer <个人 API Token>。
- 此 API 自动按 Token 对应的用户筛选当前运行中的容器，返回 GPU 型号及 ID（index）、GPU 利用率 utilization（%）、显存已用/总量 memory_used_mb/memory_total_mb、显存占用率 memory_percent（%），以及容器的访问 IP/域名 access_host、SSH 端口 ssh_port、SSH 密码 ssh_password 和服务端口 extra_ports。
- 实验运行期间可定期调用该 API 监控资源使用情况和实验状态；GPU 指标是整张卡的实时快照，共用显卡时不代表该容器独占用量，节点离线时指标可能缺失。
- 不要将 Token 写入命令行参数、URL、日志或提交到 Git；AGENTS.md 包含敏感凭据，请确保该文件不会被提交或分享。令牌过期时间：${new Date(createdToken.expires_at).toLocaleString()}。` : "";

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

        <section className="profile-section profile-token-section">
          <h2>个人 API Token</h2>
          <div className="profile-form">
            <p className="profile-hint">用于查询本人容器和 GPU 使用情况。令牌只在创建时显示一次，请妥善保管。</p>
            <form onSubmit={handleTokenCreate}>
              <div className="profile-field">
                <label htmlFor="personal-token-name">令牌名称</label>
                <input id="personal-token-name" className="profile-input" value={tokenName} onChange={(e) => setTokenName(e.target.value)} maxLength={64} placeholder="例如：实验监控" required />
              </div>
              <div className="profile-field">
                <label htmlFor="personal-token-days">有效期（天）</label>
                <input id="personal-token-days" className="profile-input" type="number" min={1} max={365} value={expiresInDays} onChange={(e) => setExpiresInDays(Number(e.target.value))} required />
              </div>
              <button type="submit" className="btn btn-primary profile-submit" disabled={tokenSaving}>{tokenSaving ? "正在创建..." : "新增 Token"}</button>
            </form>
            {tokenMessage && <div className={`profile-message ${tokenMessage.type}`}>{tokenMessage.text}</div>}
            {createdToken && (
              <div className="profile-token-created">
                <h3>新令牌（仅显示一次）</h3>
                <input className="profile-input" type="text" readOnly value={createdToken.token} aria-label="新创建的个人 API Token" onFocus={(e) => e.currentTarget.select()} />
                <p className="profile-hint">以下说明会将令牌交给 Agent，请勿发送到公开聊天或提交到代码仓库。</p>
                <label htmlFor="personal-agent-instructions">复制以下内容发送给实验目录中的 Agent</label>
                <textarea id="personal-agent-instructions" className="profile-input profile-agent-instructions" readOnly value={agentInstructions} onFocus={(e) => e.currentTarget.select()} />
                <button type="button" className="btn btn-frosted profile-submit" onClick={() => navigator.clipboard.writeText(agentInstructions).then(() => setTokenMessage({ type: "ok", text: "说明已复制" })).catch(() => setTokenMessage({ type: "err", text: "复制失败，请手动选择文本" }))}>复制 Agent 说明</button>
                <button type="button" className="btn btn-frosted profile-submit" onClick={() => setCreatedToken(null)}>已保存，关闭令牌显示</button>
              </div>
            )}
            <h3>已创建的令牌</h3>
            {tokens.length === 0 && <p className="profile-hint">尚无令牌</p>}
            <ul className="profile-token-list">
              {tokens.map((token) => (
                <li key={token.id}>
                  <div><strong>{token.name}</strong><span className="profile-hint">到期：{new Date(token.expires_at).toLocaleString()}{token.revoked_at ? " · 已撤销" : new Date(token.expires_at) <= new Date() ? " · 已过期" : ""}</span></div>
                  {!token.revoked_at && <button type="button" className="btn btn-frosted" onClick={() => handleTokenRevoke(token)}>撤销</button>}
                </li>
              ))}
            </ul>
          </div>
        </section>
      </div>
    </div>
  );
}
