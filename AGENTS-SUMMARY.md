# AGENTS-SUMMARY

Current clean-context handoff for `telecodex_bot`.

## Repository

- Path: `/home/keeper/repo/telecodex_bot`
- Production path: `/opt/telecodex`, symlinked to this checkout
- Service: `telecodex`
- Main entrypoint: `python -m telecodex.main`
- Tests: `./.venv/bin/pytest -q`

## Product

Telecodex is a Telegram bot for running Codex CLI tasks in selected project directories. An authorized user can choose a project, start or resume a Codex session, send text/file/voice tasks, and receive streamed Codex replies in Telegram.

## Current Behavior

- Project list, selected project, and selected session are stored in SQLite.
- Codex CLI is launched through `CODEX_COMMAND`; the default model is `gpt-5.5`.
- New tasks use `codex exec --json -- <prompt>`.
- Resumed tasks use `codex exec resume --json <codex_session_id> -- <prompt>`.
- The runner injects `--dangerously-bypass-approvals-and-sandbox`, `--cd <project_path>`, and `--skip-git-repo-check` for Codex CLI commands.
- If `codex` is missing from the systemd `PATH`, the runner searches common per-user install paths such as `~/.nvm/.../bin/codex`.
- When a project or session is selected, the bot performs a fresh Codex diagnostic request in that project/session context and shows the result in Telegram.
- Diagnostic context and rate-limit lines are shown only when Codex CLI reports enough metadata to calculate them.
- Runtime artifacts in `data/`, `history/`, and `logs/` are local-only and should not be committed.

## Documentation

- `AGENTS.md` is the primary agent entrypoint.
- `CLAUDE.md` is a compatibility entrypoint that redirects agents to the same workflow.
- `AGENTS-TECHSPEC.md` is the functional specification; `FS.md` is a compatibility pointer only.
- `AGENTS-SPECIFICS-1.md` documents the Telegram -> bot -> Codex CLI execution path.
- `AGENTS-HISTORY.md` records concise agent work history.
- `AGENTS-TODO.md` records deferred work only.

## Recent Important Commits

- `ec75898` migrated the default Codex model to `gpt-5.5`.
- `b84580a` added fresh Codex context checks when selecting projects or sessions.
- `d05fa47` hid unavailable diagnostic metadata instead of showing `unknown`.
- `64a02aa` exposed raw Codex diagnostic error detail when diagnostics fail.
