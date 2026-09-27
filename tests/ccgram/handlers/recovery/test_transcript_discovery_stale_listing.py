"""Regression: a stale window listing must not switch a live agent's provider.

The status poller takes one window listing per cycle and ticks every bound
window from it, awaiting Telegram between windows, so a snapshot can be
seconds old by the time a window is ticked. On 2026-09-27 a shell-origin
window started Claude; SessionStart wrote its session_map entry and the
monitor corrected the provider to claude, then the same cycle's tick still
saw ``zsh`` from its snapshot, switched the provider back to shell, and the
switch cleared the fresh entry. The topic stopped receiving the session.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccgram.handlers.recovery import transcript_discovery
from ccgram.handlers.recovery.transcript_discovery import _detect_and_apply_provider
from ccgram.multiplexer.base import WindowRef
from ccgram.window_state_ports import identity_state

_MODULE = "ccgram.handlers.recovery.transcript_discovery"
WINDOW_ID = "@15"


def _window(pane_current_command: str) -> WindowRef:
    return WindowRef(
        window_id=WINDOW_ID,
        window_name="sayo-4",
        cwd="/Users/me/cc/daily",
        pane_current_command=pane_current_command,
    )


def _claude_identity() -> identity_state.IdentityProjection:
    return identity_state.IdentityProjection(
        window_id=WINDOW_ID,
        cwd="/Users/me/cc/daily",
        session_id="3077bd28",
        transcript_path=Path("/Users/me/.claude/projects/-daily/3077bd28.jsonl"),
        provider_name="claude",
        window_name="sayo-4",
        approval_mode="default",
    )


@pytest.fixture
def session_manager(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_sm = MagicMock()
    monkeypatch.setattr(f"{_MODULE}.session_manager", mock_sm)
    monkeypatch.setattr(
        f"{_MODULE}.identity_state.is_provider_manually_overridden",
        lambda _wid: False,
    )
    return mock_sm


def _live_pane(monkeypatch: pytest.MonkeyPatch, window: WindowRef | None) -> None:
    mux = MagicMock()
    mux.find_window_by_id = AsyncMock(return_value=window)
    monkeypatch.setattr(f"{_MODULE}.tmux_manager", mux)


class TestShellOriginWindowStartingAnAgent:
    @pytest.fixture(autouse=True)
    def _shell_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            transcript_discovery, "_is_agent_origin", lambda _wid, _ident: False
        )

    async def test_stale_shell_snapshot_does_not_switch_a_live_agent_to_shell(
        self, monkeypatch: pytest.MonkeyPatch, session_manager: MagicMock
    ) -> None:
        # Snapshot taken before Claude started; the pane now runs Claude, whose
        # process title is its version number.
        _live_pane(monkeypatch, _window("2.1.283"))

        agent_exited = await _detect_and_apply_provider(
            WINDOW_ID, _claude_identity(), _window("zsh")
        )

        assert agent_exited is False
        session_manager.set_window_provider.assert_not_called()

    async def test_a_real_return_to_the_shell_still_switches_to_shell(
        self, monkeypatch: pytest.MonkeyPatch, session_manager: MagicMock
    ) -> None:
        _live_pane(monkeypatch, _window("zsh"))
        monkeypatch.setattr(
            f"{_MODULE}.identity_state.clear_transcript_path", MagicMock()
        )
        ensure_setup = AsyncMock()
        monkeypatch.setattr(
            "ccgram.handlers.shell.shell_prompt_orchestrator.ensure_setup",
            ensure_setup,
        )

        await _detect_and_apply_provider(WINDOW_ID, _claude_identity(), _window("zsh"))

        session_manager.set_window_provider.assert_called_once_with(
            WINDOW_ID, "shell", cwd="/Users/me/cc/daily"
        )


class TestAgentOriginWindow:
    @pytest.fixture(autouse=True)
    def _agent_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            transcript_discovery, "_is_agent_origin", lambda _wid, _ident: True
        )

    async def test_stale_shell_snapshot_does_not_report_the_agent_exited(
        self, monkeypatch: pytest.MonkeyPatch, session_manager: MagicMock
    ) -> None:
        _live_pane(monkeypatch, _window("2.1.283"))

        agent_exited = await _detect_and_apply_provider(
            WINDOW_ID, _claude_identity(), _window("zsh")
        )

        assert agent_exited is False
        session_manager.set_window_provider.assert_not_called()

    async def test_a_real_exit_to_the_shell_is_still_reported(
        self, monkeypatch: pytest.MonkeyPatch, session_manager: MagicMock
    ) -> None:
        _live_pane(monkeypatch, _window("zsh"))

        agent_exited = await _detect_and_apply_provider(
            WINDOW_ID, _claude_identity(), _window("zsh")
        )

        assert agent_exited is True
        session_manager.set_window_provider.assert_not_called()
