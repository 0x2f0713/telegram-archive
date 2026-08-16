from pathlib import Path

import pytest

from app.config import ConfigurationError, Settings, apply_runtime_overrides


def test_default_storage_mode_is_local() -> None:
    settings = Settings(_env_file=None)

    assert settings.storage_mode == "local"
    assert settings.terabox_enabled is False
    assert settings.media_storage_roots() == (settings.download_dir.expanduser().resolve(),)


def test_invalid_storage_mode_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, storage_mode="cloud")


def test_terabox_storage_roots_include_mount_dir(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        storage_mode="terabox",
        terabox_ndus="token",
        download_dir=tmp_path / "downloads",
        terabox_mount_dir=tmp_path / "mnt" / "terabox",
    )

    roots = settings.media_storage_roots()

    assert roots[0] == (tmp_path / "downloads").resolve()
    assert roots[1] == (tmp_path / "mnt" / "terabox").resolve()


def test_terabox_policy_disables_local_only_media_flags() -> None:
    settings = Settings(
        _env_file=None,
        storage_mode="terabox",
        terabox_ndus="token",
        media_faststart=True,
        media_variants=True,
    )

    effective = settings.with_terabox_policy()

    assert effective.media_faststart is True
    assert effective.media_variants is False


def test_local_policy_keeps_media_flags() -> None:
    settings = Settings(_env_file=None, media_faststart=True, media_variants=True)

    assert settings.with_terabox_policy() is settings


def test_policy_still_applied_after_runtime_overrides() -> None:
    settings = Settings(
        _env_file=None,
        storage_mode="terabox",
        terabox_ndus="token",
        media_variants=False,
    )

    reenabled = apply_runtime_overrides(settings, {"media_variants": "true"})

    assert reenabled.media_variants is True
    assert reenabled.with_terabox_policy().media_variants is False


def test_require_terabox_ndus_prefers_env_value() -> None:
    settings = Settings(_env_file=None, terabox_ndus="from-env", terabox_profile=None)

    assert settings.require_terabox_ndus() == "from-env"


def test_require_terabox_ndus_reads_unidisk_profile(tmp_path: Path) -> None:
    profile = tmp_path / "terabox.profile.json"
    profile.write_text('{"module": "TeraBox", "ndus": "from-profile"}', encoding="utf-8")
    settings = Settings(_env_file=None, terabox_profile=profile)

    assert settings.require_terabox_ndus() == "from-profile"


def test_require_terabox_ndus_missing_has_no_secret_in_message() -> None:
    settings = Settings(_env_file=None, terabox_profile=Path("/nonexistent/profile.json"))

    with pytest.raises(ConfigurationError, match="TERABOX_NDUS is required"):
        settings.require_terabox_ndus()


def test_terabox_remote_root_normalization() -> None:
    settings = Settings(_env_file=None, terabox_remote_dir="Telegram Archive/")

    assert settings.terabox_remote_root == "/Telegram Archive"
