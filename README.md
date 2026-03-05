# Telecodex Bot

Асинхронный Telegram-бот на Python, который запускает Codex CLI в выбранном проекте, хранит сессии в SQLite и стримит вывод в одно редактируемое сообщение.

## 1. Архитектура

### Сущности
- `Project` (из env): whitelist `name -> absolute path`.
- `ChatState` (SQLite `chat_state`): текущий проект/сессия для `chat_id`.
- `Session` (SQLite `sessions`): UUID, проект, alias, путь до лога.
- `History` (SQLite `history`): последние сообщения `user/assistant` для подмешивания в prompt.
- `ActiveRun` (in-memory): активная задача в чате, `cancel_event`, `started_at`.

### Потоки
1. Пользователь выбирает проект `/project <name>`.
2. Выбирает/создает сессию `/session new` или `/session <id>`.
3. Отправляет задачу (`/run` или обычный текст).
4. Бот формирует prompt: метаданные сессии + последние N сообщений + текущий запрос.
5. `CodexRunner` запускает `CODEX_COMMAND` через `asyncio.create_subprocess_exec` в `cwd=project_path`.
6. `stdout/stderr` читаются построчно, `TelegramStreamEditor` обновляет одно сообщение раз в `STREAM_UPDATE_INTERVAL_SEC`.
7. Полный вывод пишется в history log файл и в БД (усеченно для контекста).
8. По завершении отправляется `✅ done`/`❌ failed`; длинный вывод прикладывается файлом.

### Где хранится что
- SQLite: `DB_PATH`.
- Runtime-логи приложения: `LOG_DIR/telecodex_bot.log` (RotatingFileHandler).
- Полные логи сессий Codex: `HISTORY_DIR/*.log`.

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
- `/last`

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
- `TELECODEX_PROJECTS_JSON` (только whitelist абсолютных путей)
- `CODEX_COMMAND`

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
pytest -q
```
