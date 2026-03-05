from telecodex_bot.config import Settings


def test_projects_json_parse() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x",
        TELECODEX_PROJECTS_JSON='{"demo":"/tmp/demo"}',
    )
    assert "demo" in settings.projects
    assert str(settings.projects["demo"]) == "/tmp/demo"


def test_admin_chat_ids_parse() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x",
        TELECODEX_PROJECTS_JSON='{"demo":"/tmp/demo"}',
        TELECODEX_ADMIN_CHAT_IDS="1001, 1002 ,1001",
    )

    assert settings.admin_chat_ids == {1001, 1002}
