"""Periodic task orchestration for the polling subsystem.

Orchestrates time-gated tasks within the poll loop: topic lifecycle management,
live view ticking, and state pruning.

Key components:
  - run_periodic_tasks: time-gated live view tick and topic check
  - run_lifecycle_tasks: per-tick autoclose and unbound window management
"""

import time
from typing import TYPE_CHECKING

import structlog

from ...config import config
from ...telegram_client import TelegramClient
from ...utils import log_throttle_sweep
from ..live.live_view import tick_live_views
from ..topics.topic_lifecycle import (
    check_autoclose_timers,
    check_unbound_window_ttl,
    probe_topic_existence,
    prune_stale_state,
    sync_topic_names_from_windows,
)

if TYPE_CHECKING:
    from ...multiplexer.base import WindowRef as TmuxWindow

logger = structlog.get_logger()

# ── Timing constants ──────────────────────────────────────────────────────

TOPIC_CHECK_INTERVAL = 60.0  # seconds
TOPIC_NAME_SYNC_INTERVAL = 5.0  # seconds


# ── Orchestration ──────────────────────────────────────────────────────────


async def run_periodic_tasks(
    client: TelegramClient,
    all_windows: list["TmuxWindow"],
    timers: dict[str, float],
) -> None:
    """Run time-gated periodic tasks (topic check, live view, name sync)."""
    now = time.monotonic()

    if now - timers["live_view"] >= config.live_view_interval:
        timers["live_view"] = now
        await tick_live_views(client)

    if (
        config.sync_topic_name_from_window
        and now - timers["topic_name_sync"] >= TOPIC_NAME_SYNC_INTERVAL
    ):
        timers["topic_name_sync"] = now
        await sync_topic_names_from_windows(client, all_windows)

    if now - timers["topic_check"] >= TOPIC_CHECK_INTERVAL:
        timers["topic_check"] = now
        await prune_stale_state(all_windows)
        await probe_topic_existence(client)
        log_throttle_sweep()


async def run_lifecycle_tasks(
    client: TelegramClient, all_windows: list["TmuxWindow"]
) -> None:
    """Run per-tick lifecycle tasks (autoclose timers, unbound window TTL)."""
    await check_autoclose_timers(client)
    await check_unbound_window_ttl(all_windows)
