# -*- coding: utf-8 -*-
"""IP allowlist middleware: restrict Web UI / API access to configured clients.

Complements admin password auth (``ADMIN_AUTH_ENABLED``): even with the
password enabled, binding to ``0.0.0.0`` exposes the service to every host on
the LAN. This middleware adds a network-level gate so only explicitly allowed
IPs/CIDRs can reach **any** path (pages included, not just ``/api/v1``).

Design notes:

- **Direct peer IP only** (``request.client.host``). We deliberately ignore
  ``X-Forwarded-For`` here even when ``TRUST_X_FORWARDED_FOR=true``: a
  allowlist that trusts a spoofable header is no allowlist at all. Put a real
  reverse proxy in front if you need proxy awareness.
- **Loopback is always allowed** (``127.0.0.1`` / ``::1``) so a misconfigured
  list can never lock the host machine out of its own service.
- **Empty list = disabled** (fail-open) to preserve the previous behaviour for
  anyone who never sets ``WEBUI_ALLOWED_IPS``.
- Values are read per request, so an edit only needs the env to be reloaded
  (same contract as ``add_auth_middleware`` / ``is_auth_enabled``).
"""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Callable, List

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

ENV_KEY = "WEBUI_ALLOWED_IPS"

# Never lock the host out of its own service.
_ALWAYS_ALLOWED = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})


def parse_allowed_networks(raw: str) -> List[ipaddress._BaseNetwork]:
    """Parse a comma-separated list of IPs/CIDRs into network objects.

    Invalid entries are skipped with a warning instead of aborting startup —
    a typo in one CIDR should not take the whole service down.
    """
    networks: List[ipaddress._BaseNetwork] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("[IP白名单] 忽略非法条目: %s", token)
    return networks


def _client_ip(request: Request) -> str:
    """Direct peer IP (never trusts X-Forwarded-For)."""
    if request.client:
        return request.client.host or ""
    return ""


def _is_allowed(ip: str, networks: List[ipaddress._BaseNetwork]) -> bool:
    if not ip:
        return False
    if ip in _ALWAYS_ALLOWED:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in networks)


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """Reject requests from IPs outside ``WEBUI_ALLOWED_IPS`` with 403."""

    async def dispatch(self, request: Request, call_next: Callable):
        networks = parse_allowed_networks(os.getenv(ENV_KEY, ""))
        if not networks:
            return await call_next(request)  # not configured → no restriction

        ip = _client_ip(request)
        if _is_allowed(ip, networks):
            return await call_next(request)

        logger.warning("[IP白名单] 拒绝访问: %s → %s", ip, request.url.path)
        return JSONResponse(
            status_code=403,
            content={
                "error": "forbidden",
                "message": "该 IP 不在允许访问的白名单中。",
            },
        )


def add_ip_allowlist_middleware(app) -> None:
    """Add the IP allowlist middleware.

    Registered **after** the auth middleware so it runs *outermost* (Starlette
    executes the most recently added middleware first) — blocked IPs are
    rejected before any authentication work happens.
    """
    app.add_middleware(IPAllowlistMiddleware)
