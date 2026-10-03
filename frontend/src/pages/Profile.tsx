import { useState, useEffect, FormEvent } from "react";
import {
  UserRound,
  ShieldCheck,
  KeyRound,
  ChevronRight,
  Copy,
  Plus,
  Terminal,
} from "lucide-react";
import { fetcher } from "../api/client";
import { useAuth } from "../hooks/useAuth";
import PasskeyManager from "../components/PasskeyManager";
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

  const [activeModule, setActiveModule] = useState<
    "personal" | "security" | "api"
  >("personal");
  const [tokensLoading, setTokensLoading] = useState(true);
  const [tokensError, setTokensError] = useState("");
  const [tokenReload, setTokenReload] = useState(0);
  const [revokingId, setRevokingId] = useState<number | null>(null);

  // 个人信息状态
  const [displayName, setDisplayName] = useState("");
  const [realName, setRealName] = useState("");
  const [contactType, setContactType] = useState<"phone" | "wechat">("wechat");
  const [contactValue, setContactValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{
    type: "ok" | "err";
    text: string;
  } | null>(null);

  // 修改密码状态
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [pwSaving, setPwSaving] = useState(false);
  const [pwMessage, setPwMessage] = useState<{
    type: "ok" | "err";
    text: string;
  } | null>(null);
  const [tokens, setTokens] = useState<PersonalToken[]>([]);
  const [tokenName, setTokenName] = useState("");
  const [expiresInDays, setExpiresInDays] = useState(30);
  const [createdToken, setCreatedToken] = useState<CreatedToken | null>(null);
  const [tokenSaving, setTokenSaving] = useState(false);
  const [tokenMessage, setTokenMessage] = useState<{
    type: "ok" | "err";
    text: string;
  } | null>(null);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    setTokensLoading(true);
    setTokensError("");
    fetcher<PersonalToken[]>("/personal/tokens")
      .then((items) => {
        if (!cancelled) setTokens(items);
      })
      .catch((err) => {
        if (!cancelled)
          setTokensError(err instanceof Error ? err.message : "加载令牌失败");
      })
      .finally(() => {
        if (!cancelled) setTokensLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [user?.id, tokenReload]);

  useEffect(() => {
    if (user) {
      setDisplayName(user.display_name || "");
      setRealName(user.real_name ?? "");
      setContactType(
        (user.contact_type === "phone" ? "phone" : "wechat") as
          | "phone"
          | "wechat",
      );
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
      setMessage({
        type: "err",
        text: err instanceof Error ? err.message : "更新失败",
      });
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
      setPwMessage({
        type: "err",
        text: err instanceof Error ? err.message : "修改失败",
      });
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
        body: JSON.stringify({
          name: tokenName.trim(),
          expires_in_days: expiresInDays,
        }),
      });
      setCreatedToken(created);
      setTokens((current) => [
        {
          id: created.id,
          name: created.name,
          created_at: created.created_at,
          expires_at: created.expires_at,
          revoked_at: created.revoked_at,
        },
        ...current,
      ]);
      setTokenName("");
    } catch (err) {
      setTokenMessage({
        type: "err",
        text: err instanceof Error ? err.message : "创建令牌失败",
      });
    } finally {
      setTokenSaving(false);
    }
  };

  const handleTokenRevoke = async (token: PersonalToken) => {
    if (revokingId !== null) return;
    if (
      !window.confirm(
        `确定撤销令牌「${token.name}」？撤销后使用该令牌的实验监控将立即失效。`,
      )
    )
      return;
    setTokenMessage(null);
    setRevokingId(token.id);
    try {
      await fetcher(`/personal/tokens/${token.id}`, { method: "DELETE" });
      setTokens((current) =>
        current.map((item) =>
          item.id === token.id
            ? { ...item, revoked_at: new Date().toISOString() }
            : item,
        ),
      );
      if (createdToken?.id === token.id) setCreatedToken(null);
    } catch (err) {
      setTokenMessage({
        type: "err",
        text: err instanceof Error ? err.message : "撤销令牌失败",
      });
    } finally {
      setRevokingId(null);
    }
  };

  const copyTokenText = async (text: string, successMessage: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setTokenMessage({ type: "ok", text: successMessage });
    } catch {
      setTokenMessage({ type: "err", text: "复制失败，请手动选择文本" });
    }
  };

  const agentInstructions = createdToken
    ? `先检查当前实验目录的 AGENTS.md 是否被 Git 跟踪或忽略：只有确认文件未被跟踪且已被 Git 忽略时，才能追加以下内容（不要覆盖原有内容）；否则先提醒我，改用不入库的私有文件或环境变量保存 Token，不要写入 AGENTS.md：

## 实验资源监控
- API 地址：${window.location.origin}/api/personal/containers
- 个人 API Token：${createdToken.token}
- 请求方式：GET，设置请求头 Authorization: Bearer <个人 API Token>。
- 此 API 自动按 Token 对应的用户筛选当前运行中的容器，返回 GPU 型号及 ID（index）、GPU 利用率 utilization（%）、显存已用/总量 memory_used_mb/memory_total_mb、显存占用率 memory_percent（%），以及容器的访问 IP/域名 access_host、SSH 端口 ssh_port、SSH 密码 ssh_password 和服务端口 extra_ports。
- 实验运行期间可定期调用该 API 监控资源使用情况和实验状态；GPU 指标是整张卡的实时快照，共用显卡时不代表该容器独占用量，节点离线时指标可能缺失。
- 不要将 Token 写入命令行参数、URL、日志或提交到 Git；AGENTS.md 包含敏感凭据，请确保该文件不会被提交或分享。令牌过期时间：${new Date(createdToken.expires_at).toLocaleString()}。`
    : "";

  if (!user) return null;

  const modules = [
    {
      id: "personal",
      title: "个人资料",
      description: "身份与联系方式",
      icon: UserRound,
    },
    {
      id: "security",
      title: "账户安全",
      description: "密码与 Passkey",
      icon: ShieldCheck,
    },
    {
      id: "api",
      title: "API 访问",
      description: "令牌与实验监控",
      icon: Terminal,
    },
  ] as const;
  const activeTokens = tokens.filter(
    (token) => !token.revoked_at && new Date(token.expires_at) > new Date(),
  ).length;
  const profileDirty =
    displayName !== (user.display_name || "") ||
    realName !== (user.real_name ?? "") ||
    contactType !== (user.contact_type === "phone" ? "phone" : "wechat") ||
    contactValue !== (user.contact_value ?? "");

  return (
    <div className="profile-page">
      <header className="profile-page-header">
        <span className="profile-eyebrow">ACCOUNT SETTINGS</span>
        <h1>个人中心</h1>
        <p>管理你的个人信息、登录方式与开发者访问权限。</p>
      </header>

      <div className="profile-layout">
        <aside className="profile-sidebar">
          <div className="profile-identity">
            <div className="profile-avatar" aria-hidden="true">
              {(user.display_name || user.username).slice(0, 1).toUpperCase()}
            </div>
            <strong>{user.display_name || user.username}</strong>
            <span className="profile-username">@{user.username}</span>
            <span className="profile-role">
              {user.role === "admin" ? "管理员" : "用户"}
            </span>
          </div>
          <nav className="profile-nav" aria-label="个人中心模块">
            {modules.map(({ id, title, description, icon: Icon }) => (
              <button
                key={id}
                type="button"
                className={activeModule === id ? "active" : ""}
                aria-pressed={activeModule === id}
                aria-controls={`profile-${id}`}
                onClick={() => setActiveModule(id)}
              >
                <Icon size={19} aria-hidden="true" />
                <span>
                  <strong>{title}</strong>
                  <small>{description}</small>
                </span>
                <ChevronRight size={16} aria-hidden="true" />
              </button>
            ))}
          </nav>
          <p className="profile-sidebar-note">
            <ShieldCheck size={15} aria-hidden="true" />
            密码与 Passkey 操作需验证密码
          </p>
        </aside>

        <div className="profile-container">
          <div id="profile-personal" hidden={activeModule !== "personal"}>
            <section className="profile-section">
              <div className="profile-card-heading">
                <UserRound size={19} aria-hidden="true" />
                <div>
                  <h3>基本信息</h3>
                  <p>你的账户身份与展示名称</p>
                </div>
              </div>
              <form className="profile-form" onSubmit={handleProfileSubmit}>
                <div className="profile-field">
                  <label htmlFor="profile-username">用户名</label>
                  <input
                    id="profile-username"
                    type="text"
                    value={user.username}
                    disabled
                    className="profile-input disabled"
                  />
                  <span className="profile-hint">
                    用户名由系统通过拼音生成，不可修改
                  </span>
                </div>
                <div className="profile-field">
                  <label htmlFor="profile-display-name">显示名称</label>
                  <input
                    id="profile-display-name"
                    autoComplete="nickname"
                    type="text"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder="用于界面展示的名称"
                    className="profile-input"
                    maxLength={64}
                  />
                </div>
                <div className="profile-field">
                  <label htmlFor="profile-real-name">实名</label>
                  <input
                    id="profile-real-name"
                    autoComplete="name"
                    type="text"
                    value={realName}
                    onChange={(e) => setRealName(e.target.value)}
                    placeholder="真实姓名"
                    className="profile-input"
                    maxLength={64}
                  />
                </div>
                <div className="profile-form-divider">
                  <h3>联系方式</h3>
                  <p>用于实验室日常沟通与协作</p>
                </div>
                <div className="profile-field">
                  <label htmlFor="profile-contact-type">联系方式类型</label>
                  <select
                    id="profile-contact-type"
                    value={contactType}
                    onChange={(e) =>
                      setContactType(e.target.value as "phone" | "wechat")
                    }
                    className="profile-input"
                  >
                    <option value="wechat">微信</option>
                    <option value="phone">手机号</option>
                  </select>
                </div>
                <div className="profile-field">
                  <label htmlFor="profile-contact-value">
                    {contactType === "wechat" ? "微信号" : "手机号"}
                  </label>
                  <input
                    id="profile-contact-value"
                    type={contactType === "phone" ? "tel" : "text"}
                    autoComplete={contactType === "phone" ? "tel" : "off"}
                    value={contactValue}
                    onChange={(e) => setContactValue(e.target.value)}
                    placeholder={contactType === "wechat" ? "微信号" : "手机号"}
                    className="profile-input"
                    maxLength={64}
                  />
                </div>
                {message && (
                  <div
                    className={`profile-message ${message.type}`}
                    role={message.type === "err" ? "alert" : "status"}
                  >
                    {message.text}
                  </div>
                )}
                <div className="profile-form-footer">
                  <span className="profile-hint">
                    {profileDirty ? "有尚未保存的更改" : "资料已同步"}
                  </span>
                  <button
                    type="submit"
                    className="btn btn-primary"
                    disabled={saving || !profileDirty}
                  >
                    {saving ? "正在保存..." : "保存更改"}
                  </button>
                </div>
              </form>
            </section>
          </div>

          <div id="profile-security" hidden={activeModule !== "security"}>
            <section className="profile-section">
              <div className="profile-card-heading">
                <KeyRound size={19} aria-hidden="true" />
                <div>
                  <h3>登录密码</h3>
                  <p>建议使用不与其他账户重复的密码</p>
                </div>
              </div>
              <form className="profile-form" onSubmit={handlePasswordSubmit}>
                <div className="profile-field">
                  <label htmlFor="profile-old-password">原密码</label>
                  <input
                    id="profile-old-password"
                    autoComplete="current-password"
                    type="password"
                    value={oldPassword}
                    onChange={(e) => setOldPassword(e.target.value)}
                    placeholder="请输入当前密码"
                    className="profile-input"
                    required
                  />
                </div>
                <div className="profile-field">
                  <label htmlFor="profile-new-password">新密码</label>
                  <input
                    id="profile-new-password"
                    autoComplete="new-password"
                    minLength={6}
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="请输入新密码（至少6位）"
                    className="profile-input"
                    required
                  />
                </div>
                <div className="profile-field">
                  <label htmlFor="profile-confirm-password">确认新密码</label>
                  <input
                    id="profile-confirm-password"
                    autoComplete="new-password"
                    minLength={6}
                    type="password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="请再次输入新密码"
                    className="profile-input"
                    required
                  />
                </div>
                {pwMessage && (
                  <div
                    className={`profile-message ${pwMessage.type}`}
                    role={pwMessage.type === "err" ? "alert" : "status"}
                  >
                    {pwMessage.text}
                  </div>
                )}
                <button
                  type="submit"
                  className="btn btn-frosted profile-submit"
                  disabled={pwSaving}
                >
                  {pwSaving ? "正在修改..." : "修改密码"}
                </button>
              </form>
            </section>

            <PasskeyManager key={user.id} />
          </div>

          <div id="profile-api" hidden={activeModule !== "api"}>
            <div className="profile-api-overview">
              <Terminal size={24} aria-hidden="true" />
              <div>
                <strong>个人 API Token</strong>
                <p>仅允许查询本人容器与资源使用情况</p>
              </div>
              <span className="profile-token-count">
                {tokensLoading
                  ? "加载中"
                  : tokensError
                    ? "暂时无法获取"
                    : `${activeTokens} 个有效令牌`}
              </span>
            </div>
            <section className="profile-section profile-token-section">
              <div className="profile-card-heading">
                <KeyRound size={19} aria-hidden="true" />
                <div>
                  <h3>访问令牌</h3>
                  <p>为不同工具分别创建令牌，便于独立撤销</p>
                </div>
              </div>
              <div className="profile-form">
                <details className="profile-token-create">
                  <summary>
                    <Plus size={17} aria-hidden="true" />
                    创建新令牌
                    <ChevronRight size={16} aria-hidden="true" />
                  </summary>
                  <p className="profile-hint">
                    令牌只在创建时显示一次，请立即保存，勿提交到代码仓库。
                  </p>
                  <form onSubmit={handleTokenCreate}>
                    <div className="profile-field">
                      <label htmlFor="personal-token-name">令牌名称</label>
                      <input
                        id="personal-token-name"
                        className="profile-input"
                        value={tokenName}
                        onChange={(e) => setTokenName(e.target.value)}
                        maxLength={64}
                        placeholder="例如：实验监控"
                        required
                      />
                    </div>
                    <div className="profile-field">
                      <label htmlFor="personal-token-days">有效期（天）</label>
                      <input
                        id="personal-token-days"
                        className="profile-input"
                        type="number"
                        min={1}
                        max={365}
                        value={expiresInDays}
                        onChange={(e) =>
                          setExpiresInDays(Number(e.target.value))
                        }
                        required
                      />
                    </div>
                    <button
                      type="submit"
                      className="btn btn-primary profile-submit"
                      disabled={tokenSaving || tokensLoading || !!createdToken}
                    >
                      {tokenSaving ? "正在创建..." : "创建 Token"}
                    </button>
                    {createdToken && (
                      <p className="profile-hint profile-token-save-hint">
                        请先保存下方的新令牌并关闭显示，再创建其他令牌。
                      </p>
                    )}
                  </form>
                </details>
                {tokenMessage && (
                  <div
                    className={`profile-message ${tokenMessage.type}`}
                    role={tokenMessage.type === "err" ? "alert" : "status"}
                  >
                    {tokenMessage.text}
                  </div>
                )}
                {createdToken && (
                  <div className="profile-token-created">
                    <h3>请保存你的新令牌</h3>
                    <p className="profile-hint">
                      仅本次显示。关闭后将无法再次查看完整令牌。
                    </p>
                    <button
                      type="button"
                      className="btn btn-frosted btn-sm profile-copy-token"
                      onClick={() =>
                        void copyTokenText(
                          createdToken.token,
                          "令牌已复制，请妥善保管",
                        )
                      }
                    >
                      <Copy size={14} aria-hidden="true" />
                      复制令牌
                    </button>
                    <input
                      className="profile-input"
                      type="text"
                      readOnly
                      value={createdToken.token}
                      aria-label="新创建的个人 API Token"
                      onFocus={(e) => e.currentTarget.select()}
                    />
                    <p className="profile-hint">
                      以下说明会将令牌交给
                      Agent，请勿发送到公开聊天或提交到代码仓库。
                    </p>
                    <label htmlFor="personal-agent-instructions">
                      复制以下内容发送给实验目录中的 Agent
                    </label>
                    <textarea
                      id="personal-agent-instructions"
                      className="profile-input profile-agent-instructions"
                      readOnly
                      value={agentInstructions}
                      onFocus={(e) => e.currentTarget.select()}
                    />
                    <button
                      type="button"
                      className="btn btn-frosted profile-submit"
                      onClick={() =>
                        void copyTokenText(agentInstructions, "说明已复制")
                      }
                    >
                      复制 Agent 说明
                    </button>
                    <button
                      type="button"
                      className="btn btn-frosted profile-submit"
                      onClick={() => setCreatedToken(null)}
                    >
                      已保存，关闭令牌显示
                    </button>
                  </div>
                )}
                <h3 className="profile-list-title">
                  已创建的令牌 <span>{tokens.length}</span>
                </h3>
                {tokensLoading && (
                  <p className="profile-hint" role="status">
                    正在加载令牌…
                  </p>
                )}
                {tokensError && (
                  <div className="profile-message err" role="alert">
                    {tokensError}{" "}
                    <button
                      type="button"
                      className="btn btn-frosted btn-sm"
                      onClick={() => setTokenReload((current) => current + 1)}
                    >
                      重试
                    </button>
                  </div>
                )}
                {!tokensLoading && !tokensError && tokens.length === 0 && (
                  <div className="profile-empty">
                    <KeyRound size={26} aria-hidden="true" />
                    <strong>还没有访问令牌</strong>
                    <p>需要连接监控工具时，在上方创建你的第一个令牌。</p>
                  </div>
                )}
                <ul className="profile-token-list" aria-busy={tokensLoading}>
                  {tokens.map((token) => {
                    const status = token.revoked_at
                      ? "revoked"
                      : new Date(token.expires_at) <= new Date()
                        ? "expired"
                        : "active";
                    return (
                      <li key={token.id}>
                        <div className="profile-token-info">
                          <div className="profile-token-title">
                            <strong>{token.name}</strong>
                            <span className={`profile-token-status ${status}`}>
                              {status === "revoked"
                                ? "已撤销"
                                : status === "expired"
                                  ? "已过期"
                                  : "有效"}
                            </span>
                          </div>
                          <span className="profile-hint">
                            到期：{new Date(token.expires_at).toLocaleString()}
                          </span>
                        </div>
                        {!token.revoked_at && (
                          <button
                            type="button"
                            className="btn btn-frosted btn-sm profile-danger"
                            disabled={revokingId !== null}
                            aria-label={`撤销令牌 ${token.name}`}
                            onClick={() => handleTokenRevoke(token)}
                          >
                            {revokingId === token.id ? "正在撤销…" : "撤销"}
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>
  );
}
