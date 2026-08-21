from pathlib import Path

import pytest

from app.config import ConfigurationError, Settings


def test_web_default_port_is_8686() -> None:
    assert Settings(_env_file=None).web_port == 8686


def test_transfer_tuning_defaults_are_conservative() -> None:
    settings = Settings(_env_file=None)

    assert settings.telegram_download_part_size_kb == 0
    assert settings.terabox_upload_concurrency == 1


@pytest.mark.parametrize("value", [1, 64, 384, 513])
def test_invalid_telegram_download_part_size_is_rejected(value: int) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, telegram_download_part_size_kb=value)


def test_transfer_tuning_environment_values_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_DOWNLOAD_PART_SIZE_KB", "512")
    monkeypatch.setenv("TERABOX_UPLOAD_CONCURRENCY", "3")

    settings = Settings(_env_file=None)

    assert settings.telegram_download_part_size_kb == 512
    assert settings.terabox_upload_concurrency == 3


def test_environment_configuration_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TARGET_CHATS", "-1001, -1002,-1001")
    monkeypatch.setenv("DOWNLOAD_PHOTOS", "false")
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "25")
    settings = Settings(_env_file=None)

    assert settings.configured_chat_ids == (-1001, -1002)
    assert settings.download_photos is False
    assert settings.max_file_size_bytes == 25 * 1024 * 1024


def test_yaml_and_environment_chat_configuration(tmp_path: Path) -> None:
    yaml_file = tmp_path / "chats.yml"
    yaml_file.write_text(
        "chats:\n  - id: -1002\n    enabled: true\n  - id: -1003\n    enabled: false\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, target_chats="-1001,-1002", config_file=yaml_file)

    assert settings.configured_chat_ids == (-1001, -1002)


def test_invalid_target_chat_has_clear_error() -> None:
    settings = Settings(_env_file=None, target_chats="not-an-id")

    with pytest.raises(ConfigurationError, match="invalid Telegram chat ID"):
        _ = settings.configured_chat_ids


def test_missing_credentials_do_not_expose_secrets() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ConfigurationError, match="TG_API_ID and TG_API_HASH"):
        settings.require_telegram_credentials()


def test_mtproto_proxy_link_is_parsed_without_losing_secret() -> None:
    settings = Settings(
        _env_file=None,
        tg_mtproto_proxy=(
            "https://t.me/proxy?server=proxy.example&port=443&"
            "secret=ee00000000000000000000000000000000"
        ),
    )

    assert settings.mtproto_proxy_config == (
        "proxy.example",
        443,
        "ee00000000000000000000000000000000",
    )


def test_invalid_mtproto_proxy_link_fails_without_exposing_secret() -> None:
    settings = Settings(
        _env_file=None,
        tg_mtproto_proxy="tg://proxy?server=proxy.example&port=443&secret=too-short",
    )

    with pytest.raises(ConfigurationError, match="secret") as error:
        _ = settings.mtproto_proxy_config
    assert "too-short" not in str(error.value)


def test_yaml_enabled_must_be_boolean(tmp_path: Path) -> None:
    yaml_file = tmp_path / "chats.yml"
    yaml_file.write_text("chats:\n  - id: -1001\n    enabled: 'false'\n", encoding="utf-8")
    settings = Settings(_env_file=None, config_file=yaml_file)

    with pytest.raises(ConfigurationError, match="must be true or false"):
        _ = settings.configured_chat_ids
