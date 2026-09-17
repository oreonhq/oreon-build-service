from oreon_build.services.discovery import cap_atom, caps_overlap, name_level_cycles


def test_cap_atom_strips_evr() -> None:
    assert cap_atom("pkgconfig(foo) >= 1.2") == "pkgconfig(foo)"


def test_caps_overlap_uses_actual_provides() -> None:
    assert caps_overlap(["pkgconfig(foo)"], ["pkgconfig(foo) = 1.0-1"])
    assert not caps_overlap(["pkgconfig(foo)"], ["pkgconfig(bar)"])


def test_name_level_cycle_and_downstream() -> None:
    blocked = [
        ("A", ["B"]),
        ("B", ["A"]),
        ("C", ["A"]),
        ("D", ["pkgconfig(missing)"]),
    ]
    cycles, downstream = name_level_cycles(blocked)
    assert any("A" in c and "B" in c for c in cycles)
    assert downstream >= 1
