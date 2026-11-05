import time
from dataclasses import dataclass
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from telegram import Bot
from telegram.error import BadRequest, RetryAfter

from ccgram.multiplexer.base import WindowRef
from ccgram.window_view import WindowView

from ccgram.handlers.topics.topic_lifecycle import (
    PROBE_MAX_PER_CYCLE,
    check_unbound_window_ttl,
    probe_topic_existence,
    prune_stale_state,
    reset_probe_schedule,
    rollback_legacy_herdr_binding,
)
from ccgram.handlers.polling.polling_state import (
    lifecycle_strategy,
    terminal_poll_state,
)
from ccgram.telegram_rate_limiter import NO_RETRY_RATE_LIMIT_ARGS


@pytest.fixture(autouse=True)
def _clean_strategy_state():
    reset_probe_schedule()
    terminal_poll_state._states.clear()
    lifecycle_strategy._states.clear()
    lifecycle_strategy._dead_notified.clear()
    yield
    reset_probe_schedule()
    terminal_poll_state._states.clear()
    lifecycle_strategy._states.clear()
    lifecycle_strategy._dead_notified.clear()


class TestLegacyHerdrMigration:
    def test_rollback_restores_binding_but_remains_action_blocked(self) -> None:
        with (
            patch(
                "ccgram.handlers.topics.topic_lifecycle.legacy_state"
            ) as legacy_state,
            patch("ccgram.handlers.topics.topic_lifecycle.thread_router") as router,
        ):
            legacy_state.rollback_legacy_herdr_archive.return_value = True
            assert rollback_legacy_herdr_binding(1, 100, "w2:t1") is True
            router.bind_thread.assert_called_once_with(1, 100, "w2:t1")

    def test_rollback_rejects_non_archived_record(self) -> None:
        with patch(
            "ccgram.handlers.topics.topic_lifecycle.legacy_state"
        ) as legacy_state:
            legacy_state.rollback_legacy_herdr_archive.return_value = False
            assert rollback_legacy_herdr_binding(1, 100, "w2:t1") is False


HERDR_TARGET = "herdr-session-v1-" + "a" * 64


def _window_view(origin: str, window_id: str = "@0") -> WindowView:
    return WindowView(
        window_id=window_id,
        cwd="/tmp",
        provider_name="claude",
        approval_mode="normal",
        batch_mode="batched",
        tool_call_visibility="default",
        transcript_path=None,
        window_name="test",
        session_id="s1",
        origin=origin,
    )


@dataclass
class _TtlSweep:
    adapter: MagicMock
    revoke: MagicMock
    unbound_timer: float | None


async def _sweep_expired_unbound_window(
    window_id: str = "@0",
    *,
    origin: str = "ccgram_created",
    kill_ok: bool = True,
    bindings: list[tuple[int, int, str]] | None = None,
) -> _TtlSweep:
    """Age *window_id* past the unbound TTL, run one sweep, report what happened."""
    state = terminal_poll_state.get_state(window_id)
    state.unbound_timer = time.monotonic() - 100
    window = MagicMock(window_id=window_id, window_name="test")
    with (
        patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
        patch("ccgram.handlers.topics.topic_lifecycle.thread_router") as mock_router,
        patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
        patch("ccgram.handlers.topics.topic_lifecycle.revoke_window_tokens") as revoke,
    ):
        mock_config.unbound_window_ttl_minutes = 1
        mock_router.iter_thread_bindings.return_value = bindings or []
        mock_wq.view_window.return_value = _window_view(origin, window_id)
        adapter = MagicMock()
        adapter.close = AsyncMock(
            return_value=(kill_ok, "closed" if kill_ok else "failed")
        )
        await check_unbound_window_ttl([window], adapter=adapter)
    return _TtlSweep(
        adapter=adapter,
        revoke=revoke,
        unbound_timer=terminal_poll_state.get_state(window_id).unbound_timer,
    )


