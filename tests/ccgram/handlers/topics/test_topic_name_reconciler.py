"""tmux → Telegram topic name sync (the reverse of topic_edited_handler)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.topics import topic_lifecycle
from ccgram.handlers.topics.topic_lifecycle import sync_topic_names_from_windows
from ccgram.multiplexer.base import WindowRef

USER_ID = 1
CHAT_ID = -100
THREAD_ID = 42
WINDOW_ID = "@33"


@pytest.fixture(autouse=True)
def _reset_pending():
    topic_lifecycle._pending_window_names.clear()
    topic_lifecycle._synced_window_names.clear()
    yield
    topic_lifecycle._pending_window_names.clear()
    topic_lifecycle._synced_window_names.clear()


@pytest.fixture
def router():
    with patch("ccgram.handlers.topics.topic_lifecycle.thread_router") as mock_router:
        mock_router.iter_thread_bindings.return_value = [
            (USER_ID, THREAD_ID, WINDOW_ID)
        ]
        mock_router.resolve_chat_id.return_value = CHAT_ID
        mock_router.get_display_name.return_value = "old-name"
        yield mock_router


@pytest.fixture
def session():
    with patch("ccgram.handlers.topics.topic_lifecycle.session_manager") as mock_sm:
        yield mock_sm


@pytest.fixture
def sync():
    with patch(
        "ccgram.handlers.status.topic_emoji.sync_topic_name", new_callable=AsyncMock
    ) as mock_sync:
        yield mock_sync


def _window(name: str, window_id: str = WINDOW_ID) -> WindowRef:
    return WindowRef(window_id=window_id, window_name=name, cwd="/tmp")


class TestRenamePushedToTelegram:
    async def test_stable_rename_is_pushed_once(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        client = MagicMock()

        await sync_topic_names_from_windows(client, [_window("new-name")])
        await sync_topic_names_from_windows(client, [_window("new-name")])

        sync.assert_awaited_once_with(client, CHAT_ID, THREAD_ID, "new-name")
        session.set_display_name.assert_called_once_with(WINDOW_ID, "new-name")

    async def test_display_name_refresh_between_ticks_does_not_swallow_rename(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        """prune_stale_state copies the live name into the display name every
        60s without touching Telegram; a rename seen once before that refresh
        must still be pushed on the next tick."""
        client = MagicMock()

        await sync_topic_names_from_windows(client, [_window("new-name")])
        router.get_display_name.return_value = "new-name"  # the 60s refresh ran
        await sync_topic_names_from_windows(client, [_window("new-name")])

        sync.assert_awaited_once_with(client, CHAT_ID, THREAD_ID, "new-name")

    async def test_name_that_settles_after_churn_is_pushed_once(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        client = MagicMock()

        await sync_topic_names_from_windows(client, [_window("src")])
        await sync_topic_names_from_windows(client, [_window("lib")])
        await sync_topic_names_from_windows(client, [_window("lib")])

        sync.assert_awaited_once_with(client, CHAT_ID, THREAD_ID, "lib")


class TestRenameNotPushed:
    async def test_single_sighting_is_debounced(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        await sync_topic_names_from_windows(MagicMock(), [_window("new-name")])

        sync.assert_not_awaited()
        session.set_display_name.assert_not_called()

    async def test_unchanged_name_is_ignored(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        for _ in range(3):
            await sync_topic_names_from_windows(MagicMock(), [_window("old-name")])

        sync.assert_not_awaited()

    async def test_bot_status_prefix_on_stored_name_is_not_a_rename(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        router.get_display_name.return_value = "\U0001f7e2 old-name"

        for _ in range(2):
            await sync_topic_names_from_windows(MagicMock(), [_window("old-name")])

        sync.assert_not_awaited()

    async def test_window_absent_from_live_list_is_skipped(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        for _ in range(2):
            await sync_topic_names_from_windows(MagicMock(), [_window("x", "@99")])

        sync.assert_not_awaited()

    async def test_hidden_underscore_window_is_skipped(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        for _ in range(2):
            await sync_topic_names_from_windows(MagicMock(), [_window("_scratch")])

        sync.assert_not_awaited()

    async def test_window_without_display_name_is_skipped(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        """get_display_name falls back to the window_id when nothing is stored."""
        router.get_display_name.return_value = WINDOW_ID

        for _ in range(2):
            await sync_topic_names_from_windows(MagicMock(), [_window("new-name")])

        sync.assert_not_awaited()

    async def test_reverted_rename_resets_the_debounce(
        self, router: MagicMock, session: MagicMock, sync: AsyncMock
    ) -> None:
        client = MagicMock()

        await sync_topic_names_from_windows(client, [_window("new-name")])
        await sync_topic_names_from_windows(client, [_window("old-name")])
        await sync_topic_names_from_windows(client, [_window("new-name")])

        sync.assert_not_awaited()
