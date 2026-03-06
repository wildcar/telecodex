# Telecodex Bot

Асинхронный Telegram-бот на Python, который запускает Codex CLI в выбранном проекте, хранит в SQLite только состояние чата и метаданные Codex-сессий, показывает в Telegram одно редактируемое сообщение со статусом выполнения и финальным ответом и поддерживает голосовой ввод через Deepgram.

## 1. Архитектура

### Сущности
- `Project` (из env): `name -> absolute path`.
- `ChatState` (SQLite `chat_state`): текущий проект и выбранный `codex_session_id` для `chat_id`.
- `Session` (SQLite `sessions`): metadata сохраненных Codex-сессий, ключом служит сам `codex_session_id`.
- `ActiveRun` (in-memory): активная задача в чате, `cancel_event`, `started_at`.

### Потоки
1. Пользователь выбирает проект `/project <name>`.
2. Выбирает сохраненную сессию `/session <id>` или сбрасывает текущую через `/session new`.
3. Отправляет задачу (`/run` или обычный текст).
4. Голосовое сообщение сначала распознается через Deepgram и затем запускается как обычная текстовая задача.
5. `CodexRunner` запускает Codex в JSONL-режиме: для новой сессии `codex exec --json <prompt>`, для продолжения `codex exec resume --json <codex_session_id> <prompt>`.
6. `CodexRunner` запускает `CODEX_COMMAND` через `asyncio.create_subprocess_exec` в `cwd=project_path`.
7. `stdout` читается как поток JSONL-событий; `TelegramStreamEditor` получает статусы и потоковый текст ответа отдельно.
8. Основной стриминг строится по `item.message.delta`, а совместимые snapshot-события (`response_item`, `event_msg`, `item.completed`) используются как fallback.
9. Для каждого Telegram user id ведется plaintext-файл `HISTORY_DIR/conversation<TelegramUserID>.log` с timestamp, сообщением пользователя, полной командой запуска и сырым ответом Codex.
10. По завершении бот извлекает `session id` из JSON-событий или stderr fallback и сохраняет его как единственный идентификатор сессии.
11. То же сообщение заменяется финальным ответом; длинный ответ или технические детали при необходимости прикладываются отдельным файлом.

### Где хранится что
- SQLite: `DB_PATH`.
- Runtime-логи приложения: `LOG_DIR/telecodex_bot.log` (RotatingFileHandler).
- Пользовательские conversation-логи: `HISTORY_DIR/conversation*.log`.

## 2. Структура репозитория

```text
telecodex_bot/
  __init__.py
  bot.py
  config.py
  db.py
  logging_config.py
  main.py
  repository.py
  runner.py
  streaming.py
systemd/
  telecodexbot.service
tests/
  test_config.py
  test_repository.py
  test_streaming.py
.env.example
FS.md
README.md
requirements.txt
```

## 3. Команды бота
После старта бот публикует в Telegram menu набор основных команд с короткими описаниями.

- `/projects`
- `/project <name>`
- `/pwd`
- `/sessions`
- `/session <id|new>`
- `/session_name <alias>`
- `/whereami`
- `/run <text>`
- обычный текст = запуск задачи
- `/status`
- `/cancel`
- `/restart`

## 4. Установка (Ubuntu 22.04/24.04)

```bash
sudo useradd -r -m -d /opt/telecodexbot -s /usr/sbin/nologin telecodexbot
sudo mkdir -p /opt/telecodexbot
sudo chown -R telecodexbot:telecodexbot /opt/telecodexbot

cd /opt/telecodexbot
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env`:
- `TELEGRAM_BOT_TOKEN`
- `TELECODEX_PROJECTS_JSON`
- `CODEX_COMMAND` (по умолчанию без sandbox/approval-ограничений)
- `DEEPGRAM_API_KEY` для голосовых сообщений
- `DEEPGRAM_BASE_URL`, `DEEPGRAM_MODEL` при необходимости изменить STT-провайдера/модель
- `TELECODEX_ADMIN_CHAT_IDS` (comma-separated chat id для админ-команд, например `/restart`)

Инициализация service:

```bash
sudo cp systemd/telecodexbot.service /etc/systemd/system/telecodexbot.service
sudo systemctl daemon-reload
sudo systemctl enable telecodexbot
sudo systemctl start telecodexbot
sudo systemctl status telecodexbot
```

## 5. Логи и эксплуатация
- Runtime-лог: `journalctl -u telecodexbot -f`
- Логи задач: `HISTORY_DIR`
- Рекомендуется добавить logrotate для `LOG_DIR/*.log`.

## 6. Тесты

```bash
./.venv/bin/pytest -q
```
