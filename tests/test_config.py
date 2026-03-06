from telecodex_bot.config import Settings


def test_admin_chat_ids_parse() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x",
        TELECODEX_ADMIN_CHAT_IDS="1001, 1002 ,1001",
    )

    assert settings.admin_chat_ids == {1001, 1002}


def test_deepgram_settings_parse() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x",
        DEEPGRAM_API_KEY="dg-key",
        DEEPGRAM_MODEL="nova-3",
        DEEPGRAM_TIMEOUT_SEC=15,
        DEEPGRAM_RETRIES=4,
    )

    assert settings.deepgram_api_key == "dg-key"
    assert settings.deepgram_model == "nova-3"
    assert settings.deepgram_timeout_sec == 15
    assert settings.deepgram_retries == 4
