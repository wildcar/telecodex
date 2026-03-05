from telecodex_bot.streaming import TelegramStreamEditor


def test_tail_keeps_right_part() -> None:
    assert TelegramStreamEditor.tail("abcdef", 4) == "cdef"
    assert TelegramStreamEditor.tail("abc", 10) == "abc"
