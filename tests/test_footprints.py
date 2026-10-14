# Footprint localization: everything goes into ONE project library,
# libs/<project>.pretty (here the project folder is "proj"), and keeps the
# schematic in step: the symbols' Footprint fields follow the board, so
# "Update PCB from Schematic" doesn't swap the footprints back to the global
# library. Footprints whose library file is missing are rebuilt from the
# board.
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
        assert '(property "Footprint" "proj:R")' in _sch(proj, "board.kicad_sch")
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
        assert '(property "Footprint" "proj:R")' in _sch(proj, "sub.kicad_sch")
    finally:
        shutil.rmtree(root)


def test_missing_footprint_is_rebuilt_from_the_board():
    root, proj = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        rows = _fp_rows(proj, {"X1"})
        assert rows and rows[0]["status"] == "rebuild" and rows[0]["wanted"], rows
        res = _run(proj, rows)["footprints"]
        assert res["rebuilt_from_board"] == 1 and res["remapped"] == 1, res
        mod = os.path.join(proj, "libs", "proj.pretty", "Gone.kicad_mod")
        assert os.path.isfile(mod)
        assert '(footprint "proj:Gone"' in open(pcb, encoding="utf-8").read()
        assert '"proj:Gone"' in _sch(proj, "board.kicad_sch")
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert not os.path.exists(mod)
    finally:
        shutil.rmtree(root)


def _add_project_lib(proj, nick):
    lp = os.path.join(proj, "libs", nick + ".pretty")
    os.makedirs(lp)
    with open(os.path.join(proj, "fp-lib-table"), encoding="utf-8") as fh:
        t = fh.read()
    t = t.rstrip()[:-1] + ('\t(lib (name "{0}")(type "KiCad")'
                           '(uri "${{KIPRJMOD}}/libs/{0}.pretty")(options "")(descr ""))\n)\n'.format(nick))
    _write(os.path.join(proj, "fp-lib-table"), t)
    return lp


def test_missing_file_in_the_project_library_is_restored_in_place():
    root, proj = _project()
    try:
        lp = _add_project_lib(proj, "proj")
        pcb = os.path.join(proj, "board.kicad_pcb")
        with open(pcb, encoding="utf-8") as fh:
            b = fh.read()
        _write(pcb, b.replace('"Lib:Gone"', '"proj:Gone"'))
        before = _bytes(pcb)
        rows = _fp_rows(proj, {"X1"})
        assert rows[0]["status"] == "rebuild", rows
        res = _run(proj, rows)["footprints"]
        assert res["rebuilt_from_board"] == 1 and res["remapped"] == 0, res
        assert os.path.isfile(os.path.join(lp, "Gone.kicad_mod"))
        assert _bytes(pcb) == before, "the footprint id was already right"
        assert portability.revert(proj)["errors"] == []
        assert not os.path.exists(os.path.join(lp, "Gone.kicad_mod"))
    finally:
        shutil.rmtree(root)


def test_other_project_library_is_merged_and_removed():
    """An earlier run's libs/Lib_local.pretty: its footprints move into
    libs/proj.pretty, the board and schematic follow, and the emptied library
    leaves the table and the disk -- until Revert."""
    root, proj = _project()
    try:
        lp = _add_project_lib(proj, "Lib_local")
        _write(os.path.join(lp, "R.kicad_mod"), '(footprint "R")\n')
        pcb = os.path.join(proj, "board.kicad_pcb")
        sch = os.path.join(proj, "board.kicad_sch")
        for f in (pcb, sch):
            with open(f, encoding="utf-8") as fh:
                b = fh.read()
            _write(f, b.replace('"Lib:R"', '"Lib_local:R"', 1))
        before = {f: _bytes(f) for f in (pcb, sch, os.path.join(proj, "fp-lib-table"))}
        rows = _fp_rows(proj, {"R1"})
        assert rows[0]["status"] == "merge" and rows[0]["wanted"], rows
        res = _run(proj, rows)["footprints"]
        assert res["remapped"] == 1 and res["libraries_merged"] == ["Lib_local"], res
        assert os.path.isfile(os.path.join(proj, "libs", "proj.pretty", "R.kicad_mod"))
        assert '(footprint "proj:R"' in open(pcb, encoding="utf-8").read()
        assert '"proj:R"' in _sch(proj, "board.kicad_sch")
        table = open(os.path.join(proj, "fp-lib-table"), encoding="utf-8").read()
        assert '"Lib_local"' not in table and '(uri "${KIPRJMOD}/libs/proj.pretty")' in table, table
        assert not os.path.exists(lp)
        assert portability.revert(proj)["errors"] == []
        assert {f: _bytes(f) for f in before} == before
        assert os.path.isfile(os.path.join(lp, "R.kicad_mod")), "Revert brings it back"
    finally:
        shutil.rmtree(root)


