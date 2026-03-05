from telecodex_bot.runner import CodexRunner


def test_extract_assistant_text_skips_codex_noise() -> None:
    lines = [
        "[stderr] OpenAI Codex v0.111.0 (research preview)\n",
        "[stderr] workdir: /home/codex/ort_bot\n",
        "[stderr] Session context:\n",
        "[stderr] session_id=abc\n",
        "[stderr] User task:\n",
        "[stderr] Test\n",
        "[stderr] codex\n",
        "[stderr] На связи.\n",
        "[stderr] На связи.\n",
        "[stderr] Готов выполнить задачу.\n",
        "[stderr] tokens used\n",
        "[stderr] 1,707\n",
    ]

    text = CodexRunner._extract_assistant_text(lines)

    assert text == "На связи.\nГотов выполнить задачу."
