"""Home Assistant REST client.

Only /api/states is used, one request per configured row. A long-lived access
token is required: HA profile page -> Security -> Long-lived access tokens.
"""

from __future__ import annotations

import time as clock
from dataclasses import dataclass

import httpx

from .config import Config, StatusRow

_cache: dict[str, tuple[float, object]] = {}


@dataclass
class StatusItem:
    label: str
    icon: str
    text: str
    active: bool
    available: bool


def _get_state(config: Config, entity_id: str) -> str | None:
    token = config.ha_token
    if not token:
        return None
    url = f"{config.home_assistant.base_url.rstrip('/')}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = httpx.get(
            url, headers=headers, timeout=config.home_assistant.timeout_seconds
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return response.json().get("state")


def _to_item(row: StatusRow, state: str | None) -> StatusItem:
    # HA reports these two for entities it cannot currently reach. Showing them
    # as "closed" would be a lie in exactly the case where you care most.
    if state is None or state in {"unavailable", "unknown"}:
        return StatusItem(
            label=row.label, icon=row.icon, text="?", active=False, available=False
        )
    active = state in row.on_states
    return StatusItem(
        label=row.label,
        icon=row.icon,
        text=row.on_text if active else row.off_text,
        active=active,
        available=True,
    )


def fetch_status(config: Config) -> list[StatusItem]:
    now = clock.monotonic()
    hit = _cache.get("status")
    if hit and now - hit[0] < config.cache.states_seconds:
        return hit[1]  # type: ignore[return-value]

    items = [_to_item(row, _get_state(config, row.entity)) for row in config.status]
    _cache["status"] = (now, items)
    return items
