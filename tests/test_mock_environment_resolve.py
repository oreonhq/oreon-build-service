from types import SimpleNamespace

from oreon_build.services.build_target_util import (
    choose_mock_environment_id,
    is_bootstrap_mock_environment,
    resolve_mock_environment_id,
)


def test_resolve_mock_prefers_trigger_override() -> None:
    assert (
        resolve_mock_environment_id(
            trigger_mock_environment_id=9,
            package_mock_environment_id=3,
            release_default_mock_environment_id=1,
        )
        == 9
    )


def test_resolve_mock_falls_back_to_package() -> None:
    assert (
        resolve_mock_environment_id(
            trigger_mock_environment_id=None,
            package_mock_environment_id=3,
            release_default_mock_environment_id=1,
        )
        == 3
    )


def test_resolve_mock_falls_back_to_release_default() -> None:
    assert (
        resolve_mock_environment_id(
            trigger_mock_environment_id=None,
            package_mock_environment_id=None,
            release_default_mock_environment_id=1,
        )
        == 1
    )


def test_resolve_mock_can_ignore_sticky_package_bootstrap() -> None:
    assert (
        resolve_mock_environment_id(
            trigger_mock_environment_id=None,
            package_mock_environment_id=5,
            release_default_mock_environment_id=1,
            ignore_package_mock=True,
        )
        == 1
    )


def test_bootstrap_mock_detection() -> None:
    assert is_bootstrap_mock_environment(name="Fedora 44 (bootstrap)", config_name="fedora-44")
    assert is_bootstrap_mock_environment(name="Oreon 11", config_name="fedora-43")
    assert not is_bootstrap_mock_environment(name="Oreon 11 (RP1)", config_name="oreon-11-rp1")


def test_choose_mock_uses_release_default_not_oldest_fedora() -> None:
    fedora = SimpleNamespace(id=1, release_id=11, name="Fedora 44", config_name="fedora-44")
    oreon = SimpleNamespace(id=7, release_id=11, name="Oreon 11 (RP1)", config_name="oreon-11-rp1")
    assert (
        choose_mock_environment_id(
            preferred_id=None,
            release_id=11,
            release_default_id=7,
            environments=[fedora, oreon],
        )
        == 7
    )


def test_choose_mock_chain_default_ignores_missing_pin() -> None:
    fedora = SimpleNamespace(id=1, release_id=11, name="Fedora 44", config_name="fedora-44")
    oreon = SimpleNamespace(id=7, release_id=11, name="Oreon 11 (RP1)", config_name="oreon-11-rp1")
    assert (
        choose_mock_environment_id(
            preferred_id=None,
            release_id=11,
            release_default_id=None,
            environments=[fedora, oreon],
        )
        == 7
    )


def test_choose_mock_respects_explicit_fedora_pin() -> None:
    fedora = SimpleNamespace(id=1, release_id=11, name="Fedora 44", config_name="fedora-44")
    oreon = SimpleNamespace(id=7, release_id=11, name="Oreon 11 (RP1)", config_name="oreon-11-rp1")
    assert (
        choose_mock_environment_id(
            preferred_id=1,
            release_id=11,
            release_default_id=7,
            environments=[fedora, oreon],
        )
        == 1
    )
