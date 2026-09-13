import ipaddress
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.config import AGENT_REQUEST_TIMEOUT_SECONDS


class RemoteAgentError(RuntimeError):
    pass


def normalize_agent_base_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Agent 地址格式无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Agent 地址必须是有效的 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Agent 地址只能包含协议、主机和端口")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Agent 端口无效")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("远程 Agent 地址不能指向本机回环地址")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved):
        raise ValueError("远程 Agent 地址不能使用回环、链路本地或保留地址")
    return raw


class RemoteAgentClient:
    def __init__(self, base_url: str, token: str, timeout: float = AGENT_REQUEST_TIMEOUT_SECONDS):
        try:
            self.base_url = normalize_agent_base_url(base_url)
        except ValueError as exc:
            raise RemoteAgentError(str(exc)) from exc
        if not token:
            raise RemoteAgentError("节点缺少 Agent 凭据")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}/api/agent/v1{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=json,
                timeout=self.timeout,
                follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as exc:
            raise RemoteAgentError("节点 Agent 请求超时") from exc
        except httpx.HTTPStatusError as exc:
            raise RemoteAgentError(f"节点 Agent 返回 HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RemoteAgentError("节点 Agent 响应无效或连接失败") from exc

    def health(self) -> dict:
        return self._request("GET", "/health")

    def inventory(self) -> dict:
        return self._request("GET", "/inventory")

    def list_containers(self) -> list[dict]:
        return self._request("GET", "/containers")

    def create_container(self, payload: dict) -> dict:
        return self._request("POST", "/containers", json=payload)

    def stop_container(self, container_id: str) -> dict:
        return self._request("POST", f"/containers/{quote(container_id, safe='')}/stop")

    def delete_container(self, container_id: str) -> dict:
        return self._request("DELETE", f"/containers/{quote(container_id, safe='')}")
