const API = "/api";
// 注册接口无 token，使用完整路径

function getToken() {
  return localStorage.getItem("token");
}

export async function fetcher<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options?.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API}${path}`, { ...options, headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText || "请求失败");
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

/** GET 请求并返回文本（如工作区文件内容） */
export async function fetcherText(path: string): Promise<string> {
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API}${path}`, { headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText || "请求失败");
  }
  return res.text();
}
