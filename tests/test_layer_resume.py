from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oreon_build.models import BuildStatus, LayerPlanStatus
from oreon_build.services.layer_plan import (
    _blocker_sources_from_reason,
    resume_layer_plan_by_user,
)


def test_blocker_sources_from_reason_shapes():
    assert _blocker_sources_from_reason("upstream xfdesktop failed") == ["xfdesktop"]
    assert _blocker_sources_from_reason("waiting on: xfdesktop (failed)") == ["xfdesktop"]
    assert _blocker_sources_from_reason("waiting on: a, b, c") == ["a", "b", "c"]
    assert _blocker_sources_from_reason("Layer plan stopped: Cancelled by user") == []
    assert _blocker_sources_from_reason("mock died hard") == []


class _ResumeDB:
    def __init__(self, plan, jobs, blockers):
        self.plan = plan
        self.jobs = jobs
        self.blockers = list(blockers)
        self.added = []
        self.deleted = []

    async def execute(self, stmt):
        text = str(stmt)
        result = MagicMock()
        if "layer_plans" in text.lower() or "LayerPlan" in text:
            result.scalar_one_or_none.return_value = self.plan
            return result
        if "layer_job_blockers" in text.lower() or "LayerJobBlocker" in text:
            if "DELETE" in text.upper():
                result.scalars.return_value.all.return_value = []
                return result
            jids = {int(j.id) for j in self.jobs if j.status in (BuildStatus.FAILED, BuildStatus.CANCELLED)}
            rows = [b for b in self.blockers if int(b.build_job_id) in jids]
            result.scalars.return_value.all.return_value = rows
            return result
        result.scalars.return_value.all.return_value = self.jobs
        return result

    def add(self, obj):
        self.added.append(obj)
        self.blockers.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)
        if obj in self.blockers:
            self.blockers.remove(obj)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_resume_reopens_failed_and_reblocks_waiters():
    plan = SimpleNamespace(id=42, status=LayerPlanStatus.FAILED, error_message="boom")
    ok_pkg = SimpleNamespace(name="libok")
    fail_pkg = SimpleNamespace(name="libfail")
    wait_pkg = SimpleNamespace(name="topapp")
    ok_job = SimpleNamespace(
        id=1, status=BuildStatus.SUCCESS, package=ok_pkg, blocked_reason=None, completed_at=None
    )
    fail_job = SimpleNamespace(
        id=2,
        status=BuildStatus.FAILED,
        package=fail_pkg,
        blocked_reason="compile error",
        completed_at="t",
    )
    wait_job = SimpleNamespace(
        id=3,
        status=BuildStatus.CANCELLED,
        package=wait_pkg,
        blocked_reason="upstream libfail failed",
        completed_at="t",
    )
    db = _ResumeDB(plan, [ok_job, fail_job, wait_job], [])
    user = SimpleNamespace(id=7)
    req = MagicMock()

    with patch("oreon_build.services.layer_plan.log_audit", new=AsyncMock()):
        out = await resume_layer_plan_by_user(db, req, user, layer_plan_id=42)

    assert out.status == LayerPlanStatus.RUNNING
    assert out.error_message is None
    assert fail_job.status == BuildStatus.PENDING
    assert fail_job.blocked_reason is None
    assert fail_job.completed_at is None
    assert wait_job.status == BuildStatus.BLOCKED
    assert "libfail" in (wait_job.blocked_reason or "")
    assert ok_job.status == BuildStatus.SUCCESS
    assert any(getattr(a, "blocking_source", None) == "libfail" for a in db.added)


@pytest.mark.asyncio
async def test_resume_rejects_running_plan():
    plan = SimpleNamespace(id=1, status=LayerPlanStatus.RUNNING, error_message=None)
    db = _ResumeDB(plan, [], [])
    with pytest.raises(Exception) as ei:
        await resume_layer_plan_by_user(db, MagicMock(), SimpleNamespace(id=1), layer_plan_id=1)
    assert ei.value.status_code == 400