class TestCheckUnboundWindowTtl:
    async def test_no_timeout_is_noop(self):
        with patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config:
            mock_config.unbound_window_ttl_minutes = 0
            await check_unbound_window_ttl([], adapter=MagicMock())

    async def test_bound_window_timer_cleared(self):
        sweep = await _sweep_expired_unbound_window(bindings=[(1, 100, "@0")])
        assert sweep.unbound_timer is None
        sweep.adapter.close.assert_not_awaited()

    async def test_manually_discovered_window_is_not_killed(self):
        sweep = await _sweep_expired_unbound_window(origin="manual_discovered")
        assert sweep.unbound_timer is None
        sweep.adapter.close.assert_not_awaited()

    @pytest.mark.parametrize(
        "window_id",
        ["@0", HERDR_TARGET],
        ids=["tmux_window", "guarded_herdr_target"],
    )
    async def test_ccgram_created_window_is_killed_through_the_proxy(
        self, window_id: str
    ):
        sweep = await _sweep_expired_unbound_window(window_id)
        sweep.adapter.close.assert_awaited_once_with(window_id)
        sweep.revoke.assert_called_once_with(window_id)

    async def test_failed_kill_keeps_tokens_alive(self):
        """Tokens stay valid while the window might still be running."""
        sweep = await _sweep_expired_unbound_window(kill_ok=False)
        sweep.revoke.assert_not_called()


class TestPruneStaleState:
    async def test_syncs_display_names(self):
        mock_window = MagicMock(window_id="@0", window_name="test")
        with patch("ccgram.handlers.topics.topic_lifecycle.session_manager") as mock_sm:
            await prune_stale_state([mock_window])
            mock_sm.sync_display_names.assert_called_once_with([("@0", "test")])
            mock_sm.prune_stale_state.assert_called_once_with({"@0"})


