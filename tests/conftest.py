# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _noop_distgit_provides_index():
    async def inner(**kwargs):
        return None

    with patch(
        "oreon_build.services.distgit_order.get_distgit_provides_index_cached",
        side_effect=inner,
    ):
        yield
