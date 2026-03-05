from telecodex_bot.config import Settings


def test_projects_json_parse() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x",
        TELECODEX_PROJECTS_JSON='{"demo":"/tmp/demo"}',
    )
    assert "demo" in settings.projects
    assert str(settings.projects["demo"]) == "/tmp/demo"
