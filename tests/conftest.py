from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _noop_distgit_provides_index():
    async def inner(**kwargs):
        return None

    def _no_live_solver(*_a, **_k):
        raise RuntimeError("live libsolv solver disabled in unit tests")

    with (
        patch(
            "oreon_build.services.distgit_order.get_distgit_provides_index_cached",
            side_effect=inner,
        ),
        patch(
            "oreon_build.services.dep_plan.planner.get_distgit_provides_index_cached",
            side_effect=inner,
        ),
        patch(
            "oreon_build.services.dep_plan.planner.get_repo_solver",
            side_effect=_no_live_solver,
        ),
    ):
        yield
