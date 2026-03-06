Functional specification

Project: a Python Telegram bot for driving Codex CLI through subprocesses on an Ubuntu server. The bot supports project selection, Codex session selection, streaming replies in Telegram, and systemd service operation.

Goals
1) Accept commands and regular messages from Telegram.
2) Store the selected project and current Codex session per chat without exposing raw UUID workflows in normal UX.
3) Run Codex CLI in the correct `cwd`, collect the reply stream, and present it in Telegram.
4) Keep the UX centered around a single status message, inline buttons, and compact state cards.
5) Run as a service with configuration from env/.env.

Accepted decisions
- Telegram framework: aiogram v3.
- Storage: SQLite.
- The project list lives only in SQLite and is managed through the Telegram UI.
- The selected project and selected session are stored by `chat_id`.
- The Codex launch command is defined by `CODEX_COMMAND`; by default the bot does not enforce extra Codex sandbox or approval restrictions.
- Runtime artifacts from `data/`, `history/`, and `logs/` must not be committed.

Constraints
- No external database at startup, SQLite only.
- Only one active Codex run is allowed per chat at a time.
- The user cannot execute arbitrary shell commands through the bot.

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
- The state card shows: project, project path, selected session, status (`idle`/`running`), and last activity.
- Inline buttons: `Project`, `Session`, `New session`, `Stop`, `Help`.
- The `Help` screen contains only the short usage steps and does not include a fallback command block.

B) Project management
- `/projects` shows a short header without a textual project list; the project name and path are shown directly on inline buttons.
- `/project <name>` selects a project for the current chat.
- The `Project` inline button opens the project list.
- The project menu includes a `New project` button.
- The project menu includes a `Delete project` button that opens a dedicated project deletion mode.
- In project deletion mode the bot shows project buttons prefixed with a red cross, a `Back` button at the bottom, and a separate `Yes` / `No` confirmation screen.
- When confirmed, the project is removed from the bot database together with the saved sessions for that project, and chats that had this project selected have both `project_name` and `codex_session_id` cleared.
- After `New project`, the bot asks for the project name and then lets the user choose the root folder via inline directory navigation starting from `/`.
- In path selection mode the bot shows child folders, lays out buttons in multiple columns based on folder name length, and allows going deeper or moving one level up.
- The current folder is confirmed with a single button in the format `✅ <current path>` with no extra `Select` word.
- After successful input, the project is saved in the database, appears in the shared project list, and is selected immediately for the current chat.
- When switching projects, the bot automatically selects the most recently used session for that project; if the project has no sessions yet, the next prompt starts a new session.

C) Session management
- `/sessions` shows the saved Codex sessions for the current project and also accepts `new` or `codex_session_id` as a text fallback to start a new session or choose an existing one.
- `/session_name <alias>` assigns an alias to the current session.
- `/whereami` shows the same state card as `/menu`.
- The `Session` inline button opens the recent Codex sessions for the current project.
- The `New session` inline button only clears the current binding; it does not create a local record upfront.
- The session picker includes a `Delete session` button that opens a dedicated session deletion mode.
- In session deletion mode the bot shows the current project sessions, each button prefixed with a red cross, and a `Back` button at the bottom.
- Clicking a session in deletion mode opens a `Yes` / `No` confirmation screen.
- `Yes` deletes the session from the bot database and returns the user to the deletion list.
- `No` does not delete anything and also returns to the deletion list.
- If the active session is deleted, the bot treats the chat as having a new session selected, meaning `codex_session_id` in `chat_state` is cleared.
- Everywhere the bot shows a session identifier to the user, it uses the format `projecttail|YY-MM-DD|HH:MM`, where `tail` is the final segment of `codex_session_id` including the leading `-`, with no `|` between the project name and `tail`, and the date/time comes from `updated_at`.
- After clearing the current session, the bot confirms with `The next message/request will start a new Codex session`.

D) Task execution
- Any non-command text is treated as a Codex task.
- A `voice` message is transcribed through Deepgram and, after successful transcription, executed as a normal text task.
- `/status` shows a compact status of the active task.
- `/cancel` stops the current subprocess.

