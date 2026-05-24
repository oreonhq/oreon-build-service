from oreon_build.services.build_chain import first_incomplete_stage_index_from_states


def test_resume_stage_starts_at_first_missing_jobs():
    assert first_incomplete_stage_index_from_states(3, {}) == 0


def test_resume_stage_after_completed_stages():
    jobs = {
        0: [(True, True), (True, True)],
        1: [(True, True)],
    }
    assert first_incomplete_stage_index_from_states(3, jobs) == 2


def test_resume_stage_stays_on_failed_build():
    jobs = {
        0: [(True, True)],
        1: [(True, True), (False, False)],
    }
    assert first_incomplete_stage_index_from_states(3, jobs) == 1


def test_resume_stage_stays_on_unpublished_success():
    jobs = {
        0: [(True, True)],
        1: [(True, False)],
    }
    assert first_incomplete_stage_index_from_states(2, jobs) == 1


def test_resume_stage_all_done():
    jobs = {
        0: [(True, True)],
        1: [(True, True)],
    }
    assert first_incomplete_stage_index_from_states(2, jobs) == 2
