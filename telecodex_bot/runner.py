from __future__ import annotations

import asyncio
import os
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

        collected: list[str] = []

        async def read_stream(stream: asyncio.StreamReader | None, prefix: str) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                text = f"{prefix}{chunk.decode('utf-8', errors='replace')}"
                collected.append(text)
                await on_output(text)

        stdout_task = asyncio.create_task(read_stream(proc.stdout, ""))
        stderr_task = asyncio.create_task(read_stream(proc.stderr, "[stderr] "))

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
        output = "".join(collected)
        success = return_code == 0 and not timed_out and not cancelled
        return RunResult(success=success, return_code=return_code, output=output, timed_out=timed_out, cancelled=cancelled)

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
