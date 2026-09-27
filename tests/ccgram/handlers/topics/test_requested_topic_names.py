"""Tests for requested_topic_names — the name a user gave a topic before it had a window."""

from unittest.mock import MagicMock

import pytest

from ccgram.handlers.topics import requested_topic_names
from ccgram.handlers.topics.requested_topic_names import (
    remember,
    remember_from_message,
    requested_name,
)

CHAT_ID = -100
THREAD_ID = 42


@pytest.fixture(autouse=True)
def _reset():
    requested_topic_names.reset()
    yield
    requested_topic_names.reset()


def _topic_message(created_name: str | None) -> MagicMock:
    """A message posted in a topic; Telegram points reply_to_message at the topic's creation."""
    message = MagicMock()
    message.chat.id = CHAT_ID
    message.message_thread_id = THREAD_ID
    if created_name is None:
        message.reply_to_message = None
    else:
        message.reply_to_message.forum_topic_created.name = created_name
    return message


class TestRememberFromMessage:
    def test_first_message_records_the_name_the_topic_was_created_with(self) -> None:
        remember_from_message(_topic_message("invoices"))

        assert requested_name(CHAT_ID, THREAD_ID) == "invoices"

    def test_message_without_a_topic_creation_reference_records_nothing(self) -> None:
        remember_from_message(_topic_message(None))

        assert requested_name(CHAT_ID, THREAD_ID) is None

    def test_reply_to_an_ordinary_message_records_nothing(self) -> None:
        message = _topic_message(None)
        message.reply_to_message = MagicMock()
        message.reply_to_message.forum_topic_created = None

        remember_from_message(message)

        assert requested_name(CHAT_ID, THREAD_ID) is None

    def test_a_later_rename_wins_over_the_creation_name(self) -> None:
        remember(CHAT_ID, THREAD_ID, "invoices-renamed")

        remember_from_message(_topic_message("invoices"))

        assert requested_name(CHAT_ID, THREAD_ID) == "invoices-renamed"


class TestRemember:
    def test_blank_name_is_ignored(self) -> None:
        remember(CHAT_ID, THREAD_ID, "   ")

        assert requested_name(CHAT_ID, THREAD_ID) is None

    def test_name_is_trimmed(self) -> None:
        remember(CHAT_ID, THREAD_ID, "  invoices  ")

        assert requested_name(CHAT_ID, THREAD_ID) == "invoices"

    def test_names_are_per_chat_and_thread(self) -> None:
        remember(CHAT_ID, THREAD_ID, "invoices")

        assert requested_name(CHAT_ID, THREAD_ID + 1) is None
        assert requested_name(CHAT_ID - 1, THREAD_ID) is None
