"""Home Assistant REST client.

Only /api/states is used. The HOME rows come from one of two places: a single
entity whose `rows` attribute already is the finished list (`rows_entity`), or
the static `status:` list, one request per row. A long-lived access token is
required: HA profile page -> Security -> Long-lived access tokens.
"""

from __future__ import annotations

import json
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


def _get_entity(config: Config, entity_id: str) -> dict | None:
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
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _get_state(config: Config, entity_id: str) -> str | None:
    entity = _get_entity(config, entity_id)
    return None if entity is None else entity.get("state")


def _rows_from_entity(config: Config, entity_id: str) -> list[StatusItem] | None:
    """Rows as Home Assistant composed them, or None to fall back to `status:`.

    None means "could not be read", not "empty": a template that deliberately
    yields no rows is a valid answer and must not resurrect the static list.
    """
    entity = _get_entity(config, entity_id)
    if entity is None:
        return None
    rows = (entity.get("attributes") or {}).get("rows")
    # A template that renders to text rather than a native list arrives as a
    # string; accept it when it is JSON instead of failing on a quoting detail.
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except ValueError:
            return None
    if not isinstance(rows, list):
        return None

    items = []
    for row in rows:
        if not isinstance(row, dict) or row.get("label") is None:
            continue
        text = row.get("text")
        items.append(
            StatusItem(
                label=str(row["label"]),
                icon=str(row.get("icon") or "dot"),
                text="" if text is None else str(text),
                active=bool(row.get("alert", False)),
                available=True,
            )
        )
    return items


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

    items = None
    if config.home_assistant.rows_entity:
        items = _rows_from_entity(config, config.home_assistant.rows_entity)
    if items is None:
        items = [_to_item(row, _get_state(config, row.entity)) for row in config.status]
    _cache["status"] = (now, items)
    return items
