import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  deletePasskey,
  getPasskeyErrorMessage,
  getPasskeySupportError,
  listPasskeys,
  registerPasskey,
  type Passkey,
} from "../api/passkeys";
import { Fingerprint } from "lucide-react";
import "./PasskeyManager.css";

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString();
}

export default function PasskeyManager() {
  const [passkeys, setPasskeys] = useState<Passkey[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listLoaded, setListLoaded] = useState(false);
  const [listError, setListError] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [registering, setRegistering] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Passkey | null>(null);
  const [deletePassword, setDeletePassword] = useState("");
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [message, setMessage] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const operationBusyRef = useRef(false);
  const actionAbortRef = useRef<AbortController | null>(null);
  const listAbortRef = useRef<AbortController | null>(null);
  const supportError = getPasskeySupportError();
  const operationBusy = registering || deletingId !== null;

  const loadPasskeys = useCallback(async () => {
    if (operationBusyRef.current) return;
    listAbortRef.current?.abort();
    const controller = new AbortController();
    listAbortRef.current = controller;
    setListLoading(true);
    setListError("");
    try {
      const items = await listPasskeys(controller.signal);
      if (controller.signal.aborted) return;
      setPasskeys(items);
      setListLoaded(true);
    } catch (error) {
      if (!controller.signal.aborted) {
        setListError(getPasskeyErrorMessage(error, "加载 Passkey 列表失败，请重试。"));
      }
    } finally {
      if (listAbortRef.current === controller) {
        listAbortRef.current = null;
        if (!controller.signal.aborted) setListLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadPasskeys();
    return () => {
      listAbortRef.current?.abort();
      actionAbortRef.current?.abort();
    };
  }, [loadPasskeys]);

  const handleRegister = async (event: FormEvent) => {
    event.preventDefault();
    if (operationBusyRef.current || listAbortRef.current) return;
    setMessage(null);
    if (supportError) {
      setMessage({ type: "err", text: supportError });
      return;
    }
    if (!name.trim() || !password) {
      setMessage({ type: "err", text: "请输入 Passkey 名称和当前密码。" });
      return;
    }
    operationBusyRef.current = true;
    const controller = new AbortController();
    actionAbortRef.current = controller;
    setRegistering(true);
    setDeleteTarget(null);
    setDeletePassword("");
    try {
      const created = await registerPasskey(password, name, controller.signal);
      if (controller.signal.aborted) return;
      setPasskeys((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setName("");
      setMessage({ type: "ok", text: "Passkey 绑定成功，下次可直接使用 Passkey 登录。" });
    } catch (error) {
      if (!controller.signal.aborted) {
        setMessage({ type: "err", text: getPasskeyErrorMessage(error, "绑定 Passkey 失败，请重试。") });
      }
    } finally {
      actionAbortRef.current = null;
      if (!controller.signal.aborted) {
        setPassword("");
        setRegistering(false);
      }
      operationBusyRef.current = false;
    }
  };

  const selectDelete = (passkey: Passkey) => {
    if (operationBusyRef.current || listAbortRef.current) return;
    setMessage(null);
    setPassword("");
    setDeletePassword("");
    setDeleteTarget(passkey);
  };

  const handleDelete = async (event: FormEvent) => {
    event.preventDefault();
    if (operationBusyRef.current || listAbortRef.current || !deleteTarget) return;
    if (!deletePassword) {
      setMessage({ type: "err", text: "请输入当前密码以确认删除。" });
      return;
    }
    const target = deleteTarget;
    operationBusyRef.current = true;
    const controller = new AbortController();
    actionAbortRef.current = controller;
    setDeletingId(target.id);
    setMessage(null);
    try {
      await deletePasskey(target.id, deletePassword, controller.signal);
      if (controller.signal.aborted) return;
      setPasskeys((current) => current.filter((item) => item.id !== target.id));
      setDeleteTarget(null);
      setMessage({ type: "ok", text: `已删除 Passkey「${target.name}」。` });
    } catch (error) {
      if (!controller.signal.aborted) {
        setMessage({ type: "err", text: getPasskeyErrorMessage(error, "删除 Passkey 失败，请重试。") });
      }
    } finally {
      actionAbortRef.current = null;
      if (!controller.signal.aborted) {
        setDeletePassword("");
        setDeletingId(null);
      }
      operationBusyRef.current = false;
    }
  };

  return (
    <section className="profile-section profile-passkey-section">
      <div className="profile-card-heading"><Fingerprint size={19} aria-hidden="true" /><div><h3>Passkey · 无密码登录</h3><p>通过指纹、面容或安全密钥，快捷验证身份</p></div></div>
      <div className="profile-form">
        <p className="profile-hint">绑定后可通过指纹、面容、设备 PIN 或安全密钥登录，无需输入用户名和密码。绑定与删除均需当前密码确认。</p>
        {supportError && <p className="profile-message err" role="status">{supportError} 仍可管理已绑定的 Passkey。</p>}
        <form onSubmit={handleRegister} aria-busy={registering}>
          <div className="profile-field">
            <label htmlFor="passkey-name">Passkey 名称</label>
            <input id="passkey-name" className="profile-input" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：我的手机、笔记本" maxLength={64} required disabled={operationBusy} />
          </div>
          <div className="profile-field">
            <label htmlFor="passkey-register-password">当前密码（确认绑定）</label>
            <input id="passkey-register-password" className="profile-input" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required disabled={operationBusy} />
          </div>
          <button type="submit" className="btn btn-primary profile-submit" disabled={operationBusy || listLoading || !!supportError}>
            {registering ? "正在绑定，请完成设备验证..." : "绑定 Passkey"}
          </button>
        </form>
        {message && <div className={`profile-message profile-passkey-message ${message.type}`} role={message.type === "err" ? "alert" : "status"}>{message.text}</div>}

        <div className="profile-passkey-list-heading">
          <h3>已绑定的 Passkey</h3>
          <button type="button" className="btn btn-frosted btn-sm" onClick={() => void loadPasskeys()} disabled={listLoading || operationBusy}>
            {listLoading ? "加载中..." : "刷新列表"}
          </button>
        </div>
        {listLoading && <p className="profile-hint" role="status">正在加载 Passkey 列表...</p>}
        {listError && <div className="profile-message err" role="alert">{listError} 其他个人资料功能不受影响，可点击「刷新列表」重试。</div>}
        {!listLoading && !listError && listLoaded && passkeys.length === 0 && <p className="profile-hint">尚未绑定 Passkey。</p>}
        <ul className="profile-passkey-list" aria-busy={listLoading}>
          {passkeys.map((passkey) => (
            <li key={passkey.id}>
              <div className="profile-passkey-summary">
                <div>
                  <strong>{passkey.name}</strong>
                  <span className="profile-hint">创建：{formatTime(passkey.created_at)}</span>
                  <span className="profile-hint">最后使用：{passkey.last_used_at ? formatTime(passkey.last_used_at) : "尚未使用"}</span>
                  <span className="profile-hint">{passkey.backed_up ? "已备份" : "未备份"}</span>
                </div>
                <button type="button" className="btn btn-frosted btn-sm profile-passkey-delete" onClick={() => selectDelete(passkey)} disabled={operationBusy || listLoading || deleteTarget?.id === passkey.id} aria-label={`删除 Passkey ${passkey.name}`}>删除</button>
              </div>
              {deleteTarget?.id === passkey.id && (
                <form className="profile-passkey-delete-form" onSubmit={handleDelete} aria-busy={deletingId === passkey.id}>
                  <p className="profile-hint">确认删除「{passkey.name}」？删除后无法再用此 Passkey 登录，仍可使用密码登录。</p>
                  <div className="profile-field">
                    <label htmlFor={`passkey-delete-password-${passkey.id}`}>当前密码（确认删除）</label>
                    <input id={`passkey-delete-password-${passkey.id}`} className="profile-input" type="password" autoComplete="current-password" value={deletePassword} onChange={(event) => setDeletePassword(event.target.value)} required disabled={operationBusy || listLoading} autoFocus />
                  </div>
                  <div className="profile-passkey-delete-actions">
                    <button type="submit" className="btn btn-frosted btn-sm profile-passkey-delete" disabled={operationBusy || listLoading}>{deletingId === passkey.id ? "正在删除..." : "确认删除"}</button>
                    <button type="button" className="btn btn-frosted btn-sm" disabled={operationBusy || listLoading} onClick={() => { setDeleteTarget(null); setDeletePassword(""); setMessage(null); }}>取消</button>
                  </div>
                </form>
              )}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
