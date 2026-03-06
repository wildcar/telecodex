import asyncio
from pathlib import Path

import pytest

from telecodex_bot.runner import CodexRunner
from telecodex_bot.repository import SessionRecord


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


def test_extract_assistant_text_skips_user_prompt_echo_and_duplicates() -> None:
    lines = [
        "[stderr] User task:\n",
        "[stderr] Так админка не мешает обычному UX.\n",
        "[stderr] Так админка не мешает обычному UX.\n",
        "[stderr] Если хотите, следующим сообщением могу уже предложить конкретно:\n",
        "[stderr] Если хотите, следующим сообщением могу уже предложить конкретно:\n",
        "[stderr] 1. финальную структуру кнопок для пользователя,\n",
    ]

    text = CodexRunner._extract_assistant_text(lines, user_prompt="Так админка не мешает обычному UX.")

    assert text == "Если хотите, следующим сообщением могу уже предложить конкретно:\n1. финальную структуру кнопок для пользователя,"


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


def test_extract_progress_text_keeps_human_status_only() -> None:
    assert CodexRunner._extract_progress_text("[stderr] Сначала посмотрю FS.md и bot.py.\n") == "Сначала посмотрю FS.md и bot.py."
    assert CodexRunner._extract_progress_text("[stderr] exec\n") is None
    assert CodexRunner._extract_progress_text("[stderr] /bin/bash -lc \"sed -n '1,220p' FS.md\"\n") is None
    assert CodexRunner._extract_progress_text("[stderr] requirements.txt\n") is None


def test_build_command_keeps_full_prompt_as_single_argument() -> None:
    runner = CodexRunner("codex exec --model gpt-5", timeout_sec=1)

    command = runner._build_command("line one\nline two")

    assert command == ["codex", "exec", "--model", "gpt-5", "line one\nline two"]


@pytest.mark.asyncio
async def test_run_result_contains_command_and_raw_output(tmp_path: Path) -> None:
    script = tmp_path / "fake_codex.sh"
    script.write_text("#!/usr/bin/env bash\nprintf 'raw stdout\\n'\nprintf 'raw stderr\\n' >&2\n", encoding="utf-8")
    script.chmod(0o755)
    runner = CodexRunner(str(script), timeout_sec=5)
    session = SessionRecord(
        id="12345678-1234-1234-1234-1234567890ab",
        project_name="demo",
        project_path=str(tmp_path),
        alias=None,
        created_at="2026-03-05T10:00:00+00:00",
        updated_at="2026-03-05T10:01:00+00:00",
        history_log_path=str(tmp_path / "history.log"),
        codex_resume_ref=None,
    )

    result = await runner.run(
        session=session,
        user_prompt="Привет",
        recent_history=[],
        on_output=_noop_output,
        on_progress=None,
        cancel_event=asyncio.Event(),
    )

    assert result.command.startswith(str(script))
    assert "User task:" in result.command
    assert result.raw_output == "[stdout] raw stdout\n[stderr] raw stderr\n"


async def _noop_output(_: str) -> None:
    return None
