Functional specification

Project: a Python Telegram bot that runs Codex CLI tasks on an Ubuntu server. The bot lets an authorized user choose a project, continue or start a Codex session, send tasks from Telegram, and receive streamed replies in chat.

Goals
1) Accept Telegram commands and free-form task messages.
2) Store the selected project and current Codex session per chat.
3) Run Codex CLI in the selected project directory and return the result to Telegram.
4) Keep the interaction centered around a compact state card, inline buttons, and one live status message.
5) Run reliably as a systemd service with configuration from env/.env.

Current stack and storage
- Telegram framework: aiogram v3.
- Storage: SQLite.
- The project list is stored in SQLite and managed from Telegram.
- The selected project and selected session are stored by `chat_id`.
- The Codex launch command is configured through `CODEX_COMMAND`.
- The default Codex model for new and resumed tasks is `gpt-5.5`.
- Runtime artifacts from `data/`, `history/`, and `logs/` are local-only and must not be committed.

Constraints
- SQLite is the only required database.
- Only one active Codex run is allowed per chat.
- The bot does not provide arbitrary shell access to the user.

Data model
- Chat state:
  - `chat_id`
  - `project_name`
  - `codex_session_id | null`
- Codex session:
  - `codex_session_id`
  - `project_name`
  - `project_path`
  - `created_at`
  - `updated_at`
  - `alias | null`

Telegram UX

A) Main menu
- `/start` and `/menu` show the current chat state card.
- At startup the bot publishes the Telegram command menu through `setMyCommands`.
- The state card shows the selected project, project path, selected session, run status, and last activity.
- Inline buttons: `Project`, `Session`, `New session`, `Stop`, `Help`.
- `Help` shows short usage instructions.

B) Project management
- `/projects` opens the project picker.
- `/project <name>` selects a project for the current chat.
- The `Project` button opens the project list.
- The project menu includes `New project` and `Delete project` actions.
- New project creation asks for a name, then for a root folder through inline directory navigation starting from `/`.
- The current folder is selected by pressing `✅ <current path>`.
- A created project is saved in the database, appears in the shared project list, and becomes selected for the current chat.
- Deleting a project removes it from the database together with its saved sessions; chats that used it have both `project_name` and `codex_session_id` cleared.
- When a project is selected, the bot restores the most recently used session for that project if one exists.
- After a project is selected, the bot performs a fresh Codex diagnostic request in the selected project and session context, verifies that Codex can answer from that location/session, and shows a project/session info card with the project path, selected session, reported model details when available, context remaining percentage only when it can be calculated from Codex CLI metadata, five-hour and weekly Codex limit remaining only when reported by Codex CLI, and the diagnostic model reply.

C) Session management
- `/sessions` shows saved sessions for the current project and also accepts `new` or `codex_session_id` as text input.
- `/session_id` returns the raw `codex_session_id` of the currently selected session in the chat.
- `/session_name <alias>` sets an alias for the current session.
- `/whereami` shows the same state card as `/menu`.
- The `Session` button opens recent sessions for the current project.
- After a session is selected, the bot performs the same fresh Codex diagnostic request through `codex exec resume` for that session and shows the resulting project/session info card.
- The `New session` button clears the current session binding; the next task starts a new Codex session.
- The session picker includes a `Delete session` action.
- Deleting the active session clears `codex_session_id` in `chat_state`.
- Session labels shown to the user use the format `projecttail|YY-MM-DD|HH:MM`, where `tail` is the trailing segment of `codex_session_id` including the leading `-`, and the date/time comes from `updated_at`.
- If no session is currently selected, `/session_id` returns a clear user-facing message instead of an empty value.

D) Task execution
- Any non-command text message is treated as a Codex task.
- Telegram text documents are also treated as Codex tasks.
- When a text document has a caption, the caption is prepended as the user explanation and the decoded file body is appended after it in the same Codex prompt.
- The bot accepts only decodable plaintext documents for this flow and returns a clear user-facing error for binary or oversized files.
- Voice messages are transcribed through Deepgram and then executed as normal text tasks.
- `/status` shows the current task status.
- `/cancel` stops the active subprocess.

E) Telegram output
- At task start the bot sends one live status message with the `Telecodex thinking` header.
- The status header is bold and uses emoji indicators instead of text color.
- `Telecodex thinking` uses a blue indicator, and `Telecodex working` uses a green indicator.
- The status header uses the same braille spinner pattern as voice transcription progress.
- During execution the bot periodically sends `typing`.
- Once progress appears, the header changes to `Telecodex working (Ns)` and the counter runs from task start.
- Streaming status text is built only from human-readable Codex commentary.
- The live status message is replaced by the final reply.
- If Telegram message edits stall, time out, or stop updating for too long, the bot must recover by sending a fresh status/final message instead of silently hanging on a stale timer message.
- If the reply is too large for a safe Telegram message, the bot sends a preview in chat and the full reply as a file.
- The final reply includes `New session` and `Switch project` buttons.
- For voice input, the bot shows a short transcription status, deletes it after success, and sends the clean transcript as a separate message.

