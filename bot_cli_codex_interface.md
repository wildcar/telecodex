# Bot CLI Codex Interface

## Purpose

This note describes the runtime path between Telegram, the bot, and Codex CLI.
It is intentionally short and focused on the actual implementation in this repo.

## Request Flow

1. A Telegram text message reaches `TelecodexApplication._execute_prompt()` in [telecodex/bot.py](/home/codex/telecodex_bot/telecodex/bot.py).
2. The bot checks that:
   - no other run is active in the same chat;
   - a project is selected for the chat;
   - the currently selected Codex session is loaded if one exists.
3. The bot creates `TelegramStreamEditor` from [telecodex/streaming.py](/home/codex/telecodex_bot/telecodex/streaming.py) and sends the initial live status message.
4. The bot starts `CodexRunner.run()` from [telecodex/runner.py](/home/codex/telecodex_bot/telecodex/runner.py).

## How Codex CLI Is Started

- The base command comes from `CODEX_COMMAND`.
- The subprocess is started with `cwd = project_path`.
- For a new conversation the command shape is:

```bash
codex exec --json -- "<prompt>"
```

- For continuation of an existing session the command shape is:

```bash
codex exec resume --json <codex_session_id> -- "<prompt>"
```

- Prompt text is always appended after `--`, so a prompt that starts with `-` cannot be parsed as a CLI flag.

## How CLI Output Is Parsed

`CodexRunner` reads stdout line by line and feeds each line into `CodexJsonEventParser`.

The parser extracts three kinds of useful information:

- `progress`: commentary or reasoning text for the temporary Telegram status block;
- `assistant_delta` / `assistant_snapshot`: assistant reply text that is being built incrementally;
- `session`: `session_id` returned by Codex, which becomes the canonical `codex_session_id`.

Supported event sources include:

- `item.message.delta`
- `message.delta`
- `response.output_text.delta`
- `event_msg`
- `response_item`
- `item.completed`

Invalid JSON lines and unknown event types are ignored without crashing the run.

## How Output Reaches Telegram

`TelegramStreamEditor` is the bridge from parsed Codex output to the chat.

During the run:

- `publish_status()` stores short human-readable progress lines;
- `publish_answer()` stores the latest known assistant text;
- a refresh loop periodically edits one live Telegram message;
- the header shows `Telecodex thinking` or `Telecodex working` with a spinner and elapsed seconds.

At the end:

- `stream.finish()` replaces the live status message with the final reply;
- if the final text is too long, the chat gets a preview and the full text is sent as `codex_output.md`;
- if Telegram message edits stall or fail, the stream layer falls back to sending a fresh status or final message so the chat does not stay stuck on an old timer.

## Result Handling

After the subprocess ends, the bot:

- appends the full command and raw Codex output to `history/conversation<TelegramUserID>.log`;
- saves or updates the returned `codex_session_id`;
- chooses the final user-facing text from `assistant_text`, `display_text`, or raw output-derived fallbacks;
- sends result buttons such as `New session` and `Switch project`.

## Voice Input

Voice messages follow the same CLI path after transcription:

1. Telegram voice is downloaded.
2. Deepgram transcribes it.
3. The transcript is sent through the same `_execute_prompt()` path as a regular text message.
