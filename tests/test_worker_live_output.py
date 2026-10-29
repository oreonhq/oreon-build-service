# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from oreon_build.worker.main import (
    MockLiveOutput,
    _default_worker_max_jobs,
    _mock_rpmbuild_smp_define_args,
    _mock_smp_jobs_auto,
    _worker_max_jobs_hint,
)


def test_mock_live_output_tail_trims():
    buf = MockLiveOutput(max_bytes=100)
    buf.append("a" * 80)
    buf.append("b" * 80)
    tail = buf.tail(max_bytes=50)
    assert len(tail) == 50
    assert tail.endswith("b" * 50)


def test_mock_smp_splits_cpus_by_max_jobs(monkeypatch):
    monkeypatch.setenv("OREON_WORKER_MAX_JOBS", "4")
    monkeypatch.delenv("OREON_WORKER_MOCK_SMP_JOBS", raising=False)
    monkeypatch.setattr("oreon_build.worker.main.os.cpu_count", lambda: 32)
    args = _mock_rpmbuild_smp_define_args()
    assert "_smp_build_ncpus 8" in args
    assert _worker_max_jobs_hint() == 4


def test_default_one_job_uses_all_cpus(monkeypatch):
    monkeypatch.delenv("OREON_WORKER_MAX_JOBS", raising=False)
    monkeypatch.delenv("OREON_WORKER_MOCK_SMP_JOBS", raising=False)
    monkeypatch.setattr("oreon_build.worker.main.os.cpu_count", lambda: 16)
    assert _default_worker_max_jobs() == 1
    assert _worker_max_jobs_hint() == 1
    assert _mock_smp_jobs_auto() == 16
    args = _mock_rpmbuild_smp_define_args()
    assert "_smp_build_ncpus 16" in args
