from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher

from telecodex_bot.bot import TelecodexApplication
from telecodex_bot.config import get_settings
from telecodex_bot.db import init_db
from telecodex_bot.logging_config import configure_logging
from telecodex_bot.repository import Repository
from telecodex_bot.runner import CodexRunner


async def run() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_dir)
    await init_db(str(settings.db_path))

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()

    repo = Repository(settings.db_path)
    runner = CodexRunner(settings.codex_command, settings.run_timeout_sec)
    app = TelecodexApplication(bot, dispatcher, repo, runner, settings)
    await app.configure_bot_commands()

    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