def test_same_name_different_footprint_is_renamed():
    """Two libraries both have an "R": the second, different, one becomes
    R_Other; an identical one would simply be shared."""
    root, proj = _project()
    try:
        other = os.path.join(root, "Other.pretty").replace("\\", "/")
        _write(os.path.join(other, "R.kicad_mod"), '(footprint "R" (attr smd))\n')
        same = os.path.join(root, "Same.pretty").replace("\\", "/")
        _write(os.path.join(same, "R.kicad_mod"), '(footprint "R")\n')
        t = open(os.path.join(proj, "fp-lib-table"), encoding="utf-8").read()
        t = t.rstrip()[:-1] + "".join(
            '\t(lib (name "{}")(type "KiCad")(uri "{}")(options "")(descr ""))\n'.format(n, u)
            for n, u in (("Other", other), ("Same", same))) + ")\n"
        _write(os.path.join(proj, "fp-lib-table"), t)
        pcb = os.path.join(proj, "board.kicad_pcb")
        b = open(pcb, encoding="utf-8").read()
        b = b.replace('"Lib:R" (property "Reference" "R2")', '"Other:R" (property "Reference" "R2")')
        b = b.replace('"Lib:R" (property "Reference" "R3")', '"Same:R" (property "Reference" "R3")')
        _write(pcb, b)
        res = _run(proj, _fp_rows(proj, {"R1", "R2", "R3"}))["footprints"]
        assert res["renamed"] == {"Other:R": "R_Other"}, res
        lib = os.path.join(proj, "libs", "proj.pretty")
        assert sorted(os.listdir(lib)) == ["R.kicad_mod", "R_Other.kicad_mod"], os.listdir(lib)
        assert open(os.path.join(lib, "R_Other.kicad_mod")).read().startswith('(footprint "R_Other"')
        b = open(pcb, encoding="utf-8").read()
        assert b.count('"proj:R"') == 2 and b.count('"proj:R_Other"') == 1, b
    finally:
        shutil.rmtree(root)


def test_rebuilt_footprint_carries_no_part_numbers():
    import sexpr
    board = ('(kicad_pcb (version 20260206)\n\t(footprint "Lib:R0805" (layer "F.Cu") (at 1 2)\n'
             '\t\t(property "Reference" "R7" (at 0 0) (layer "F.SilkS"))\n'
             '\t\t(property "Value" "47k" (at 0 0) (layer "F.Fab"))\n'
             '\t\t(property "Datasheet" "http://x" (at 0 0) (layer "F.Fab"))\n'
             '\t\t(property "LCSC Part" "C17520" (at 0 0) (layer "F.Fab"))\n'
             '\t\t(property "MPN" "X" (at 0 0) (layer "F.Fab"))\n'
             '\t\t(sheetname "/") (sheetfile "a.kicad_sch")\n\t)\n)\n')
    fp = sexpr.parse(board).find_all("footprint")[0]
    flat = " ".join(portability.rebuild_footprint(board, fp, "20260206").split())
    assert '(property "Value" "R0805"' in flat and '(property "Datasheet" ""' in flat
    assert "C17520" not in flat and "MPN" not in flat and "47k" not in flat
    assert "sheetfile" not in flat
    keep = " ".join(portability.rebuild_footprint(board, fp, "20260206",
                                                  instance_fields=True).split())
    assert "C17520" in keep and '"47k"' in keep, "KiCad-export behaviour on request"


