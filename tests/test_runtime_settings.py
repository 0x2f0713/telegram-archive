from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    RUNTIME_OVERRIDE_FIELDS,
    Settings,
    apply_runtime_overrides,
    decode_overrides,
    encode_overrides,
    runtime_form_values,
    settings_form_values,
)
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.settings import RuntimeSettingsRepository


async def test_runtime_settings_repository_round_trip(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'runtime.db'}")
    await database.initialize()
    repository = RuntimeSettingsRepository(database)

    assert await repository.overrides() == {}
    await repository.set_values({"download_photos": "false", "log_level": "DEBUG"})
    assert await repository.overrides() == {
        "download_photos": "false",
        "log_level": "DEBUG",
    }
    await repository.set_values({"download_photos": "true"})
    assert await repository.overrides() == {
        "download_photos": "true",
        "log_level": "DEBUG",
    }
    await repository.clear()
    assert await repository.overrides() == {}
    await database.close()


def test_decode_overrides_coerces_types_and_drops_unknown_keys() -> None:
    decoded = decode_overrides(
        {
            "download_photos": "yes",
            "download_videos": "false",
            "download_concurrency": "5",
            "keywords": "  alpha, beta ",
            "tg_api_id": "9999",
            "not_a_setting": "x",
        }
    )
    assert decoded == {
        "download_photos": True,
        "download_videos": False,
        "download_concurrency": 5,
        "keywords": "alpha, beta",
    }


def test_decode_overrides_rejects_bad_integers() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        decode_overrides({"download_concurrency": "many"})


def test_apply_runtime_overrides_validates_and_ignores_secrets() -> None:
    settings = Settings(_env_file=None)
    effective = apply_runtime_overrides(
        settings, {"log_level": "debug", "web_refresh_seconds": "30"}
    )
    assert effective.log_level == "DEBUG"
    assert effective.web_refresh_seconds == 30

    with pytest.raises(ValidationError):
        apply_runtime_overrides(settings, {"log_level": "loud"})
    with pytest.raises(ValidationError):
        apply_runtime_overrides(settings, {"download_concurrency": "99"})

    secret_probe = apply_runtime_overrides(settings, {"tg_api_id": "12345"})
    assert secret_probe.tg_api_id == settings.tg_api_id
    assert apply_runtime_overrides(settings, {}) is settings


def test_apply_runtime_overrides_round_trips_through_encode() -> None:
    settings = Settings(_env_file=None)
    applied = apply_runtime_overrides(settings, encode_overrides(settings))
    assert settings_form_values(applied) == settings_form_values(settings)
    assert set(encode_overrides(settings)) == set(RUNTIME_OVERRIDE_FIELDS)


def test_runtime_form_values_maps_checkboxes_and_required_fields() -> None:
    values = {
        "csrf_token": ["x"],
        "download_photos": ["true"],
        "max_file_size_mb": ["250"],
        "download_concurrency": [""],
        "keywords": ["  a, b  "],
        "log_level": ["DEBUG"],
    }
    overrides = runtime_form_values(values)
    assert overrides["download_photos"] == "true"
    assert overrides["download_videos"] == "false"
    assert overrides["max_file_size_mb"] == "250"
    assert overrides["keywords"] == "a, b"
    assert overrides["log_level"] == "DEBUG"
    assert "csrf_token" not in overrides
    with pytest.raises(ValueError, match="must be an integer"):
        decode_overrides({key: value for key, value in overrides.items()})
