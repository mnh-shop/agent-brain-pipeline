from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from pipeline.config import get_config
from pipeline.db import connect, create_run
from pipeline.urls import parse_repository_url
from pipeline.util import run_command, temporary_askpass, utc_now

logger = logging.getLogger(__name__)


async def scheduler_loop(stop_event: asyncio.Event) -> None:
    cfg = get_config()
    if not cfg.get("maintenance", {}).get("enabled", True):
        return
    poll_seconds = int(cfg["maintenance"].get("scheduler_poll_minutes", 10)) * 60
    while not stop_event.is_set():
        await asyncio.to_thread(check_due_sources)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            continue


def check_due_sources() -> None:
    cfg = get_config()
    now = datetime.now(timezone.utc)
    with connect() as connection:
        rows = connection.execute("SELECT * FROM sources WHERE next_refresh_at IS NOT NULL").fetchall()
    for row in rows:
        try:
            due = datetime.fromisoformat(row["next_refresh_at"])
            if due > now:
                continue
            enqueue_refresh(dict(row), cfg)
        except Exception:
            logger.exception("Failed refresh check for %s", row["source_id"])


def enqueue_refresh(source: dict[str, Any], cfg: dict[str, Any]) -> None:
    with connect() as connection:
        existing = connection.execute(
            "SELECT 1 FROM runs WHERE (source_id=? OR repository_url=?) AND status IN ('queued','running') LIMIT 1",
            (source["source_id"], source["repository_url"])
        ).fetchone()
        if existing:
            return
    create_run(source["repository_url"], source.get("default_branch"), "maintainer")
    hours = float(cfg["maintenance"].get("refresh_interval_hours", 36))
    jitter = float(cfg["maintenance"].get("refresh_jitter_hours", 6))
    next_hours = max(1, hours + random.uniform(-jitter, jitter))
    next_at = (datetime.now(timezone.utc) + timedelta(hours=next_hours)).isoformat()
    with connect() as connection:
        connection.execute("UPDATE sources SET next_refresh_at=?, updated_at=? WHERE source_id=?", (next_at, utc_now(), source["source_id"]))
