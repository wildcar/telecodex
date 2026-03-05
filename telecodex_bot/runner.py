from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from telecodex_bot.repository import HistoryItem, SessionRecord


@dataclass(slots=True)
class RunResult:
    success: bool
    return_code: int
    output: str
    assistant_text: str
    timed_out: bool = False
    cancelled: bool = False


class CodexRunner:
    def __init__(self, codex_command: str, timeout_sec: int) -> None:
        self.command = shlex.split(codex_command)
        self.timeout_sec = timeout_sec

    @staticmethod
    def _build_prompt(
        session: SessionRecord,
        user_prompt: str,
        recent_history: Iterable[HistoryItem],
    ) -> str:
        history_lines = []
        for item in recent_history:
            history_lines.append(f"{item.role}: {item.content}")
        history_blob = "\n".join(history_lines) if history_lines else "(empty)"
        return (
            "Session context:\n"
            f"session_id={session.id}\n"
            f"project={session.project_name}\n"
            f"history_log={session.history_log_path}\n"
            "Recent history:\n"
            f"{history_blob}\n\n"
            "User task:\n"
            f"{user_prompt}"
        )

    async def run(
        self,
        session: SessionRecord,
        user_prompt: str,
        recent_history: Iterable[HistoryItem],
        on_output: Callable[[str], Awaitable[None]],
        cancel_event: asyncio.Event,
    ) -> RunResult:
        prompt = self._build_prompt(session, user_prompt, recent_history)
        command = [*self.command, prompt]
        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(Path(session.project_path)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        stream_collected: list[str] = []
        raw_collected: list[str] = []

        async def read_stream(stream: asyncio.StreamReader | None, source: str, prefix: str) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace")
                raw_collected.append(f"[{source}] {line}")
                if self._is_noise_line(line):
                    continue
                text = f"{prefix}{line}"
                stream_collected.append(text)
                await on_output(text)

        stdout_task = asyncio.create_task(read_stream(proc.stdout, "stdout", ""))
        stderr_task = asyncio.create_task(read_stream(proc.stderr, "stderr", "[stderr] "))

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
        output = "".join(stream_collected)
        assistant_text = self._extract_assistant_text(stream_collected)
        if not assistant_text.strip():
            assistant_text = self._extract_assistant_text(raw_collected)
        success = return_code == 0 and not timed_out and not cancelled
        return RunResult(
            success=success,
            return_code=return_code,
            output=output,
            assistant_text=assistant_text,
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
    def _is_noise_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        lower = stripped.lower()
        starts_with_noise = (
            "openai codex v",
            "--------",
            "workdir:",
            "model:",
            "provider:",
            "approval:",
            "sandbox:",
            "reasoning effort:",
            "reasoning summaries:",
            "session id:",
            "session context:",
            "session_id=",
            "project=",
            "history_log=",
            "recent history:",
            "user task:",
            "mcp startup:",
            "tokens used",
            "usage:",
            "for more information",
            "user",
            "assistant:",
            "codex",
        )
        if lower.startswith(starts_with_noise):
            return True
        if lower.startswith("user:"):
            return True
        if re.fullmatch(r"[0-9,]+", stripped):
            return True
        return False

    @classmethod
    def _extract_assistant_text(cls, lines: list[str]) -> str:
        clean_lines: list[str] = []
        previous = ""
        skip_next_user_task_line = False
        for raw in lines:
            line = raw.rstrip("\n")
            if line.startswith("[stderr] "):
                line = line[len("[stderr] ") :]
            if line.startswith("[stdout] "):
                line = line[len("[stdout] ") :]
            if skip_next_user_task_line and line.strip():
                skip_next_user_task_line = False
                continue
            if line.strip().lower().startswith("user task:"):
                skip_next_user_task_line = True
            if cls._is_noise_line(line):
                continue
            normalized = line.strip()
            if not normalized:
                if previous:
                    clean_lines.append("")
                    previous = ""
                continue
            if normalized == previous:
                continue
            clean_lines.append(normalized)
            previous = normalized
        return "\n".join(clean_lines).strip()
