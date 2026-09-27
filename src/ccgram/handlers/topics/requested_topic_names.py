"""Names users gave topics before those topics had a window.

A topic created in Telegram ("invoices") gets its window only after the
directory browser, and the window used to be named after the chosen
directory ("daily"), which then overwrote the topic title. This module keeps
the name the user typed so ``launch_window`` can name the window after it.

Sources, in priority order: a rename of the still-unbound topic
(``forum_topic_edited``), then the creation name Telegram attaches to every
message posted in a topic (``reply_to_message.forum_topic_created``).
In memory only: the gap between creating a topic and picking a directory is
seconds, and the next message re-supplies the creation name after a restart.
"""

from __future__ import annotations

from typing import Any

_names: dict[tuple[int, int], str] = {}


def remember(chat_id: int, thread_id: int, name: str) -> None:
    """Record *name* for the topic; blank names are ignored."""
    clean = name.strip()
    if clean:
        _names[(chat_id, thread_id)] = clean


def remember_from_message(message: Any) -> None:
    """Record the topic's creation name from a message posted in it.

    Never overrides a name already recorded (a later rename wins).
    """
    reply = getattr(message, "reply_to_message", None)
    created = getattr(reply, "forum_topic_created", None) if reply else None
    name = getattr(created, "name", None) if created else None
    thread_id = getattr(message, "message_thread_id", None)
    if not isinstance(name, str) or thread_id is None:
        return
    key = (message.chat.id, thread_id)
    if key not in _names:
        remember(*key, name)


def requested_name(chat_id: int, thread_id: int) -> str | None:
    return _names.get((chat_id, thread_id))


def forget(chat_id: int, thread_id: int) -> None:
    _names.pop((chat_id, thread_id), None)


def reset() -> None:
    _names.clear()