def test_flip_toggles_text_mirroring_on_sided_layers():
    import sexpr
    board = ('(kicad_pcb (version 20260206)\n\t(footprint "Lib:J" (layer "B.Cu") (at 0 0)\n'
             '\t\t(property "Reference" "J1" (at 0 0) (layer "B.SilkS")'
             ' (effects (font (size 1 1)) (justify mirror)))\n'
             '\t\t(fp_text user "front-layer note" (at 0 1) (layer "F.Fab")'
             ' (effects (font (size 1 1))))\n'
             '\t\t(fp_text user "PCB edge" (at 0 2) (layer "Dwgs.User")'
             ' (effects (font (size 1 1)) (justify left mirror)))\n\t)\n)\n')
    fp = sexpr.parse(board).find_all("footprint")[0]
    flat = " ".join(portability.rebuild_footprint(board, fp, "20260206",
                                                  instance_fields=True).split())
    ref = flat[flat.index('"REF**"'):flat.index("front-layer")]
    assert "mirror" not in ref, "back text comes to the front unmirrored"
    note = flat[flat.index("front-layer"):flat.index("PCB edge")]
    assert '(layer "B.Fab")' in note and "(justify mirror)" in note, \
        "unmirrored text on a sided layer comes out mirrored (toggled)"
    edge = flat[flat.index("PCB edge"):]
    assert '(layer "Dwgs.User")' in edge and "(justify left mirror)" in edge, \
        "text on a layer with no side keeps its mirroring"


def test_flip_mirrors_pad_chamfers_offsets_and_deltas():
    import sexpr
    board = ('(kicad_pcb (version 20260206)\n\t(footprint "Lib:P" (layer "B.Cu") (at 0 0)\n'
             '\t\t(property "Reference" "P1" (at 0 0) (layer "B.SilkS")'
             ' (effects (font (size 1 1)) (justify mirror)))\n'
             '\t\t(pad "1" thru_hole roundrect (at 0 0) (size 2 2) (drill 1 (offset 0.1 0.3))'
             ' (layers "*.Cu" "*.Mask") (chamfer_ratio 0.25) (chamfer top_left bottom_right))\n'
             '\t\t(pad "2" smd trapezoid (at 3 0) (size 2 2) (rect_delta 0 0.4)'
             ' (layers "B.Cu" "B.Mask"))\n\t)\n)\n')
    fp = sexpr.parse(board).find_all("footprint")[0]
    flat = " ".join(portability.rebuild_footprint(board, fp, "20260206").split())
    assert "(chamfer bottom_left top_right)" in flat, flat
    assert "(offset 0.1 -0.3)" in flat and "(rect_delta 0 -0.4)" in flat, flat
    assert '(layers "F.Cu" "F.Mask")' in flat


def test_flip_text_box_angle_and_dimension_height():
    import sexpr
    board = ('(kicad_pcb (version 20260206)\n\t(footprint "Lib:T" (layer "B.Cu") (at 5 5)\n'
             '\t\t(property "Reference" "T1" (at 0 0) (layer "B.SilkS")'
             ' (effects (font (size 1 1)) (justify mirror)))\n'
             '\t\t(fp_text_box "BOX" (start -1.5 -1.5) (end 1.5 -3) (angle 180) (layer "B.SilkS")'
             ' (effects (font (size 1 1)) (justify left mirror)))\n'
             '\t\t(dimension (type aligned) (layer "B.Fab") (pts (xy 3 7) (xy 7 7)) (height 1)'
             ' (gr_text "4" (at 5 8 0) (layer "B.Fab") (effects (font (size 1 1)) (justify mirror))))\n'
             '\t)\n)\n')
    fp = sexpr.parse(board).find_all("footprint")[0]
    flat = " ".join(portability.rebuild_footprint(board, fp, "20260206").split())
    box = flat[flat.index("fp_text_box"):flat.index("(dimension")]
    assert "(angle" not in box and "mirror" not in box and "(start -1.5 1.5)" in box, box
    dim = flat[flat.index("(dimension"):]
    assert "(height -1)" in dim and "(pts (xy -2 -2) (xy 2 -2))" in dim, dim
    assert "mirror" not in dim and '(layer "F.Fab")' in dim, dim


