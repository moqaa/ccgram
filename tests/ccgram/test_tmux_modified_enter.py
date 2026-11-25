from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.multiplexer.tmux import TmuxManager


@pytest.mark.parametrize(
    ("text", "literal", "expected", "wire_literal"),
    [
        ("S-Enter", False, "\x1b[13;2u", True),
        ("Enter", False, "Enter", False),
        ("S-Enter", True, "S-Enter", True),
    ],
)
def test_modified_enter_preserves_editor_newlines(
    text, literal, expected, wire_literal
):
    pane = MagicMock()
    TmuxManager._send_pane_input(pane, text, enter=False, literal=literal)
    pane.send_keys.assert_called_once_with(expected, enter=False, literal=wire_literal)


async def _typed(text: str) -> list[tuple[str, bool]]:
    manager = TmuxManager.__new__(TmuxManager)
    calls: list[tuple[str, bool]] = []

    def record(_window_id, chars, *, enter, literal, **_):
        calls.append((chars, literal) if not enter else ("<Enter>", literal))
        return True

    with (
        patch.object(manager, "_ensure_vim_insert_mode", new_callable=AsyncMock),
        patch.object(manager, "_pane_send", side_effect=record),
        patch("ccgram.multiplexer.tmux.asyncio.sleep", new_callable=AsyncMock),
    ):
        assert await manager._send_literal_then_enter("@1", text) is True
    return calls


async def test_line_breaks_are_typed_as_shift_enter_then_submitted() -> None:
    # tmux send-keys -l drops "\n"; a bare Enter would submit the first line.
    assert await _typed("uno\r\ndos\n\ntres") == [
        ("uno", True),
        ("S-Enter", False),
        ("dos", True),
        ("S-Enter", False),
        ("S-Enter", False),
        ("tres", True),
        ("<Enter>", False),
    ]


async def test_single_line_is_typed_in_one_send() -> None:
    assert await _typed("hola") == [("hola", True), ("<Enter>", False)]


async def test_bang_command_keeps_its_line_breaks() -> None:
    assert await _typed("!echo a\necho b") == [
        ("!", True),
        ("echo a", True),
        ("S-Enter", False),
        ("echo b", True),
        ("<Enter>", False),
    ]
