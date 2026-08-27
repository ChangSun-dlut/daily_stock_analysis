"""In-process store for web (browser) alert popups.

This acts as a fallback delivery channel for alert notifications: when an alert
fires (e.g. a volume-spike) but the primary push channel (WeChat / Feishu) is
unavailable or slow, the web dashboard can still surface the alert in real time
as a popup. It is intentionally simple and process-local — alerts are ephemeral
and bounded; a restart loses history (acceptable for a popup fallback).

The frontend polls ``GET /api/v1/alerts/web-popups`` with ``?since=<id>`` to get
only new alerts and renders them as toasts/modals.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

_MAX_ALERTS = 200


@dataclass
class WebAlert:
    id: int
    title: str
    body: str
    level: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "level": self.level,
            "created_at": self.created_at,
        }


class WebAlertHub:
    """Thread-safe in-memory ring buffer of recent alerts for the web UI."""

    def __init__(self, max_alerts: int = _MAX_ALERTS) -> None:
        self._lock = threading.Lock()
        self._deque: deque[WebAlert] = deque(maxlen=max_alerts)
        self._seq = 0

    def push(self, title: str, body: str, level: str = "warning") -> WebAlert:
        with self._lock:
            self._seq += 1
            alert = WebAlert(
                id=self._seq,
                title=title,
                body=body,
                level=level,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._deque.append(alert)
            return alert

    def get_all(self) -> List[WebAlert]:
        with self._lock:
            return [a for a in self._deque]

    def get_since(self, since_id: int) -> List[WebAlert]:
        with self._lock:
            return [a for a in self._deque if a.id > since_id]

    def clear(self) -> None:
        with self._lock:
            self._deque.clear()


# Module-level singleton shared across the process.
_web_alert_hub: Optional[WebAlertHub] = None


def get_web_alert_hub() -> WebAlertHub:
    global _web_alert_hub
    if _web_alert_hub is None:
        _web_alert_hub = WebAlertHub()
    return _web_alert_hub
