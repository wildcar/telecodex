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

C) Session management
- `/sessions` shows saved sessions for the current project and also accepts `new` or `codex_session_id` as text input.
- `/session_name <alias>` sets an alias for the current session.
- `/whereami` shows the same state card as `/menu`.
- The `Session` button opens recent sessions for the current project.
- The `New session` button clears the current session binding; the next task starts a new Codex session.
- The session picker includes a `Delete session` action.
- Deleting the active session clears `codex_session_id` in `chat_state`.
- Session labels shown to the user use the format `projecttail|YY-MM-DD|HH:MM`, where `tail` is the trailing segment of `codex_session_id` including the leading `-`, and the date/time comes from `updated_at`.

D) Task execution
- Any non-command text message is treated as a Codex task.
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
- User-facing history is stored only in a plaintext file named `conversation<TelegramUserID>.log`.
- Each run appends:
  - timestamp
  - original user message
  - full Codex shell command
  - full raw Codex output
- The conversation log preserves original line breaks.

Codex CLI integration
- The bot runs `CODEX_COMMAND` with the prompt appended.
- The Codex subprocess is started with `cwd = project_path`.
- New tasks use `codex exec --json <prompt>`.
- Continuing a conversation uses `codex exec resume --json <codex_session_id> <prompt>`.
- The primary source of streamed reply text is `item.message.delta`.
- Compatible fallback events are also supported, including `response_item`, `event_msg`, and `item.completed` with final text.
- Session/thread identifiers returned by Codex CLI are used as the source of truth for `codex_session_id`.
- Invalid JSON lines and unexpected event types must not crash execution.

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
- Documentation, source-code comments, and user-facing UI text are maintained in English.
- Regular commit flow: `git add <files> && git commit -m "<message>" && git push origin main`.
- Commits in this repository must use the git author identity `wildcar <wildcar@users.noreply.github.com>`.
- `README.md` should describe the current bot behavior and operations in a short practical form.
- Tests run locally with `cd /home/codex/telecodex_bot && ./.venv/bin/pytest -q`.