class TestProbeTopicExistence:
    @pytest.mark.parametrize(
        ("window_id", "origin", "expect_kill"),
        [
            ("@0", "manual_discovered", False),
            ("@0", "ccgram_created", True),
            (HERDR_TARGET, "ccgram_created", True),
        ],
        ids=["manual_window_survives", "ccgram_window_killed", "herdr_target_killed"],
    )
    async def test_deleted_topic_unbinds_and_kills_only_what_ccgram_created(
        self, window_id: str, origin: str, expect_kill: bool
    ):
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("Topic_id_invalid")
        )
        adapter = MagicMock()
        adapter.close = AsyncMock(return_value=(True, "closed"))
        with (
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, window_id)]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id=window_id)
            )
            mock_wq.view_window.return_value = _window_view(origin, window_id)
            mock_tmux.kill_window = AsyncMock()
            await probe_topic_existence(bot, adapter)

        mock_router.unbind_thread.assert_called_once_with(
            1, 100, chat_id=42, retirement_reason="remote_deleted"
        )
        if expect_kill:
            adapter.close.assert_awaited_once_with(window_id)
        else:
            adapter.close.assert_not_awaited()

    async def test_flood_control_backs_off_without_suspending(self):
        """RetryAfter is chat-wide: it must not disable deleted-topic detection."""
        from ccgram.handlers.topics import topic_lifecycle as tl

        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=[RetryAfter(3), None]
        )
        with (
            patch.object(tl, "thread_router") as mock_router,
            patch.object(tl, "lifecycle_strategy") as mock_strategy,
            patch.object(tl.time, "monotonic", return_value=100.0) as mock_time,
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_strategy.should_skip_probe.return_value = False

            await probe_topic_existence(bot, MagicMock())
            bot.unpin_all_forum_topic_messages.assert_awaited_once_with(
                chat_id=42,
                message_thread_id=100,
                rate_limit_args=NO_RETRY_RATE_LIMIT_ARGS,
            )
            mock_strategy.record_probe_failure.assert_not_called()

            # Whole pass is paused while the chat is flood-limited.
            bot.unpin_all_forum_topic_messages.reset_mock()
            await probe_topic_existence(bot, MagicMock())
            bot.unpin_all_forum_topic_messages.assert_not_called()

            # Clearing Telegram's short delay does not spend another admin
            # request before the normal five-minute probe interval.
            tl._probe_backoff_until[42] = 0.0
            await probe_topic_existence(bot, MagicMock())
            bot.unpin_all_forum_topic_messages.assert_not_called()

            mock_time.return_value = 100.0 + tl.PROBE_INTERVAL
            await probe_topic_existence(bot, MagicMock())
            bot.unpin_all_forum_topic_messages.assert_called_once()

    async def test_flood_control_is_scoped_to_the_affected_chat(self):
        from ccgram.handlers.topics import topic_lifecycle as tl

        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=[RetryAfter(3), None, None]
        )
        bindings = [
            (1, 42, 100, "@0"),
            (1, 43, 100, "@1"),
        ]
        with (
            patch.object(tl, "thread_router") as mock_router,
            patch.object(tl.time, "monotonic", return_value=100.0) as mock_time,
        ):
            mock_router.iter_thread_bindings_with_chat.return_value = bindings
            await probe_topic_existence(bot, MagicMock())

            assert bot.unpin_all_forum_topic_messages.call_count == 2
            assert (
                bot.unpin_all_forum_topic_messages.call_args_list[0].kwargs["chat_id"]
                == 42
            )
            assert (
                bot.unpin_all_forum_topic_messages.call_args_list[1].kwargs["chat_id"]
                == 43
            )

            bot.unpin_all_forum_topic_messages.reset_mock()
            await probe_topic_existence(bot, MagicMock())
            bot.unpin_all_forum_topic_messages.assert_not_called()

            # Thread IDs are chat-local. Clearing chat 42's short backoff still
            # preserves the normal probe interval for its failed topic.
            tl._probe_backoff_until[42] = 0.0
            await probe_topic_existence(bot, MagicMock())
            bot.unpin_all_forum_topic_messages.assert_not_called()

            tl._probe_last_ts[(1, 43, 100)] = 200.0
            mock_time.return_value = 100.0 + tl.PROBE_INTERVAL
            await probe_topic_existence(bot, MagicMock())
            bot.unpin_all_forum_topic_messages.assert_called_once()
            assert bot.unpin_all_forum_topic_messages.call_args.kwargs["chat_id"] == 42

    async def test_probe_budget_allows_only_one_topic_per_chat(self):
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock()
        bindings = [
            (1, 42, 100, "@0"),
            (1, 42, 101, "@1"),
            (1, 43, 102, "@2"),
        ]
        with patch(
            "ccgram.handlers.topics.topic_lifecycle.thread_router"
        ) as mock_router:
            mock_router.iter_thread_bindings_with_chat.return_value = bindings
            await probe_topic_existence(bot, MagicMock())

        assert bot.unpin_all_forum_topic_messages.call_count == 2
        assert {
            call.kwargs["chat_id"]
            for call in bot.unpin_all_forum_topic_messages.call_args_list
        } == {42, 43}

    async def test_probes_run_shortly_after_boot(self):
        """time.monotonic() starts near zero: never-probed must still be due."""
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock()
        with (
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.time.monotonic",
                return_value=5.0,
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            await probe_topic_existence(bot, MagicMock())

        bot.unpin_all_forum_topic_messages.assert_called_once()

    async def test_probe_budget_rotates_across_cycles(self):
        """Bounded admin calls per cycle, least-recently-probed topic first."""
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock()
        topics = [
            (1, 1000 + i, 100 + i, f"@{i}") for i in range(3 * PROBE_MAX_PER_CYCLE)
        ]
        with patch(
            "ccgram.handlers.topics.topic_lifecycle.thread_router"
        ) as mock_router:
            mock_router.iter_thread_bindings_with_chat.return_value = topics

            probed = []
            for _ in range(3):
                bot.unpin_all_forum_topic_messages.reset_mock()
                await probe_topic_existence(bot, MagicMock())
                assert (
                    bot.unpin_all_forum_topic_messages.call_count == PROBE_MAX_PER_CYCLE
                )
                probed += [
                    c.kwargs["message_thread_id"]
                    for c in bot.unpin_all_forum_topic_messages.call_args_list
                ]

        # Every topic probed exactly once — no repeats while others are due.
        assert sorted(probed) == [t[2] for t in topics]

    async def test_unprobeable_windows_do_not_hold_cycle_slots(self):
        from ccgram.handlers.topics import topic_lifecycle as tl

        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock()
        blocked = [f"@blocked{i}" for i in range(PROBE_MAX_PER_CYCLE)]
        tl._probe_pin_disabled.update(blocked)
        try:
            with patch.object(tl, "thread_router") as mock_router:
                mock_router.iter_thread_bindings.return_value = [
                    *[(1, 200 + i, wid) for i, wid in enumerate(blocked)],
                    (1, 300, "@live"),
                ]
                mock_router.resolve_chat_id.return_value = 42
                await probe_topic_existence(bot, MagicMock())
        finally:
            tl._probe_pin_disabled.difference_update(blocked)

        bot.unpin_all_forum_topic_messages.assert_called_once()
        assert (
            bot.unpin_all_forum_topic_messages.call_args.kwargs["message_thread_id"]
            == 300
        )

    async def test_suspended_probe_skipped(self):
        bot = AsyncMock(spec=Bot)
        ws = terminal_poll_state.get_state("@0")
        ws.probe_failures = 999
        with patch(
            "ccgram.handlers.topics.topic_lifecycle.thread_router"
        ) as mock_router:
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            await probe_topic_existence(bot, MagicMock())
        bot.unpin_all_forum_topic_messages.assert_not_called()

    async def test_missing_pin_rights_disables_probe_without_suspending(self):
        from ccgram.handlers.topics import topic_lifecycle as tl

        bot = AsyncMock(spec=Bot)
        # Real Telegram error for unpin without can_pin_messages is lowercase.
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("not enough rights to manage pinned messages")
        )
        wid = "@probe-pin"
        tl._probe_pin_disabled.discard(wid)
        try:
            with (
                patch.object(tl, "thread_router") as mock_router,
                patch.object(tl, "lifecycle_strategy") as mock_strategy,
            ):
                mock_router.iter_thread_bindings.return_value = [(1, 100, wid)]
                mock_router.resolve_chat_id.return_value = 42
                mock_strategy.should_skip_probe.return_value = False

                await probe_topic_existence(bot, MagicMock())

                # Permission error must not count as a probe failure (no suspend).
                mock_strategy.record_probe_failure.assert_not_called()
                assert wid in tl._probe_pin_disabled

                # Next tick skips the window entirely — no further API call.
                bot.unpin_all_forum_topic_messages.reset_mock()
                await probe_topic_existence(bot, MagicMock())
                bot.unpin_all_forum_topic_messages.assert_not_called()
        finally:
            tl._probe_pin_disabled.discard(wid)


