"""DM_NEG_CHAT_20260910: Telegram chat_id negativo nunca vira sessão ':dm:' no wake nem na assinatura."""
from gateway import kanban_watchers as kw
from hermes_cli import kanban_db


def test_wake_negative_chat_id_with_dm_row_is_group():
    sub = {"chat_type": "dm", "chat_id": "-1004309874643", "delivery_metadata": None}
    assert kw._sub_chat_type(sub, "telegram") == "group"


def test_wake_positive_chat_id_keeps_dm():
    assert kw._sub_chat_type({"chat_type": "dm", "chat_id": "5551234"}, "telegram") == "dm"


def test_wake_empty_defaults_to_group_and_metadata_wins():
    assert kw._sub_chat_type({"chat_type": "", "chat_id": "-100"}, "telegram") == "group"
    meta = {"chat_type": "supergroup"}
    assert kw._sub_chat_type({"chat_type": None, "chat_id": "-100", "delivery_metadata": meta}, "telegram") == "supergroup"


def test_wake_other_platform_untouched():
    assert kw._sub_chat_type({"chat_type": "dm", "chat_id": "-100"}, "api_server") == "dm"


def test_subscribe_default_follows_chat_id_sign():
    assert kanban_db._default_sub_chat_type("telegram", "-1004309874643") == "group"
    assert kanban_db._default_sub_chat_type("telegram", "5551234") == "dm"
    assert kanban_db._default_sub_chat_type("api_server", "-100") == "dm"
