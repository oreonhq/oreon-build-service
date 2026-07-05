import os
from unittest.mock import patch

from oreon_build.services import publisher as pub


def test_createrepo_workers_default_never_exceeds_c_max():
    with patch.object(pub, "_createrepo_cpu_count", return_value=64):
        with patch.dict(os.environ, {}, clear=True):
            assert pub._createrepo_workers() == 100
            assert pub._createrepo_workers_hard_cap() == 100


def test_createrepo_workers_clamps_explicit_above_c_max():
    with patch.object(pub, "_createrepo_cpu_count", return_value=8):
        env = {"OREON_CREATEREPO_WORKERS": "512"}
        with patch.dict(os.environ, env, clear=True):
            assert pub._createrepo_workers() == 100


def test_createrepo_workers_respects_lower_hard_cap():
    with patch.object(pub, "_createrepo_cpu_count", return_value=8):
        env = {
            "OREON_CREATEREPO_WORKERS": "99",
            "OREON_CREATEREPO_WORKERS_HARD_CAP": "32",
        }
        with patch.dict(os.environ, env, clear=True):
            assert pub._createrepo_workers() == 32


def test_createrepo_fallback_below_primary():
    with patch.dict(os.environ, {"OREON_CREATEREPO_WORKERS": "100"}, clear=True):
        fb = pub._createrepo_fallback_workers(100)
        assert fb < 100
        assert fb >= 1
