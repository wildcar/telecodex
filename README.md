# Telecodex Bot

A Telegram bot for working with Codex CLI inside a selected project. The bot manages Codex sessions, streams replies into a single updated Telegram message, accepts voice messages through Deepgram, and can restart itself on admin request.

## Features
- per-chat project and Codex session selection
- project creation and deletion directly from Telegram
- task execution from regular text messages
- conversation continuation through native Codex resume
- streaming replies in Telegram
- voice input through Deepgram
- restart with confirmation after the service comes back up
- plaintext run logs in `conversation<TelegramUserID>.log`

## Main commands
- `/menu` or `/start`
- `/projects`
- `/project <name>`
- `/sessions`
- `/sessions <id>`
- `/sessions new`
- `/session_name <alias>`
- `/whereami`
- `/status`
- `/cancel`
- `/restart`

A regular text message is also treated as a Codex task.

## Required `.env` values
- `TELEGRAM_BOT_TOKEN`
- `TELECODEX_ADMIN_CHAT_IDS`
- `CODEX_COMMAND`

Additional values for voice messages:
- `DEEPGRAM_API_KEY`
- `DEEPGRAM_BASE_URL`
- `DEEPGRAM_MODEL`

See [.env.example](/home/codex/telecodex_bot/.env.example) for an example.

## Quick start

```bash
cd /home/codex/telecodex_bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

After filling `.env`, run locally:

```bash
cd /home/codex/telecodex_bot
./.venv/bin/python -m telecodex.main
```

## Systemd

```bash
sudo cp systemd/telecodexbot.service /etc/systemd/system/telecodexbot.service
sudo systemctl daemon-reload
sudo systemctl enable telecodexbot
sudo systemctl start telecodexbot
```

## Logs
- runtime log: `LOG_DIR/telecodex_bot.log`
- conversation logs: `HISTORY_DIR/conversation*.log`

## Tests

```bash
cd /home/codex/telecodex_bot
./.venv/bin/pytest -q
```
