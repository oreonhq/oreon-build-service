from oreon_build.services.build_target_util import resolve_mock_environment_id


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
