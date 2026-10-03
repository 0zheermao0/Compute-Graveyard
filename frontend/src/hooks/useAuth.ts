import { useState, useEffect, useCallback } from "react";
import { authenticatePasskey } from "../api/passkeys";

const API = "/api";

export interface User {
  id: number;
  username: string;
  display_name: string;
  role: string;
  created_at: string;
  real_name?: string | null;
  contact_type?: string | null;
  contact_value?: string | null;
}

export interface LoginToken {
  access_token: string;
  token_type: string;
  user: User;
}

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchUser = useCallback(async () => {
    const token = localStorage.getItem("token");
    if (!token) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      const res = await fetch(`${API}/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const data = await res.json();
        setUser(data);
      } else {
        localStorage.removeItem("token");
        setUser(null);
      }
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchUser();
  }, [fetchUser]);

  const completeLogin = (data: LoginToken) => {
    localStorage.setItem("token", data.access_token);
    setUser(data.user);
    return data.user;
  };

  const loginWithPasskey = async (signal?: AbortSignal) => {
    const data = await authenticatePasskey(signal);
    if (signal?.aborted) throw new DOMException("Passkey operation aborted", "AbortError");
    return completeLogin(data);
  };

  const login = async (username: string, password: string) => {
    const res = await fetch(`${API}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "登录失败");
    }
    const data: LoginToken = await res.json();
    return completeLogin(data);
  };

  const logout = () => {
    localStorage.removeItem("token");
    setUser(null);
  };

  return { user, loading, login, loginWithPasskey, logout, refresh: fetchUser };
}
