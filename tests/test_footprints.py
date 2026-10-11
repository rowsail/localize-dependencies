# Footprint localization keeps the schematic in step: the symbols' Footprint
# fields follow the board, so "Update PCB from Schematic" doesn't swap the
# footprints back to the global library. Footprints whose library file is
# missing are left alone.
#
# Run: python tests/test_footprints.py   (or pytest)

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins"))
sys.path.insert(0, HERE)

import portability  # noqa: E402
from test_safety import _bytes, _write  # noqa: E402


def _project():
    """Board R1, R2, R3 on Lib:R and X1 on Lib:Gone (no such file). The
    schematic has R1 alone, and one symbol in a reused sheet that is R2 in one
    instance and R3 in the other."""
    root = tempfile.mkdtemp()
    proj = os.path.join(root, "proj")
    lib = os.path.join(root, "Lib.pretty").replace("\\", "/")
    _write(os.path.join(lib, "R.kicad_mod"), '(footprint "R")\n')
    _write(os.path.join(proj, "fp-lib-table"),
           '(fp_lib_table\n\t(version 7)\n'
           '\t(lib (name "Lib")(type "KiCad")(uri "{}")(options "")(descr ""))\n'
           ')\n'.format(lib))
    _write(os.path.join(proj, "board.kicad_pcb"),
           "(kicad_pcb\n" +
           "".join('\t(footprint "Lib:{}" (property "Reference" "{}"))\n'.format(f, r)
                   for r, f in (("R1", "R"), ("R2", "R"), ("R3", "R"), ("X1", "Gone"))) +
           ")\n")
    _write(os.path.join(proj, "board.kicad_sch"),
           "(kicad_sch\n"
           '\t(symbol (lib_id "S:R") (property "Reference" "R1") (property "Footprint" "Lib:R"))\n'
           "\t(sheet (property \"Sheetfile\" \"sub.kicad_sch\"))\n"
           '\t(symbol (lib_id "S:X") (property "Reference" "X1") (property "Footprint" "Lib:Gone"))\n'
           ")\n")
    _write(os.path.join(proj, "sub.kicad_sch"),
           "(kicad_sch\n"
           '\t(symbol (lib_id "S:R") (property "Reference" "R2") (property "Footprint" "Lib:R")\n'
           '\t\t(instances (project "p"\n'
           '\t\t\t(path "/a" (reference "R2") (unit 1))\n'
           '\t\t\t(path "/b" (reference "R3") (unit 1)))))\n'
           ")\n")
    return root, proj


def _fp_rows(proj, refs):
    return [r for r in portability.scan_items(proj)
            if r["type"] == "footprint" and r["ref"] in refs]


def _run(proj, rows):
    s = portability.Session(proj)
    res = portability.apply_selection(s, rows)
    s.save_manifest(res)
    return res


def _sch(proj, name):
    return open(os.path.join(proj, name), encoding="utf-8").read()


def test_schematic_field_follows_the_board():
    root, proj = _project()
    try:
        before_sub = _bytes(os.path.join(proj, "sub.kicad_sch"))
        res = _run(proj, _fp_rows(proj, {"R1"}))["footprints"]
        assert res["schematic_fields"] == 1 and res["schematic_skipped"] == 0, res
        assert '(property "Footprint" "Lib_local:R")' in _sch(proj, "board.kicad_sch")
        assert _bytes(os.path.join(proj, "sub.kicad_sch")) == before_sub, \
            "R2/R3 were not localized: their symbol stays as it was"
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert '(property "Footprint" "Lib:R")' in _sch(proj, "board.kicad_sch")
    finally:
        shutil.rmtree(root)


def test_shared_symbol_changes_only_when_every_instance_moves():
    root, proj = _project()
    try:
        res = _run(proj, _fp_rows(proj, {"R2"}))["footprints"]
        assert res["schematic_fields"] == 0 and res["schematic_skipped"] == 1, res
        assert '"Lib:R"' in _sch(proj, "sub.kicad_sch"), "R3 still uses Lib:R"
        res = _run(proj, _fp_rows(proj, {"R3"}))["footprints"]
        assert res["schematic_fields"] == 1, res
        assert '(property "Footprint" "Lib_local:R")' in _sch(proj, "sub.kicad_sch")
    finally:
        shutil.rmtree(root)


def test_missing_footprint_file_is_left_alone():
    root, proj = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        rows = _fp_rows(proj, {"X1"})
        assert rows and rows[0]["status"] == "external", rows
        res = _run(proj, rows)["footprints"]
        assert res["remapped"] == 0, res
        assert '(footprint "Lib:Gone"' in open(pcb, encoding="utf-8").read()
        assert '"Lib:Gone"' in _sch(proj, "board.kicad_sch")
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
