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
    command: str
    raw_output: str
    output: str
    assistant_text: str
    display_text: str
    timed_out: bool = False
    cancelled: bool = False


class CodexRunner:
    def __init__(self, codex_command: str, timeout_sec: int) -> None:
        self.command = shlex.split(codex_command)
        self.timeout_sec = timeout_sec

    def _build_command(self, prompt: str) -> list[str]:
        return [*self.command, prompt]

    @staticmethod
    def _build_prompt(
        session: SessionRecord,
        user_prompt: str,
        recent_history: Iterable[HistoryItem],
    ) -> str:
        history_lines = []
        for item in recent_history:
            clean_content = CodexRunner._sanitize_history_for_prompt(item.content)
            history_lines.append(f"{item.role}: {clean_content}")
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
        on_progress: Callable[[str], Awaitable[None]] | None,
        cancel_event: asyncio.Event,
    ) -> RunResult:
        prompt = self._build_prompt(session, user_prompt, recent_history)
        command = self._build_command(prompt)
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
        success = return_code == 0 and not timed_out and not cancelled
        return RunResult(
            success=success,
            return_code=return_code,
            command=shlex.join(command),
            raw_output=raw_output,
            output=output,
            assistant_text=assistant_text,
            display_text=display_text,
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
    def _extract_assistant_text(cls, lines: list[str], user_prompt: str = "") -> str:
        clean_lines: list[str] = []
        previous = ""
        skip_next_user_task_line = False
        prompt_variants = cls._prompt_variants(user_prompt)
        for raw in lines:
            line = cls._normalize_line(raw.rstrip("\n"))
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
            if normalized in prompt_variants:
                continue
            if normalized == previous:
                continue
            clean_lines.append(normalized)
            previous = normalized
        return cls._deduplicate_blocks("\n".join(clean_lines).strip())

    @classmethod
    def _sanitize_history_for_prompt(cls, content: str) -> str:
        parts = content.splitlines()
        cleaned = cls._extract_assistant_text(parts)
        if cleaned:
            return cls._collapse_inline(cleaned)
        fallback = []
        for part in parts:
            normalized = cls._normalize_line(part)
            if not normalized or cls._is_noise_line(normalized):
                continue
            fallback.append(normalized)
        fallback_text = "\n".join(fallback).strip()
        if fallback_text:
            return cls._collapse_inline(fallback_text)
        return "(filtered noisy history)"

    @staticmethod
    def _collapse_inline(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _prompt_variants(user_prompt: str) -> set[str]:
        variants = {user_prompt.strip()}
        collapsed = re.sub(r"\s+", " ", user_prompt).strip()
        if collapsed:
            variants.add(collapsed)
        return {item for item in variants if item}

    @staticmethod
    def _deduplicate_blocks(text: str) -> str:
        if not text:
            return text
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        result: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            result.append(line)
        return "\n".join(result)

    @classmethod
    def _extract_progress_text(cls, raw_line: str) -> str | None:
        line = cls._normalize_line(raw_line)
        normalized = re.sub(r"\s+", " ", line).strip()
        if not normalized or cls._is_noise_line(normalized):
            return None
        lower = normalized.lower()
        if lower.startswith(("exec", "succeeded in", "failed in", "error:", "usage:", "tip:")):
            return None
        if normalized.startswith(("/", "$", "`")):
            return None
        if " in /" in normalized or normalized.endswith(":") and "/" in normalized:
            return None
        if "::" in normalized or "\t" in normalized:
            return None
        if re.fullmatch(r"[\w./-]+\.[\w./-]+", normalized):
            return None
        if re.search(r"\b(?:sed|rg|pytest|git|python|bash|cat|ls|find|apply_patch)\b", lower):
            return None
        if len(normalized) > 220:
            return None
        if normalized.count("/") >= 2:
            return None
        if not re.search(r"[A-Za-zА-Яа-я]", normalized):
            return None
        return normalized
