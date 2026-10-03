import {
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
  WebAuthnAbortService,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON,
} from "@simplewebauthn/browser";
import { fetcher } from "./client";
import type { LoginToken } from "../hooks/useAuth";

const PASSKEY_PATH = "/auth/passkeys";

export interface Passkey {
  id: number;
  name: string;
  created_at: string;
  last_used_at: string | null;
  backed_up: boolean;
}

type PasskeyOptions<T> = { challenge_id: string; options: T };

/** 不要求平台验证器：外置安全密钥和跨设备 Passkey 也可以使用。 */
export function getPasskeySupportError(): string | null {
  if (typeof window === "undefined" || !window.isSecureContext) {
    return "Passkey 需要安全环境，请通过 HTTPS 或 localhost 访问。";
  }
  if (!browserSupportsWebAuthn() || !navigator.credentials?.create || !navigator.credentials?.get) {
    return "当前浏览器不支持 Passkey，请使用新版 Chrome、Edge、Safari 或 Firefox。";
  }
  return null;
}

function requirePasskeySupport() {
  const error = getPasskeySupportError();
  if (error) throw new Error(error);
}

/** 页面离开时，同时取消网络请求和正在显示的设备验证。 */
async function runCeremony<T>(run: () => Promise<T>, signal?: AbortSignal): Promise<T> {
  if (signal?.aborted) throw new DOMException("Passkey operation aborted", "AbortError");
  const abort = () => WebAuthnAbortService.cancelCeremony();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    return await run();
  } finally {
    signal?.removeEventListener("abort", abort);
  }
}

/** 登录接口公开访问，不附带已有 JWT。 */
async function postPublic<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${PASSKEY_PATH}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = typeof error?.detail === "string" ? error.detail : "";
    throw new Error(detail || (response.status === 429
      ? "操作过于频繁，请稍后重试。"
      : "Passkey 登录验证失败，请重试或使用密码登录。"));
  }
  return response.json();
}

export function listPasskeys(signal?: AbortSignal) {
  return fetcher<Passkey[]>(PASSKEY_PATH, { signal });
}

export async function registerPasskey(password: string, name: string, signal?: AbortSignal) {
  requirePasskeySupport();
  const { challenge_id, options } = await fetcher<PasskeyOptions<PublicKeyCredentialCreationOptionsJSON>>(
    `${PASSKEY_PATH}/register/options`,
    { method: "POST", body: JSON.stringify({ password }), signal },
  );
  const credential = await runCeremony(() => startRegistration({ optionsJSON: options }), signal);
  return fetcher<Passkey>(`${PASSKEY_PATH}/register/verify`, {
    method: "POST",
    body: JSON.stringify({ challenge_id, credential, name: name.trim() }),
    signal,
  });
}

export async function authenticatePasskey(signal?: AbortSignal) {
  requirePasskeySupport();
  const { challenge_id, options } = await postPublic<PasskeyOptions<PublicKeyCredentialRequestOptionsJSON>>(
    "/login/options", {}, signal,
  );
  const credential = await runCeremony(() => startAuthentication({ optionsJSON: options }), signal);
  return postPublic<LoginToken>("/login/verify", { challenge_id, credential }, signal);
}

export function deletePasskey(id: number, password: string, signal?: AbortSignal) {
  return fetcher<{ ok: boolean }>(`${PASSKEY_PATH}/${id}`, {
    method: "DELETE",
    body: JSON.stringify({ password }),
    signal,
  });
}

/** WebAuthn 将取消、超时和无凭据统一报告为 NotAllowedError，不能可靠区分。 */
export function getPasskeyErrorMessage(error: unknown, fallback = "Passkey 操作失败，请重试。") {
  const details = error && typeof error === "object"
    ? error as { name?: string; code?: string; message?: string; cause?: { name?: string } }
    : {};
  const name = details.cause?.name || details.name;
  const code = details.code;
  const message = typeof details.message === "string" ? details.message : "";

  if (name === "NotAllowedError") {
    return "操作已取消、超时或未找到可用的 Passkey，请重试。";
  }
  if (name === "AbortError" || code === "ERROR_CEREMONY_ABORTED") {
    return "已取消 Passkey 操作，可重新尝试。";
  }
  if (name === "InvalidStateError" || code === "ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED" || /already (?:been )?(?:registered|exists|bound)|duplicate credential/i.test(message)) {
    return "此 Passkey 已绑定，请使用已有的 Passkey，或绑定其他设备。";
  }
  if (/unauthorized|not authenticated|invalid token|token.*expired|登录.*(?:过期|失效)/i.test(message)) {
    return "登录状态已失效，请重新登录。";
  }
  if (name === "TimeoutError" || /timed?\s*out|expired|过期|超时/i.test(message)) {
    return "Passkey 验证请求已超时或过期，请重新尝试。";
  }
  if (/challenge.*(?:invalid|not found|missing)|(?:invalid|missing).*challenge|挑战.*(?:无效|不存在)/i.test(message)) {
    return "Passkey 验证请求已失效，请重新尝试。";
  }
  if (name === "SecurityError" || code === "ERROR_INVALID_DOMAIN" || code === "ERROR_INVALID_RP_ID") {
    return "当前域名无法使用 Passkey，请通过 HTTPS 或 localhost 访问，并联系管理员检查域名配置。";
  }
  if (name === "NotSupportedError" || name === "ConstraintError" || code?.startsWith("ERROR_AUTHENTICATOR_MISSING_") || code === "ERROR_AUTHENTICATOR_NO_SUPPORTED_PUBKEYCREDPARAMS_ALG") {
    return "当前浏览器或设备不支持此 Passkey 验证方式，请使用其他浏览器或设备。";
  }
  if (name === "UnknownError" || code === "ERROR_AUTHENTICATOR_GENERAL_ERROR") {
    return "设备未能完成 Passkey 验证，请重试或使用其他设备。";
  }
  if (/failed to fetch|network\s*error|load failed|network request failed/i.test(message)) {
    return "网络连接失败，请检查连接后重试。";
  }
  if (/(?:incorrect|invalid|wrong) (?:current )?password|password.*(?:incorrect|invalid|wrong)/i.test(message)) {
    return "当前密码不正确，请重新输入。";
  }
  if (/(?:credential|passkey).*(?:not found|unknown|unregistered)|(?:unknown|unregistered) credential/i.test(message)) {
    return "未找到此 Passkey，请先用密码登录后绑定，或选择已绑定的 Passkey。";
  }
  if (/too many requests|rate limit/i.test(message)) {
    return "操作过于频繁，请稍后重试。";
  }
  // 保留后端已提供的中文提示，未知英文错误不直接暴露给用户。
  return /[\u3400-\u9fff]/.test(message) ? message : fallback;
}
