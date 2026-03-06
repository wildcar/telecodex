import asyncio
from pathlib import Path

import pytest

from telecodex_bot.runner import CodexRunner


def test_extract_assistant_text_skips_codex_noise() -> None:
    lines = [
        "[stderr] OpenAI Codex v0.111.0 (research preview)\n",
        "[stderr] workdir: /home/codex/ort_bot\n",
        "[stderr] Test\n",
        "[stderr] codex\n",
        "[stderr] На связи.\n",
        "[stderr] На связи.\n",
        "[stderr] Готов выполнить задачу.\n",
        "[stderr] tokens used\n",
        "[stderr] 1,707\n",
    ]

    text = CodexRunner._extract_assistant_text(lines)

    assert text == "Test\nНа связи.\nГотов выполнить задачу."


def test_extract_assistant_text_handles_nested_stderr_prefix() -> None:
    lines = [
        "[stderr] [stderr] workdir: /home/codex/ort_bot\n",
        "[stderr] [stderr] Проверка\n",
        "[stderr] [stderr] На связи. Готов к работе.\n",
    ]

    text = CodexRunner._extract_assistant_text(lines)

    assert text == "Проверка\nНа связи. Готов к работе."


def test_extract_assistant_text_skips_user_prompt_echo_and_duplicates() -> None:
    lines = [
        "[stderr] Так админка не мешает обычному UX.\n",
        "[stderr] Так админка не мешает обычному UX.\n",
        "[stderr] Если хотите, следующим сообщением могу уже предложить конкретно:\n",
        "[stderr] Если хотите, следующим сообщением могу уже предложить конкретно:\n",
        "[stderr] 1. финальную структуру кнопок для пользователя,\n",
    ]

    text = CodexRunner._extract_assistant_text(lines, user_prompt="Так админка не мешает обычному UX.")

    assert text == "Если хотите, следующим сообщением могу уже предложить конкретно:\n1. финальную структуру кнопок для пользователя,"


def test_extract_progress_text_keeps_human_status_only() -> None:
    assert CodexRunner._extract_progress_text("[stderr] Сначала посмотрю FS.md и bot.py.\n") == "Сначала посмотрю FS.md и bot.py."
    assert CodexRunner._extract_progress_text("[stderr] exec\n") is None
    assert CodexRunner._extract_progress_text("[stderr] /bin/bash -lc \"sed -n '1,220p' FS.md\"\n") is None
    assert CodexRunner._extract_progress_text("[stderr] requirements.txt\n") is None


def test_build_command_keeps_full_prompt_as_single_argument() -> None:
    runner = CodexRunner("codex exec --model gpt-5", timeout_sec=1)

    command = runner._build_command("line one\nline two", None)

    assert command == ["codex", "exec", "--model", "gpt-5", "line one\nline two"]


def test_build_command_uses_native_resume_when_ref_exists() -> None:
    runner = CodexRunner("codex exec --model gpt-5", timeout_sec=1)

    command = runner._build_command("continue", "12345678-1234-1234-1234-1234567890ab")

    assert command == [
        "codex",
        "exec",
        "--model",
        "gpt-5",
        "resume",
        "12345678-1234-1234-1234-1234567890ab",
        "continue",
    ]


def test_extract_codex_session_id_from_raw_output() -> None:
    lines = [
        "[stderr] OpenAI Codex v0.111.0 (research preview)\n",
        "[stderr] session id: 12345678-1234-1234-1234-1234567890ab\n",
    ]

    assert CodexRunner._extract_codex_session_id(lines) == "12345678-1234-1234-1234-1234567890ab"


@pytest.mark.asyncio
async def test_run_result_contains_command_and_raw_output(tmp_path: Path) -> None:
    script = tmp_path / "fake_codex.sh"
    script.write_text("#!/usr/bin/env bash\nprintf 'raw stdout\\n'\nprintf 'raw stderr\\n' >&2\n", encoding="utf-8")
    script.chmod(0o755)
    runner = CodexRunner(str(script), timeout_sec=5)

    result = await runner.run(
        project_path=str(tmp_path),
        codex_session_id=None,
        user_prompt="Привет",
        on_progress=None,
        cancel_event=asyncio.Event(),
    )

    assert result.command.startswith(str(script))
    assert "Привет" in result.command
    assert result.raw_output == "[stdout] raw stdout\n[stderr] raw stderr\n"
    assert result.codex_session_id is None


@pytest.mark.asyncio
async def test_run_uses_resume_ref_without_rebuilding_prompt(tmp_path: Path) -> None:
    script = tmp_path / "fake_codex.sh"
    script.write_text("#!/usr/bin/env bash\nprintf 'session id: 87654321-4321-4321-4321-ba0987654321\\n' >&2\n", encoding="utf-8")
    script.chmod(0o755)
    runner = CodexRunner(str(script), timeout_sec=5)

    result = await runner.run(
        project_path=str(tmp_path),
        codex_session_id="12345678-1234-1234-1234-1234567890ab",
        user_prompt="Продолжай",
        on_progress=None,
        cancel_event=asyncio.Event(),
    )

    assert " resume 12345678-1234-1234-1234-1234567890ab " in result.command
    assert result.codex_session_id == "87654321-4321-4321-4321-ba0987654321"