F) Access and admin actions
- Only chat IDs from `TELECODEX_ADMIN_CHAT_IDS` may use the bot.
- Requests from other chat IDs receive `Access denied`.
- `/restart` is available only to admin chat IDs.
- `/restart` is blocked while any Codex run is active.
- On `/restart` the bot stores a restart marker, stops polling, and after the next successful startup sends a confirmation to the same chat and clears the marker.

Logs and history
- User-facing history is stored only under `history/<TelegramUserID>/`.
- Each project has its own plaintext log file named `<project-name>.log` inside that user directory.
- The log filename is derived from the project name with filesystem-unsafe characters normalized so it is always a safe local filename.
- Each run appends:
  - timestamp
  - original user message
  - full Codex shell command
  - full raw Codex output
- The conversation log preserves original line breaks.

Codex CLI integration
- The bot runs `CODEX_COMMAND` with the prompt appended.
- The default `CODEX_COMMAND` includes `--model gpt-5.5` so Codex CLI uses GPT-5.5 unless an operator explicitly overrides the command in `.env`.
- The Codex subprocess is started with `cwd = project_path`.
- If `CODEX_COMMAND` uses the bare `codex` executable name and it is missing from the service `PATH`, the runner must resolve it from common per-user install locations such as `~/.nvm/.../bin/codex` before failing the run.
- If the resolved Codex executable is a script that depends on sibling runtime binaries such as `node` via `/usr/bin/env`, the runner must augment the subprocess `PATH` with the Codex executable directory so those binaries are discoverable even under a minimal systemd environment.
- When `CODEX_COMMAND` points to the `codex` CLI binary, the runner passes `--dangerously-bypass-approvals-and-sandbox` and `--cd <project_path>` as top-level Codex options and `--skip-git-repo-check` as an `exec` option so Codex runs with the requested sandbox mode, stays anchored to the selected path, and does not fail the trust check in non-repository directories.
- New tasks use `codex exec --json -- <prompt>`.
- Continuing a conversation uses `codex exec resume --json <codex_session_id> -- <prompt>`.
- The runner must pass prompt text after `--` so prompts that begin with `-` are never parsed as CLI flags.
- The primary source of streamed reply text is `item.message.delta`.
- Compatible fallback events are also supported, including `response_item`, `event_msg`, and `item.completed` with final text.
- Session/thread identifiers returned by Codex CLI are used as the source of truth for `codex_session_id`.
- `token_count` and `turn.completed` events from Codex CLI are parsed as best-effort run metadata, including context window, context remaining, five-hour and weekly limit usage/reset data when present.
- Invalid JSON lines and unexpected event types must not crash execution.
- Subprocess startup failures and unexpected runner exceptions must be converted into a visible final Telegram reply so the live status message never remains stuck on `Telecodex thinking`.

Deepgram integration
- If `DEEPGRAM_API_KEY` is configured, the bot accepts Telegram voice messages.
- The voice file is downloaded from Telegram as `audio/ogg` and sent to Deepgram for transcription.
- If Deepgram is temporarily unavailable, the bot returns a clear user-facing error.
- If `DEEPGRAM_API_KEY` is missing or the Deepgram client cannot initialize, voice support stays disabled and the bot continues running.

Technical requirements
- Architecture is asynchronous.
- Subprocesses are created via `asyncio.create_subprocess_exec`.
- Logging uses `logging` and `RotatingFileHandler`.
- Configuration is loaded via `.env` and `pydantic-settings`.
- The application version is stored in `telecodex/__init__.py`; the current target release is `v0.5`, and release tags use the form `v<version>`.
- The Python package directory is `telecodex/`, and the main entrypoint is `python -m telecodex.main`.
- The systemd service name is `telecodex`.
- The production deployment path is `/opt/telecodex`.
- `/opt/telecodex` is a symlink to the current working checkout used by the running systemd service and must point to a valid repository tree.
- The working repository path for local development and maintenance is `/home/keeper/repo/telecodex_bot`.
- Documentation, source-code comments, and user-facing UI text are maintained in English.
- The repository includes a short technical note `bot_cli_codex_interface.md` that explains how the bot talks to Codex CLI and how CLI output is streamed or finalized back into Telegram chat.
- Regular commit flow: `git add <files> && git commit -m "<message>" && git push origin main`.
- Commits in this repository must use the git author identity `wildcar <wildcar@users.noreply.github.com>`.
- `README.md` should describe the current bot behavior and operations in a short practical form.
- Tests run locally with `cd /home/keeper/repo/telecodex_bot && ./.venv/bin/pytest -q`.
