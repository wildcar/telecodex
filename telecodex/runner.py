from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

SESSION_ID_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{36})\b", flags=re.IGNORECASE)


@dataclass(slots=True)
class RunResult:
    success: bool
    return_code: int
    command: str
    raw_output: str
    output: str
    assistant_text: str
    display_text: str
    codex_session_id: str | None
    timed_out: bool = False
    cancelled: bool = False


@dataclass(slots=True)
class CodexStreamEvent:
    kind: str
    event_type: str
    text: str = ""
    session_id: str | None = None
    raw_line: str = ""
    payload: dict[str, Any] | None = None


class CodexJsonEventParser:
    def parse_line(self, line: str) -> list[CodexStreamEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning("Invalid Codex JSON line", extra={"line": stripped[:500]})
            return [CodexStreamEvent(kind="invalid_json", event_type="invalid_json", raw_line=line)]
        if not isinstance(payload, dict):
            logger.warning("Unexpected non-object Codex JSON event", extra={"line": stripped[:500]})
            return [CodexStreamEvent(kind="unexpected_event", event_type="unexpected_event", raw_line=line)]
        return self._parse_event(payload, line)

    def _parse_event(self, payload: dict[str, Any], raw_line: str) -> list[CodexStreamEvent]:
        event_type = str(payload.get("type", "unknown"))
        events: list[CodexStreamEvent] = []
        session_id = self._find_session_id(payload)
        if session_id:
            events.append(
                CodexStreamEvent(
                    kind="session",
                    event_type=event_type,
                    session_id=session_id,
                    raw_line=raw_line,
                    payload=payload,
                )
            )

        if event_type in {"item.message.delta", "message.delta", "response.output_text.delta"}:
            delta = self._coerce_text(payload.get("delta")) or self._coerce_text(payload.get("text"))
            if delta:
                events.append(
                    CodexStreamEvent(
                        kind="assistant_delta",
                        event_type=event_type,
                        text=delta,
                        raw_line=raw_line,
                        payload=payload,
                    )
                )
            return events or [CodexStreamEvent(kind="unexpected_event", event_type=event_type, raw_line=raw_line, payload=payload)]

        if event_type == "event_msg":
            return events + self._parse_event_msg(payload, raw_line)

        if event_type == "response_item":
            return events + self._parse_response_item(payload, raw_line)

        if event_type == "item.completed":
            return events + self._parse_item_completed(payload, raw_line)

        logger.debug("Ignoring unsupported Codex event type", extra={"event_type": event_type})
        return events or [CodexStreamEvent(kind="unexpected_event", event_type=event_type, raw_line=raw_line, payload=payload)]

    def _parse_event_msg(self, payload: dict[str, Any], raw_line: str) -> list[CodexStreamEvent]:
        body = payload.get("payload")
        if not isinstance(body, dict):
            return []
        body_type = str(body.get("type", "unknown"))
        if body_type == "agent_message":
            message = self._coerce_text(body.get("message"))
            if message:
                return [CodexStreamEvent(kind="assistant_snapshot", event_type=body_type, text=message, raw_line=raw_line, payload=body)]
        if body_type == "task_complete":
            message = self._coerce_text(body.get("last_agent_message"))
            if message:
                return [CodexStreamEvent(kind="assistant_snapshot", event_type=body_type, text=message, raw_line=raw_line, payload=body)]
        return []

    def _parse_response_item(self, payload: dict[str, Any], raw_line: str) -> list[CodexStreamEvent]:
        body = payload.get("payload")
        if not isinstance(body, dict):
            return []
        body_type = str(body.get("type", "unknown"))
        if body_type == "message":
            role = str(body.get("role", ""))
            text = self._extract_text_from_content(body.get("content"))
            phase = str(body.get("phase", ""))
            if role == "assistant" and text:
                if phase == "commentary":
                    return [CodexStreamEvent(kind="progress", event_type=phase or body_type, text=text, raw_line=raw_line, payload=body)]
                return [CodexStreamEvent(kind="assistant_snapshot", event_type=phase or body_type, text=text, raw_line=raw_line, payload=body)]
            return []
        if body_type == "reasoning":
            summary = self._extract_text_from_content(body.get("summary"))
            if summary:
                return [CodexStreamEvent(kind="progress", event_type=body_type, text=summary, raw_line=raw_line, payload=body)]
        return []

    def _parse_item_completed(self, payload: dict[str, Any], raw_line: str) -> list[CodexStreamEvent]:
        item = payload.get("item")
        if not isinstance(item, dict):
            return []
        item_type = str(item.get("type", "unknown"))
        if item_type == "agent_message":
            text = self._coerce_text(item.get("text")) or self._extract_text_from_content(item.get("content"))
            if text:
                return [
                    CodexStreamEvent(
                        kind="assistant_snapshot",
                        event_type=item_type,
                        text=text,
                        raw_line=raw_line,
                        payload=item,
                    )
                ]
        return []

    @classmethod
    def _extract_text_from_content(cls, content: Any) -> str:
        parts: list[str] = []
        cls._collect_text(content, parts)
        return "\n".join(part for part in parts if part).strip()

    @classmethod
    def _collect_text(cls, value: Any, parts: list[str]) -> None:
        if value is None:
            return
        if isinstance(value, str):
            text = value.strip()
            if text:
                parts.append(text)
            return
        if isinstance(value, list):
            for item in value:
                cls._collect_text(item, parts)
            return
        if not isinstance(value, dict):
            return
        item_type = value.get("type")
        if item_type in {"output_text", "input_text", "summary_text"}:
            text = cls._coerce_text(value.get("text"))
            if text:
                parts.append(text.strip())
            return
        for key in ("text", "message", "delta", "content", "summary"):
            if key in value:
                cls._collect_text(value.get(key), parts)

    @staticmethod
    def _coerce_text(value: Any) -> str:
        return value if isinstance(value, str) else ""

    @classmethod
    def _find_session_id(cls, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in {"session_id", "sessionId", "thread_id", "threadId"} and isinstance(value, str) and SESSION_ID_RE.search(f"session id: {value}"):
                    return value
                found = cls._find_session_id(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = cls._find_session_id(item)
                if found:
                    return found
        return None


class CodexResponseAggregator:
    def __init__(self) -> None:
        self.assistant_text = ""
        self.session_id: str | None = None

    def apply(self, event: CodexStreamEvent) -> str | None:
        if event.session_id:
            self.session_id = event.session_id
        if event.kind == "assistant_delta" and event.text:
            self.assistant_text += event.text
            return self.assistant_text
        if event.kind == "assistant_snapshot" and event.text:
            merged = self._merge_snapshot(self.assistant_text, event.text)
            if merged != self.assistant_text:
                self.assistant_text = merged
                return self.assistant_text
        return None

    @staticmethod
    def _merge_snapshot(current: str, snapshot: str) -> str:
        if not current:
            return snapshot
        if snapshot.startswith(current):
            return snapshot
        if current.startswith(snapshot):
            return current
        max_overlap = min(len(current), len(snapshot))
        for overlap in range(max_overlap, 0, -1):
            if current.endswith(snapshot[:overlap]):
                return current + snapshot[overlap:]
        return snapshot


class CodexRunner:
    def __init__(self, codex_command: str, timeout_sec: int) -> None:
        self.command = shlex.split(codex_command)
        self.timeout_sec = timeout_sec
        self.parser = CodexJsonEventParser()

    def _build_command(self, prompt: str, codex_session_id: str | None, project_path: str) -> list[str]:
        base = list(self.command)
        if base and Path(base[0]).name == "codex":
            base = [base[0], "--cd", project_path, *base[1:]]
            if len(base) >= 4 and base[3] == "exec":
                base = [*base[:4], "--skip-git-repo-check", *base[4:]]
        if codex_session_id:
            command = [*base, "resume"]
            if "--json" not in command:
                command.append("--json")
            return [*command, codex_session_id, "--", prompt]
        if "--json" not in base:
            base.append("--json")
        return [*base, "--", prompt]

    async def run(
        self,
        *,
        project_path: str,
        codex_session_id: str | None,
        user_prompt: str,
        on_progress: Callable[[str], Awaitable[None]] | None,
        on_message: Callable[[str], Awaitable[None]] | None,
        cancel_event: asyncio.Event,
    ) -> RunResult:
        command = self._build_command(user_prompt, codex_session_id, project_path)
        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(Path(project_path)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        raw_collected: list[str] = []
        aggregator = CodexResponseAggregator()
        invalid_json_lines = 0

        async def read_stdout(stream: asyncio.StreamReader | None) -> None:
            nonlocal invalid_json_lines
            if stream is None:
                return
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace")
                raw_collected.append(f"[stdout] {line}")
                for event in self.parser.parse_line(line):
                    if event.kind == "invalid_json":
                        invalid_json_lines += 1
                        continue
                    current_text = aggregator.apply(event)
                    if event.kind == "progress" and on_progress is not None:
                        progress_text = self._normalize_progress_text(event.text)
                        if progress_text:
                            await on_progress(progress_text)
                    if current_text is not None and on_message is not None:
                        await on_message(current_text)

        async def read_stderr(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace")
                raw_collected.append(f"[stderr] {line}")
                session_id = self._extract_codex_session_id_from_text(line)
                if session_id:
                    aggregator.session_id = session_id
                    continue
                logger.debug("Codex stderr", extra={"line": line.rstrip()[:500]})

        stdout_task = asyncio.create_task(read_stdout(proc.stdout))
        stderr_task = asyncio.create_task(read_stderr(proc.stderr))

        timed_out = False
        cancelled = False
        try:
            return_code = await asyncio.wait_for(self._wait_with_cancel(proc, cancel_event), timeout=self.timeout_sec)
        except asyncio.TimeoutError:
            timed_out = True
            await self._terminate(proc)
            return_code = -1
        finally:
            await stdout_task
            await stderr_task

        if cancel_event.is_set():
            cancelled = True
        if invalid_json_lines:
            logger.warning("Codex JSON stream contained invalid lines", extra={"count": invalid_json_lines})

        assistant_text = aggregator.assistant_text.strip()
        display_text = assistant_text
        success = return_code == 0 and not timed_out and not cancelled
        return RunResult(
            success=success,
            return_code=return_code,
            command=shlex.join(command),
            raw_output="".join(raw_collected),
            output=assistant_text,
            assistant_text=assistant_text,
            display_text=display_text,
            codex_session_id=aggregator.session_id or codex_session_id,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    async def _wait_with_cancel(self, proc: asyncio.subprocess.Process, cancel_event: asyncio.Event) -> int:
        while True:
            if cancel_event.is_set():
                await self._terminate(proc)
                return -2
            try:
                return await asyncio.wait_for(proc.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    @staticmethod
    def _normalize_progress_text(text: str) -> str:
        normalized = " ".join(text.split()).strip()
        if not normalized:
            return ""
        if len(normalized) > 200:
            normalized = normalized[:197].rstrip() + "..."
        return normalized

    @staticmethod
    def _extract_codex_session_id_from_text(line: str) -> str | None:
        match = SESSION_ID_RE.search(line)
        if match:
            return match.group(1)
        return None
