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


def test_extract_assistant_text_handles_nested_stderr_prefix() -> None:
    lines = [
        "[stderr] [stderr] workdir: /home/codex/ort_bot\n",
        "[stderr] [stderr] Session context:\n",
        "[stderr] [stderr] User task:\n",
        "[stderr] [stderr] Test\n",
        "[stderr] [stderr] codex\n",
        "[stderr] [stderr] Проверка\n",
        "[stderr] [stderr] На связи. Готов к работе.\n",
    ]

    text = CodexRunner._extract_assistant_text(lines)

    assert text == "Проверка\nНа связи. Готов к работе."


def test_sanitize_history_for_prompt_removes_technical_lines() -> None:
    content = (
        "[stderr] OpenAI Codex v0.111.0 (research preview)\n"
        "[stderr] session id: 123\n"
        "[stderr] User task:\n"
        "[stderr] Test\n"
        "[stderr] codex\n"
        "[stderr] Полезный ответ\n"
    )

    clean = CodexRunner._sanitize_history_for_prompt(content)

    assert clean == "Полезный ответ"


def test_sanitize_history_for_prompt_collapses_multiline_history() -> None:
    content = (
        "[stderr] Первая строка старого ответа\n"
        "[stderr] Вторая строка старого ответа\n"
        "[stderr] Третья строка старого ответа\n"
    )

    clean = CodexRunner._sanitize_history_for_prompt(content)

    assert clean == "Первая строка старого ответа Вторая строка старого ответа Третья строка старого ответа"
