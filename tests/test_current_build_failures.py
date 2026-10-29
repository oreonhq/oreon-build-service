# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from oreon_build.services.current_build_failures import is_spec_not_found_error


def test_is_spec_not_found_error():
    assert is_spec_not_found_error("Spec not found: foo/foo.spec")
    assert is_spec_not_found_error("## Git clone\n\nSpec not found: oreon11/foo/foo.spec")
    assert not is_spec_not_found_error("mock build failed")
    assert not is_spec_not_found_error(None)
    assert not is_spec_not_found_error("")
