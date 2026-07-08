from oreon_build.core.chain_build_spec import parse_chain_build_spec


def test_parse_colon_parallel_groups():
    assert parse_chain_build_spec("a b : c : d") == [["a", "b"], ["c"], ["d"]]
    assert parse_chain_build_spec("libwidget libaselib : libgizmo") == [
        ["libwidget", "libaselib"],
        ["libgizmo"],
    ]


def test_parse_no_colon_sequential():
    assert parse_chain_build_spec("bash grep coreutils") == [["bash"], ["grep"], ["coreutils"]]


def test_parse_dedupes_case_insensitive_within_group():
    assert parse_chain_build_spec("Foo foo : bar") == [["Foo"], ["bar"]]


def test_parse_strips_and_skips_empty_segments():
    assert parse_chain_build_spec("  x  :  y  ") == [["x"], ["y"]]
