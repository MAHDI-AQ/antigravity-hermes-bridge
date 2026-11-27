"""A2A v1.0 HTTP/JSON-RPC client for discovering and calling external A2A agents."""

from __future__ import annotations

import os
from typing import Any
import uuid

import httpx


def build_auth_headers(peer_cfg: dict[str, Any] | None) -> dict[str, str]:
    """Resolve auth headers from peer config without persisting secrets into task logs."""
    headers: dict[str, str] = {"A2A-Version": "1.0"}
    if not peer_cfg:
        return headers
    auth = peer_cfg.get("auth")
    if not isinstance(auth, dict):
        return headers

    token = auth.get("token")
    if not token and auth.get("token_env"):
        token = os.environ.get(str(auth["token_env"]), "")
    if not token:
        return headers

    auth_type = str(auth.get("type") or "bearer").lower()
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "api_key":
        header_name = str(auth.get("header") or "X-API-Key")
        headers[header_name] = str(token)
    return headers


def select_jsonrpc_url(card: dict[str, Any], base_url: str) -> str:
    """Pick the preferred JSON-RPC endpoint from an A2A v1.0 or v0.3 AgentCard."""
    interfaces = card.get("supportedInterfaces")
    if isinstance(interfaces, list):
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            binding = str(iface.get("protocolBinding") or "").upper()
            if binding in ("JSONRPC", "JSON-RPC", "") and iface.get("url"):
                return str(iface["url"])
    if card.get("url"):
        return str(card["url"])
    return base_url.rstrip("/")


async def fetch_agent_card(
    base_url: str,
    peer_cfg: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch an A2A AgentCard from `/.well-known/agent-card.json` (or legacy `/.well-known/agent.json`)."""
    base = base_url.rstrip("/")
    headers = build_auth_headers(peer_cfg)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        last_err: Exception | None = None
        for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            try:
                resp = await client.get(f"{base}{path}", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        return data
            except Exception as exc:
                last_err = exc
        if last_err:
            raise RuntimeError(f"Failed to fetch AgentCard from {base}: {last_err}") from last_err
        raise RuntimeError(f"AgentCard not found at {base}/.well-known/agent-card.json")


async def jsonrpc_call(
    endpoint_url: str,
    method: str,
    params: dict[str, Any],
    *,
    fallback_method: str | None = None,
    peer_cfg: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Call an A2A JSON-RPC 2.0 endpoint, with v1.0 -> legacy method-name fallback."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **build_auth_headers(peer_cfg),
    }

    async def _post(m: str) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex[:12],
            "method": m,
            "params": params,
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(endpoint_url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    body = await _post(method)
    if isinstance(body, dict) and "error" in body and fallback_method:
        err = body["error"]
        if isinstance(err, dict) and err.get("code") == -32601:
            body = await _post(fallback_method)

    if isinstance(body, dict) and "error" in body:
        raise RuntimeError(f"A2A peer RPC error ({method}): {body['error']}")
    if isinstance(body, dict) and "result" in body:
        res = body["result"]
        return res if isinstance(res, dict) else {"result": res}
    return body if isinstance(body, dict) else {"result": body}


async def get_json(
    url: str,
    *,
    peer_cfg: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Perform an authenticated A2A HTTP GET returning a JSON object."""
    headers = {
        "Accept": "application/json",
        **build_auth_headers(peer_cfg),
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {"result": data}


async def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    peer_cfg: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Perform an authenticated A2A HTTP POST with a JSON payload."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **build_auth_headers(peer_cfg),
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {"result": data}


class A2AHttpClient:
    """Reusable A2A v1.0 JSON-RPC client bound to a base URL and optional peer config."""

    def __init__(
        self,
        base_url: str,
        *,
        peer_cfg: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.peer_cfg = peer_cfg
        self.timeout = timeout
        self._card: dict[str, Any] | None = None

    async def get_card(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh or self._card is None:
            self._card = await fetch_agent_card(self.base_url, self.peer_cfg, timeout=min(15.0, self.timeout))
        return self._card

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        fallback_method: str | None = None,
    ) -> dict[str, Any]:
        card = await self.get_card()
        endpoint = select_jsonrpc_url(card, self.base_url)
        return await jsonrpc_call(
            endpoint,
            method,
            params,
            fallback_method=fallback_method,
            peer_cfg=self.peer_cfg,
            timeout=self.timeout,
        )