def test_left_right_flip_is_top_bottom_turned_180():
    import sexpr
    board = ('(kicad_pcb (version 20260206)\n\t(footprint "Lib:L" (layer "B.Cu") (at 0 0)\n'
             '\t\t(property "Reference" "L1" (at 1 -2 0) (layer "B.SilkS")'
             ' (effects (font (size 1 1)) (justify mirror)))\n'
             '\t\t(fp_line (start 1 -0.5) (end 3 -0.5) (layer "B.SilkS"))\n'
             '\t\t(pad "1" smd rect (at 2 0.5 0) (size 1 2) (layers "B.Cu"))\n'
             '\t\t(model "x.step" (offset (xyz 0.5 0.25 0)) (rotate (xyz 0 0 90)))\n\t)\n)\n')
    fp = sexpr.parse(board).find_all("footprint")[0]
    tb = " ".join(portability.rebuild_footprint(board, fp, "20260206").split())
    lr = " ".join(portability.rebuild_footprint(board, fp, "20260206", flip="left_right").split())
    assert '(property "Reference" "REF**" (at 1 2 180)' in tb and "(start 1 0.5) (end 3 0.5)" in tb
    assert '(property "Reference" "REF**" (at -1 -2)' in lr, \
        "top-bottom result (1, 2, 180) turned: (-1, -2), angle 180 + 180 = 0"
    assert "(start -1 -0.5) (end -3 -0.5)" in lr
    assert '(pad "1" smd rect (at -2 0.5 180)' in lr, "pad turned with the footprint"
    assert "(offset (xyz -0.5 -0.25 0))" in lr and "(rotate (xyz 0 0 -90))" in lr, \
        "the 3D model turns too"


def test_flip_direction_follows_kicad_setting():
    import json
    import tempfile
    d = tempfile.mkdtemp()
    saved = portability._config_dirs
    try:
        portability._config_dirs = lambda: [d]
        assert portability.flip_direction() == "top_bottom", "no config: KiCad's default"
        with open(os.path.join(d, "pcbnew.json"), "w") as fh:
            json.dump({"editing": {"flip_left_right": True}}, fh)
        assert portability.flip_direction() == "left_right"
    finally:
        portability._config_dirs = saved
        shutil.rmtree(d)


BACK_FP = """(kicad_pcb (version 20260206)
\t(footprint "Lib:Q"
\t\t(layer "B.Cu")
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(at 10 20 90)
\t\t(property "Reference" "Q1"
\t\t\t(at 0 -1.5 90)
\t\t\t(layer "B.SilkS")
\t\t\t(effects (font (size 1 1)) (justify mirror))
\t\t)
\t\t(path "/abc")
\t\t(sheetname "/")
\t\t(sheetfile "board.kicad_sch")
\t\t(units (unit (name "A") (pins "1")))
\t\t(fp_line (start -1 -0.5) (end 1 -0.5) (layer "B.SilkS"))
\t\t(pad "1" smd rect (at 0.5 0.25 90) (size 1 1) (layers "B.Cu" "B.Paste" "B.Mask") (net 3 "GND"))
\t\t(model "kicad-embed://Q.step")
\t)
\t(embedded_files (file (name "Q.step") (type model) (data |AAAA|) (checksum "0")))
)
"""


def test_rebuild_undoes_rotation_and_flip():
    import sexpr
    root = sexpr.parse(BACK_FP)
    fp = root.find_all("footprint")[0]
    mod = portability.rebuild_footprint(BACK_FP, fp, "20260206",
                                        portability._embedded_files(root), instance_fields=True)
    sexpr.parse(mod)
    flat = " ".join(mod.split())
    assert flat.startswith('(footprint "Q" (version 20260206) (generator "pcbnew")'), flat[:80]
    assert '(layer "F.Cu")' in flat and "B." not in flat, "flipped to the front"
    assert '(property "Reference" "REF**" (at 0 1.5 180)' in flat, \
        "text: y mirrored, angle 180 - (90 - 90) = 180"
    assert "mirror" not in flat and "(justify" not in flat
    assert '(fp_line (start -1 0.5) (end 1 0.5) (layer "F.SilkS"))' in flat
    assert '(pad "1" smd rect (at 0.5 -0.25) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))' in flat, \
        "pad: y mirrored, angle -(90 - 90) = 0, net cleared"
    for gone in ("(at 10 20", "(path ", "(units ", "(net ", '"11111111-'):
        assert gone not in flat, gone
    assert '(sheetfile "board.kicad_sch")' in flat, "kept, as KiCad's library save does"
    assert '(embedded_files (file (name "Q.step")' in flat, "embedded model travels along"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
