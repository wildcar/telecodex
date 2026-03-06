from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable


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


class CodexRunner:
    def __init__(self, codex_command: str, timeout_sec: int) -> None:
        self.command = shlex.split(codex_command)
        self.timeout_sec = timeout_sec

    def _build_command(self, prompt: str, codex_session_id: str | None) -> list[str]:
        if codex_session_id:
            return [*self.command, "resume", codex_session_id, prompt]
        return [*self.command, prompt]

    async def run(
        self,
        *,
        project_path: str,
        codex_session_id: str | None,
        user_prompt: str,
        on_progress: Callable[[str], Awaitable[None]] | None,
        cancel_event: asyncio.Event,
    ) -> RunResult:
        command = self._build_command(user_prompt, codex_session_id)
        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(Path(project_path)),
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
                if on_progress is not None:
                    progress_text = self._extract_progress_text(text)
                    if progress_text:
                        await on_progress(progress_text)

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
        raw_output = "".join(raw_collected)
        output = "".join(stream_collected)
        assistant_text = self._extract_assistant_text(stream_collected, user_prompt=user_prompt)
        if not assistant_text.strip():
            assistant_text = self._extract_assistant_text(raw_collected, user_prompt=user_prompt)
        display_text = assistant_text
        extracted_session_id = self._extract_codex_session_id(raw_collected)
        success = return_code == 0 and not timed_out and not cancelled
        return RunResult(
            success=success,
            return_code=return_code,
            command=shlex.join(command),
            raw_output=raw_output,
            output=output,
            assistant_text=assistant_text,
            display_text=display_text,
            codex_session_id=extracted_session_id or codex_session_id,
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
        stripped = CodexRunner._normalize_line(line).strip()
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

    @staticmethod
    def _normalize_line(line: str) -> str:
        normalized = line.strip()
        while True:
            if normalized.startswith("[stderr]"):
                normalized = normalized[len("[stderr]") :].strip()
                continue
            if normalized.startswith("[stdout]"):
                normalized = normalized[len("[stdout]") :].strip()
                continue
            if normalized.startswith("[stderr] "):
                normalized = normalized[len("[stderr] ") :].strip()
                continue
            if normalized.startswith("[stdout] "):
                normalized = normalized[len("[stdout] ") :].strip()
                continue
            break
        return normalized

    @classmethod
    def _extract_codex_session_id(cls, lines: list[str]) -> str | None:
        for raw in lines:
            normalized = cls._normalize_line(raw)
            match = re.match(r"session id:\s*([0-9a-fA-F-]{36})\b", normalized, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    @classmethod
    def _extract_assistant_text(cls, lines: list[str], user_prompt: str = "") -> str:
        clean_lines: list[str] = []
        previous = ""
        prompt_variants = cls._prompt_variants(user_prompt)
        for raw in lines:
            line = cls._normalize_line(raw.rstrip("\n"))
            if cls._is_noise_line(line):
                continue
            normalized = line.strip()
            if not normalized:
                if previous:
                    clean_lines.append("")
                    previous = ""
                continue
            if normalized in prompt_variants:
                continue
            if normalized == previous:
                continue
            clean_lines.append(normalized)
            previous = normalized
        return cls._deduplicate_blocks("\n".join(clean_lines).strip())

    @classmethod
    def _extract_progress_text(cls, line: str) -> str | None:
        normalized = cls._normalize_line(line)
        if cls._is_noise_line(normalized):
            return None
        stripped = normalized.strip()
        if not stripped:
            return None
        if stripped.startswith("/") or stripped.startswith("$"):
            return None
        if re.search(r"\b(exec|sed|cat|rg|pytest|git|python|bash)\b", stripped):
            return None
        if re.fullmatch(r"[\w./-]+\.[A-Za-z0-9]+", stripped):
            return None
        if len(stripped) > 140:
            return None
        return stripped

    @staticmethod
    def _prompt_variants(user_prompt: str) -> set[str]:
        variants = {user_prompt.strip()}
        variants.update(line.strip() for line in user_prompt.splitlines())
        return {item for item in variants if item}

    @staticmethod
    def _deduplicate_blocks(text: str) -> str:
        if not text:
            return text
        lines = text.splitlines()
        deduped: list[str] = []
        previous = None
        for line in lines:
            if line == previous:
                continue
            deduped.append(line)
            previous = line
        return "\n".join(deduped).strip()