E) Telegram output
- At request start the bot sends a single `Telecodex thinking` message.
- The status header is rendered in bold. Telegram Bot API does not support arbitrary text color, so colored emoji indicators are used instead.
- `Telecodex thinking` uses a blue indicator, and `Telecodex working` uses a green one.
- The status header uses the same braille spinner animation pattern as the voice transcription indicator and does not use a `...` suffix.
- When a voice message is received, the bot shows a short STT status, then deletes it and posts the clean transcript as a separate message.
- While the task is running, the bot periodically sends `typing`.
- As soon as real progress appears, the top line changes to `Telecodex working (Ns)`.
- The seconds counter runs continuously from the start of the request.
- No separate local “preparing project” status is shown.
- Intermediate statuses are built only from human-readable Codex commentary; shell commands, raw stdout/stderr, file dumps, and technical noise are not shown in chat.
- During streaming, the `Telecodex working (Ns)` header stays visible until the request finishes, and the current statuses or response preview appear immediately below it without a blank line.
- The final reply fully replaces the initial status message.
- If the reply is long or contains large code blocks, the message keeps a safe preview and the full reply is sent as a separate file.
- The final reply includes `New session` and `Switch project` buttons underneath.

F) Access and admin actions
- Only chat IDs listed in `TELECODEX_ADMIN_CHAT_IDS` may use the bot.
- Any request from a chat ID outside that whitelist receives `Access denied`.
- `/restart` is available only to admin chat IDs.
- If any Codex run is active, `/restart` refuses to stop the service.
- On `/restart` the bot answers with a short confirmation only, without any extra “coming back after restart” phrase.
- On successful `/restart`, the bot stores a persisted restart marker, stops polling, and after the next successful startup sends a message to the same chat that the service restarted successfully, then clears the marker.

Logs and history
- User-facing conversation history is stored only in a plaintext file named `conversation<TelegramUserID>.log`.
- Each run appends the following to the conversation log:
  - timestamp
  - original user message
  - full Codex shell command
  - full raw Codex output without formatting or filtering
- Local message history in the database and separate session history logs are not used.

Codex CLI integration
- The bot runs only `CODEX_COMMAND` with the prompt appended.
- Before launch it sets `cwd = project_path`.
- Main automation mode: `codex exec --json <prompt>`.
- To continue a conversation it uses only `codex exec resume --json <codex_session_id> <prompt>`.
- The primary source of streamed reply text is `item.message.delta`.
- Compatible fallback events are also supported, including `response_item`, `event_msg`, and `item.completed` with final text.
- `thread.started` and other events containing a session/thread id are used to extract `codex_session_id`.
- Invalid JSON lines and unexpected event types must not crash execution.
- The source of truth for the session identifier is only the session/thread id returned by Codex CLI itself.

Deepgram integration
- If `DEEPGRAM_API_KEY` is set, the bot accepts Telegram voice messages.
- The voice file is downloaded from Telegram as `audio/ogg` and sent to Deepgram for transcription.
- If Deepgram is temporarily unavailable, the bot returns a clear user-facing error.
- If `DEEPGRAM_API_KEY` is missing, voice messages are rejected with an explicit message that STT is not configured.
- If the Deepgram client cannot initialize at process startup, the bot must not crash as a whole: voice support is disabled and the service continues running with a warning in the log.

Technical requirements
- Architecture is asynchronous.
- Subprocesses are started via `asyncio.create_subprocess_exec`.
- Logging is implemented with `logging` and `RotatingFileHandler`.
- The conversation log is written as a plain text file preserving original line breaks.
- Configuration is loaded via `.env` and `pydantic-settings`.
- The application version is stored in `telecodex_bot/__init__.py`; git releases are marked with tags of the form `v<version>`.
- `README.md` should describe the current bot behavior and operations in a short practical form without unnecessary internal implementation detail.
- Tests must run locally with `cd /home/codex/telecodex_bot && ./.venv/bin/pytest -q`.