class TestAgentlessTopics:
    """CCGRAM_AGENTLESS_TOPIC_MINUTES: a topic exists only while an agent runs.

    A window that is still open but whose agent has exited (a bare shell) keeps
    its topic forever otherwise: dead-session cleanup only covers windows that
    are gone. Agent liveness is the hook-maintained session_map entry. Removal
    goes through ``retire_topic_binding`` so failures are retried by the drain.
    """

    @pytest.fixture(autouse=True)
    def _clean_timers(self):
        from ccgram.handlers.topics import topic_lifecycle

        topic_lifecycle._agentless_since.clear()
        yield
        topic_lifecycle._agentless_since.clear()

    async def _sweep(
        self,
        *,
        minutes: int = 10,
        agents: frozenset[str] | set[str] | None = frozenset(),
        live: tuple[str, ...] = ("@1",),
        elapsed: tuple[float, ...] = (0.0, 601.0),
        outcome: str = "deleted",
        chat_id: int | None = -100200,
    ) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
        """Run one sweep per entry in *elapsed* (seconds on the monotonic clock)."""
        from ccgram.handlers.topics.topic_lifecycle import check_agentless_topics

        client = AsyncMock()
        windows = [WindowRef(window_id=w, window_name="w", cwd="/p") for w in live]
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch("ccgram.handlers.topics.topic_lifecycle.thread_router") as mock_tr,
            patch("ccgram.handlers.topics.topic_lifecycle.time") as mock_time,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ) as clear,
            patch(
                "ccgram.handlers.topics.topic_deletion.retire_topic_binding",
                new_callable=AsyncMock,
                return_value=outcome,
            ) as retire,
            patch(
                "ccgram.handlers.topics.topic_lifecycle._agent_window_ids",
                new_callable=AsyncMock,
                return_value=None if agents is None else set(agents),
            ),
        ):
            mock_config.agentless_topic_minutes = minutes
            mock_tr.iter_thread_bindings_with_chat.return_value = [
                (100, chat_id, 42, "@1")
            ]
            for now in elapsed:
                mock_time.monotonic.return_value = now
                await check_agentless_topics(client, windows)
            # Run the pre-delete hook the way retire_topic_binding would.
            if retire.await_args is not None:
                await retire.await_args.kwargs["before_delete"]()
        return client, retire, clear

    async def test_topic_of_agentless_window_is_retired_after_the_grace(self) -> None:
        client, retire, clear = await self._sweep()
        retire.assert_awaited_once_with(
            client,
            100,
            42,
            "@1",
            chat_id=-100200,
            before_delete=ANY,
            retirement_reason="agentless",
        )
        # The window is alive: window-dead cleanup must not run for it.
        assert clear.await_args is not None
        assert clear.await_args.kwargs["window_dead"] is False
        assert clear.await_args.kwargs["chat_id"] == -100200

    async def test_nothing_is_retired_within_the_grace(self) -> None:
        _, retire, _ = await self._sweep(elapsed=(0.0, 599.0))
        retire.assert_not_awaited()

    async def test_first_sight_only_starts_the_clock(self) -> None:
        _, retire, _ = await self._sweep(elapsed=(99999.0,))
        retire.assert_not_awaited()

    async def test_window_running_an_agent_keeps_its_topic(self) -> None:
        _, retire, _ = await self._sweep(agents={"@1"})
        retire.assert_not_awaited()

    async def test_agent_returning_resets_the_clock(self) -> None:
        from ccgram.handlers.topics import topic_lifecycle

        await self._sweep(elapsed=(0.0,))
        assert topic_lifecycle._agentless_since
        await self._sweep(agents={"@1"}, elapsed=(300.0,))
        assert not topic_lifecycle._agentless_since
        _, retire, _ = await self._sweep(elapsed=(601.0,))
        retire.assert_not_awaited()

    async def test_missing_window_is_left_to_the_dead_path(self) -> None:
        _, retire, _ = await self._sweep(live=("@9",))
        retire.assert_not_awaited()

    async def test_unreadable_session_map_defers(self) -> None:
        _, retire, _ = await self._sweep(agents=None)
        retire.assert_not_awaited()

    async def test_off_by_default(self) -> None:
        _, retire, _ = await self._sweep(minutes=0)
        retire.assert_not_awaited()

    async def test_binding_without_a_chat_is_skipped(self) -> None:
        _, retire, _ = await self._sweep(chat_id=None)
        retire.assert_not_awaited()

    @pytest.mark.parametrize(
        ("outcome", "clock_kept"),
        [
            pytest.param("deleted", False, id="deleted"),
            pytest.param("failed", False, id="failed-left-to-the-drain"),
            pytest.param("protected_provisioning", True, id="protected-retries"),
        ],
    )
    async def test_protected_outcome_keeps_the_clock(
        self, outcome: str, clock_kept: bool
    ) -> None:
        from ccgram.handlers.topics import topic_lifecycle

        await self._sweep(outcome=outcome)
        assert bool(topic_lifecycle._agentless_since) is clock_kept

    async def test_agent_windows_come_from_the_session_map(self) -> None:
        from ccgram.handlers.topics.topic_lifecycle import _agent_window_ids

        raw = {"main:@1": {"session_id": "s", "cwd": "/p"}, "other:@7": {}}
        with (
            patch(
                "ccgram.handlers.topics.topic_lifecycle.read_session_map_raw",
                new_callable=AsyncMock,
                return_value=raw,
            ),
            patch(
                "ccgram.handlers.topics.topic_lifecycle.session_map_prefix",
                return_value="main:",
            ),
        ):
            assert await _agent_window_ids() == {"@1"}
