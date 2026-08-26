from oreon_build.models import BuildStatus
from oreon_build.services.layer_engine import _LAYER_JOB_WRITABLE


def test_layer_scheduler_cannot_rewrite_running_jobs():
    assert BuildStatus.RUNNING not in _LAYER_JOB_WRITABLE
    assert BuildStatus.SUCCESS not in _LAYER_JOB_WRITABLE
    assert BuildStatus.FAILED not in _LAYER_JOB_WRITABLE
    assert BuildStatus.QUEUED in _LAYER_JOB_WRITABLE
    assert BuildStatus.BLOCKED in _LAYER_JOB_WRITABLE
