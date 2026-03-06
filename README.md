# Telecodex Bot

Telegram-бот для работы с Codex CLI из выбранного проекта. Бот умеет вести Codex-сессии, стримить ответ в одно обновляемое сообщение, принимать голосовые сообщения через Deepgram и перезапускаться по команде администратора.

## Что умеет
- выбор проекта и текущей Codex-сессии для каждого чата
- запуск задач обычным текстом
- продолжение диалога через native resume Codex
- потоковый вывод ответа в Telegram
- голосовой ввод через Deepgram
- restart с подтверждением после успешного подъема сервиса
- plaintext-лог запусков в `conversation<TelegramUserID>.log`

## Основные команды
- `/menu` или `/start`
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

Обычное текстовое сообщение тоже считается задачей для Codex.

## Что нужно в `.env`
- `TELEGRAM_BOT_TOKEN`
- `TELECODEX_PROJECTS_JSON`
- `TELECODEX_ADMIN_CHAT_IDS`
- `CODEX_COMMAND`

Для голосовых сообщений дополнительно:
- `DEEPGRAM_API_KEY`
- `DEEPGRAM_BASE_URL`
- `DEEPGRAM_MODEL`

Пример переменных есть в [.env.example](/home/codex/telecodex_bot/.env.example).

## Быстрый запуск

```bash
cd /home/codex/telecodex_bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

После заполнения `.env` локальный запуск:

```bash
cd /home/codex/telecodex_bot
./.venv/bin/python -m telecodex_bot.main
```

## Systemd

```bash
sudo cp systemd/telecodexbot.service /etc/systemd/system/telecodexbot.service
sudo systemctl daemon-reload
sudo systemctl enable telecodexbot
sudo systemctl start telecodexbot
```

## Логи
- runtime-лог: `LOG_DIR/telecodex_bot.log`
- conversation-логи: `HISTORY_DIR/conversation*.log`

## Тесты

```bash
cd /home/codex/telecodex_bot
./.venv/bin/pytest -q
```
