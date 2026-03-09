import asyncio
from pathlib import Path

import pytest

from telecodex.runner import CodexJsonEventParser, CodexRunner


def test_parser_extracts_item_message_delta() -> None:
    parser = CodexJsonEventParser()

    events = parser.parse_line('{"type":"item.message.delta","delta":"hello"}\n')

    assert len(events) == 1
    assert events[0].kind == "assistant_delta"
    assert events[0].text == "hello"


def test_parser_extracts_commentary_progress() -> None:
    parser = CodexJsonEventParser()

    events = parser.parse_line(
        '{"type":"response_item","payload":{"type":"message","role":"assistant","phase":"commentary","content":[{"type":"output_text","text":"Reviewing FS.md"}]}}\n'
    )

    assert len(events) == 1
    assert events[0].kind == "progress"
    assert events[0].text == "Reviewing FS.md"


def test_parser_extracts_completed_agent_message() -> None:
    parser = CodexJsonEventParser()

    events = parser.parse_line(
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Final reply"}}\n'
    )

    assert len(events) == 1
    assert events[0].kind == "assistant_snapshot"
    assert events[0].text == "Final reply"


def test_parser_extracts_session_id_from_thread_started() -> None:
    parser = CodexJsonEventParser()

    events = parser.parse_line('{"type":"thread.started","thread_id":"12345678-1234-1234-1234-1234567890ab"}\n')

    assert len(events) == 1
    assert events[0].kind == "session"
    assert events[0].session_id == "12345678-1234-1234-1234-1234567890ab"


def test_parser_handles_invalid_json_gracefully() -> None:
    parser = CodexJsonEventParser()

    events = parser.parse_line("{not-json}\n")

    assert len(events) == 1
    assert events[0].kind == "invalid_json"


def test_build_command_keeps_full_prompt_as_single_argument() -> None:
    runner = CodexRunner("codex exec --model gpt-5", timeout_sec=1)

    command = runner._build_command("line one\nline two", None)

    assert command == ["codex", "exec", "--model", "gpt-5", "--json", "line one\nline two"]


def test_build_command_uses_native_resume_when_ref_exists() -> None:
    runner = CodexRunner("codex exec --model gpt-5", timeout_sec=1)

    command = runner._build_command("continue", "12345678-1234-1234-1234-1234567890ab")

    assert command == [
        "codex",
        "exec",
        "--model",
        "gpt-5",
        "resume",
        "--json",
        "12345678-1234-1234-1234-1234567890ab",
        "continue",
    ]


def test_extract_codex_session_id_from_raw_output() -> None:
    assert (
        CodexRunner._extract_codex_session_id_from_text(
            "session id: 12345678-1234-1234-1234-1234567890ab\n"
        )
        == "12345678-1234-1234-1234-1234567890ab"
    )


@pytest.mark.asyncio
async def test_run_result_contains_command_and_streamed_output(tmp_path: Path) -> None:
    script = tmp_path / "fake_codex.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' '{\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"assistant\",\"phase\":\"commentary\",\"content\":[{\"type\":\"output_text\",\"text\":\"Reviewing FS.md\"}]}}'\n"
        "printf '%s\\n' '{\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"assistant\",\"content\":[{\"type\":\"output_text\",\"text\":\"Acce\"}]}}'\n"
        "printf '%s\\n' '{\"type\":\"item.message.delta\",\"delta\":\"pted\"}'\n"
        "printf 'session id: 87654321-4321-4321-4321-ba0987654321\\n' >&2\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = CodexRunner(str(script), timeout_sec=5)
    progress_updates: list[str] = []
    message_updates: list[str] = []

    result = await runner.run(
        project_path=str(tmp_path),
        codex_session_id=None,
        user_prompt="Hello",
        on_progress=_collector(progress_updates),
        on_message=_collector(message_updates),
        cancel_event=asyncio.Event(),
    )

    assert result.command.startswith(str(script))
    assert "--json" in result.command
    assert result.raw_output.count("[stdout]") == 3
    assert result.assistant_text == "Accepted"
    assert result.output == "Accepted"
    assert result.codex_session_id == "87654321-4321-4321-4321-ba0987654321"
    assert progress_updates == ["Reviewing FS.md"]
    assert message_updates == ["Acce", "Accepted"]


@pytest.mark.asyncio
async def test_run_uses_resume_with_json_mode(tmp_path: Path) -> None:
    script = tmp_path / "fake_codex.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' '{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\",\"last_agent_message\":\"Done\"}}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = CodexRunner(str(script), timeout_sec=5)

    result = await runner.run(
        project_path=str(tmp_path),
        codex_session_id="12345678-1234-1234-1234-1234567890ab",
        user_prompt="Continue",
        on_progress=None,
        on_message=None,
        cancel_event=asyncio.Event(),
    )

    assert " resume --json 12345678-1234-1234-1234-1234567890ab " in result.command
    assert result.assistant_text == "Done"


@pytest.mark.asyncio
async def test_run_uses_completed_agent_message_as_final_text(tmp_path: Path) -> None:
    script = tmp_path / "fake_codex.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"87654321-4321-4321-4321-ba0987654321\"}'\n"
        "printf '%s\\n' '{\"type\":\"item.completed\",\"item\":{\"id\":\"item_0\",\"type\":\"agent_message\",\"text\":\"Final reply\"}}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = CodexRunner(str(script), timeout_sec=5)
    message_updates: list[str] = []

    result = await runner.run(
        project_path=str(tmp_path),
        codex_session_id=None,
        user_prompt="What changed?",
        on_progress=None,
        on_message=_collector(message_updates),
        cancel_event=asyncio.Event(),
    )

    assert result.assistant_text == "Final reply"
    assert result.codex_session_id == "87654321-4321-4321-4321-ba0987654321"
    assert message_updates == ["Final reply"]


def _collector(items: list[str]):
    async def _inner(value: str) -> None:
        items.append(value)

    return _inner
