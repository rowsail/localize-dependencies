# Symbol localization: the selected symbols (with their 'extends' parents)
# go into ONE project library, libs/<project>.kicad_sym (here the project
# folder is "proj"), from single-file .kicad_sym libraries and KiCad 10
# folder libraries (one .kicad_sym per symbol, *.kicad_symdir) alike;
# libraries whose source is missing are left alone, never remapped to a copy
# that doesn't exist.
#
# Run: python tests/test_symbols.py   (or pytest)

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins"))
sys.path.insert(0, HERE)

import portability  # noqa: E402
import sexpr  # noqa: E402
from test_safety import _bytes, _write  # noqa: E402

LIB_HEAD = '(kicad_symbol_lib (version 20251024) (generator "test")\n'


def _project():
    """Temp project using three symbol libraries: 'One' (a .kicad_sym file),
    'Dir' (a folder library: R and C each in their own file, plus a stray
    non-library file), and 'Gone' (its folder does not exist)."""
    root = tempfile.mkdtemp()
    proj = os.path.join(root, "proj")
    one = os.path.join(root, "ext", "One.kicad_sym").replace("\\", "/")
    sdir = os.path.join(root, "ext", "Dir.kicad_symdir").replace("\\", "/")
    gone = os.path.join(root, "ext", "Gone.kicad_symdir").replace("\\", "/")
    _write(one, LIB_HEAD + '\t(symbol "U")\n)\n')
    _write(os.path.join(sdir, "R.kicad_sym"), LIB_HEAD + '\t(symbol "R")\n)\n')
    _write(os.path.join(sdir, "C.kicad_sym"), LIB_HEAD + '\t(symbol "C")\n)\n')
    _write(os.path.join(sdir, "README.txt"), "not a library file")
    _write(os.path.join(proj, "sym-lib-table"),
           '(sym_lib_table\n\t(version 7)\n' +
           ''.join('\t(lib (name "{}")(type "KiCad")(uri "{}")(options "")(descr ""))\n'
                   .format(n, u) for n, u in (("One", one), ("Dir", sdir), ("Gone", gone))) +
           ')\n')
    _write(os.path.join(proj, "top.kicad_sch"),
           "(kicad_sch\n"
           '\t(lib_symbols (symbol "One:U") (symbol "Dir:R") (symbol "Dir:C") (symbol "Gone:X"))\n'
           '\t(symbol (lib_id "One:U") (property "Reference" "U1"))\n'
           '\t(symbol (lib_id "Dir:R") (property "Reference" "R1"))\n'
           '\t(symbol (lib_id "Dir:C") (property "Reference" "C1"))\n'
           '\t(symbol (lib_id "Gone:X") (property "Reference" "X1"))\n'
           ")\n")
    return root, proj, sdir


def _sym_rows(proj):
    return [r for r in portability.scan_items(proj) if r["type"] == "symbol"]


def _run(proj, rows):
    s = portability.Session(proj)
    s.save_manifest(portability.apply_selection(s, rows))


def test_scan_sees_folder_library_as_external():
    root, proj, _ = _project()
    try:
        st = {r["ref"]: r["status"] for r in _sym_rows(proj)}
        assert st == {"U1": "external", "R1": "external", "C1": "external",
                      "X1": "missing"}, st
    finally:
        shutil.rmtree(root)


def test_folder_library_symbol_goes_into_the_project_library():
    root, proj, sdir = _project()
    try:
        _run(proj, [r for r in _sym_rows(proj) if r["ref"] == "R1"])
        assert sorted(os.listdir(os.path.join(proj, "libs"))) == ["proj.kicad_sym"]
        lib = sexpr.parse(open(os.path.join(proj, "libs", "proj.kicad_sym"), encoding="utf-8").read())
        assert [s.children[1].value for s in lib.find_all("symbol")] == ["R"], \
            "only the selected symbol"
        table = open(os.path.join(proj, "sym-lib-table"), encoding="utf-8").read()
        assert '(name "proj")(type "KiCad")(uri "${KIPRJMOD}/libs/proj.kicad_sym")' in table
        sch = open(os.path.join(proj, "top.kicad_sch"), encoding="utf-8").read()
        assert '(lib_id "proj:R")' in sch and '(symbol "proj:R")' in sch, "lib_symbols cache follows"
        assert '(lib_id "Dir:C")' in sch and '(lib_id "One:U")' in sch, "unselected: untouched"
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert not os.path.exists(os.path.join(proj, "libs"))
    finally:
        shutil.rmtree(root)


def test_single_file_library_still_works():
    root, proj, _ = _project()
    try:
        _run(proj, [r for r in _sym_rows(proj) if r["ref"] in ("U1", "C1")])
        lib = os.path.join(proj, "libs", "proj.kicad_sym")
        names = [s.children[1].value for s in sexpr.parse(open(lib, encoding="utf-8").read())
                 .find_all("symbol")]
        assert sorted(names) == ["C", "U"], names
        table = open(os.path.join(proj, "sym-lib-table"), encoding="utf-8").read()
        assert table.count('(uri "${KIPRJMOD}/libs/proj.kicad_sym")') == 1, table
        sch = open(os.path.join(proj, "top.kicad_sch"), encoding="utf-8").read()
        assert '(lib_id "proj:U")' in sch and '(lib_id "proj:C")' in sch
    finally:
        shutil.rmtree(root)


def test_earlier_local_library_is_merged_and_removed():
    """An earlier run's libs/One_local.kicad_sym: offered as "merge", its
    symbol moves into libs/proj.kicad_sym and the emptied library goes."""
    root, proj, _ = _project()
    try:
        old = os.path.join(proj, "libs", "One_local.kicad_sym")
        _write(old, LIB_HEAD + '\t(symbol "U")\n)\n')
        t = open(os.path.join(proj, "sym-lib-table"), encoding="utf-8").read()
        _write(os.path.join(proj, "sym-lib-table"), t.rstrip()[:-1] +
               '\t(lib (name "One_local")(type "KiCad")(uri "${KIPRJMOD}/libs/One_local.kicad_sym")'
               '(options "")(descr ""))\n)\n')
        sch_path = os.path.join(proj, "top.kicad_sch")
        _write(sch_path, open(sch_path, encoding="utf-8").read().replace('"One:U"', '"One_local:U"'))
        rows = [r for r in _sym_rows(proj) if r["ref"] == "U1"]
        assert rows[0]["status"] == "merge" and rows[0]["wanted"], rows
        s = portability.Session(proj)
        res = portability.apply_selection(s, rows)
        s.save_manifest(res)
        assert res["symbols"]["libraries_merged"] == ["One_local"], res
        assert not os.path.exists(old)
        sch = open(sch_path, encoding="utf-8").read()
        assert '(lib_id "proj:U")' in sch and '(symbol "proj:U")' in sch
        assert "One_local" not in open(os.path.join(proj, "sym-lib-table"), encoding="utf-8").read()
        assert portability.revert(proj)["errors"] == []
        assert os.path.isfile(old), "Revert brings it back"
    finally:
        shutil.rmtree(root)


def test_clashing_symbol_is_renamed_and_extends_follows():
    """'Dir' and 'Two' both have an R, different: Two's becomes R_Two, with
    its unit sub-symbols; Two's D extends R, so it now extends R_Two."""
    root, proj, _ = _project()
    try:
        two = os.path.join(root, "ext", "Two.kicad_sym").replace("\\", "/")
        _write(two, LIB_HEAD + '\t(symbol "R" (property "Value" "two") (symbol "R_1_1"))\n'
               '\t(symbol "D" (extends "R"))\n)\n')
        t = open(os.path.join(proj, "sym-lib-table"), encoding="utf-8").read()
        _write(os.path.join(proj, "sym-lib-table"), t.rstrip()[:-1] +
               '\t(lib (name "Two")(type "KiCad")(uri "{}")(options "")(descr ""))\n)\n'.format(two))
        sch_path = os.path.join(proj, "top.kicad_sch")
        sch = open(sch_path, encoding="utf-8").read().replace(
            '(symbol "Gone:X")', '(symbol "Two:D" (symbol "R_1_1"))').replace(
            '(lib_id "Gone:X")', '(lib_id "Two:D")')
        _write(sch_path, sch)
        _run(proj, [r for r in _sym_rows(proj) if r["ref"] in ("R1", "X1")])
        lib = open(os.path.join(proj, "libs", "proj.kicad_sym"), encoding="utf-8").read()
        flat = " ".join(lib.split())
        assert '(symbol "R")' in flat, flat
        assert '(symbol "R_Two" (property "Value" "two") (symbol "R_Two_1_1"))' in flat, flat
        assert '(symbol "D" (extends "R_Two"))' in flat, flat
        sch = open(sch_path, encoding="utf-8").read()
        assert '(lib_id "proj:R")' in sch and '(lib_id "proj:D")' in sch, sch
    finally:
        shutil.rmtree(root)


def test_missing_library_is_not_remapped():
    root, proj, _ = _project()
    try:
        sch_path = os.path.join(proj, "top.kicad_sch")
        before = _bytes(sch_path)
        row = [r for r in _sym_rows(proj) if r["ref"] == "X1"][0]
        s = portability.Session(proj)
        res = portability.apply_selection(s, [row])
        assert res["symbols"]["libraries"] == 0, res
        assert _bytes(sch_path) == before, "no reference to a copy that doesn't exist"
        table = open(os.path.join(proj, "sym-lib-table"), encoding="utf-8").read()
        assert "Gone_local" not in table
    finally:
        shutil.rmtree(root)


def _fields_project():
    """Library 'One': U (LCSC, MPN), parent P (Mfr) with V extending it
    (LCSC), and W (LCSC) that nothing places. Every placed U and V has had
    its LCSC field deleted; one U keeps MPN, the other is DNP without it."""
    root = tempfile.mkdtemp()
    proj = os.path.join(root, "proj")
    one = os.path.join(root, "ext", "One.kicad_sym").replace("\\", "/")
    p = lambda n, v: '(property "{}" "{}")'.format(n, v)  # noqa: E731
    _write(one, LIB_HEAD +
           '\t(symbol "U"\n\t\t' + p("Reference", "U") + '\n\t\t' + p("ki_keywords", "k") +
           '\n\t\t' + p("LCSC", "C1") + '\n\t\t' + p("MPN", "M1") + '\n\t)\n'
           '\t(symbol "P"\n\t\t' + p("Mfr", "Acme") + '\n\t)\n'
           '\t(symbol "V"\n\t\t(extends "P")\n\t\t' + p("LCSC", "C2") + '\n\t)\n'
           '\t(symbol "W"\n\t\t' + p("LCSC", "C3") + '\n\t)\n)\n')
    _write(os.path.join(proj, "sym-lib-table"),
           '(sym_lib_table\n\t(version 7)\n'
           '\t(lib (name "One")(type "KiCad")(uri "{}")(options "")(descr ""))\n)\n'.format(one))
    _write(os.path.join(proj, "top.kicad_sch"),
           "(kicad_sch\n\t(lib_symbols\n"
           '\t\t(symbol "One:U"\n\t\t\t' + p("LCSC", "C1") + '\n\t\t\t' + p("MPN", "M1") + '\n\t\t)\n'
           '\t\t(symbol "One:V"\n\t\t\t' + p("Mfr", "Acme") + '\n\t\t\t' + p("LCSC", "C2") + '\n\t\t)\n'
           '\t)\n'
           '\t(symbol (lib_id "One:U") ' + p("Reference", "U1") + ' ' + p("MPN", "M1") + ')\n'
           '\t(symbol (lib_id "One:U") (dnp yes) ' + p("Reference", "U2") + ')\n'
           '\t(symbol (lib_id "One:V") ' + p("Reference", "V1") + ' ' + p("Mfr", "Acme") + ')\n'
           ")\n")
    return root, proj


def test_fields_deleted_from_placed_symbols_leave_the_library():
    root, proj = _fields_project()
    try:
        sch_path = os.path.join(proj, "top.kicad_sch")
        before = _bytes(sch_path)
        s = portability.Session(proj)
        res = portability.apply_selection(s, _sym_rows(proj))
        s.save_manifest(res)
        assert res["symbols"]["fields_changed"] == 2, res
        lib = sexpr.parse(open(os.path.join(proj, "libs", "proj.kicad_sym"),
                               encoding="utf-8").read())
        got = {sy.children[1].value: sorted(portability.user_fields(sy))
               for sy in lib.find_all("symbol")}
        assert got == {"U": ["MPN"], "P": ["Mfr"], "V": []}, \
            "W isn't placed: not copied; P comes along as V's parent"
        assert '"ki_keywords"' in open(os.path.join(proj, "libs", "proj.kicad_sym"),
                                       encoding="utf-8").read()
        sch = sexpr.parse(open(sch_path, encoding="utf-8").read())
        cache = {sy.children[1].value: sorted(portability.user_fields(sy))
                 for sy in sch.find("lib_symbols").find_all("symbol")}
        assert cache == {"proj:U": ["MPN"], "proj:V": ["Mfr"]}, cache
        assert portability.revert(proj)["errors"] == []
        assert _bytes(sch_path) == before
    finally:
        shutil.rmtree(root)


def _stale_project():
    """Project library 'proj' (a folder library): R330 still says FOJAN and
    'LCSC Part' C2933198, while every placed R330 now has MPN
    AF0603FR-07330RL and 'LCSC' C144102. Generic R is placed with two
    different values. Q is up to date (its placed Datasheet "~" is KiCad's
    old way of writing empty)."""
    root = tempfile.mkdtemp()
    proj = os.path.join(root, "proj")
    lib = os.path.join(proj, "libs", "proj.kicad_symdir")
    p = lambda n, v: '(property "{}" "{}" (at 0 0 0))'.format(n, v)  # noqa: E731

    def sym(name, *props):
        return LIB_HEAD + '\t(symbol "{}"\n'.format(name) + "".join(
            "\t\t" + p(*kv) + "\n" for kv in props) + "\t)\n)\n"
    _write(os.path.join(lib, "R330.kicad_sym"), sym(
        "R330", ("Reference", "R"), ("Value", "FRC0603F3300TS"), ("ki_keywords", "R"),
        ("MPN", "FRC0603F3300TS"), ("LCSC Part", "C2933198")))
    _write(os.path.join(lib, "R.kicad_sym"), sym("R", ("Reference", "R"), ("Value", "R")))
    _write(os.path.join(lib, "Q.kicad_sym"), sym("Q", ("Reference", "Q"), ("Datasheet", ""),
                                                          ("LCSC", "C1")))
    _write(os.path.join(proj, "sym-lib-table"),
           '(sym_lib_table\n\t(version 7)\n\t(lib (name "proj")(type "KiCad")'
           '(uri "${KIPRJMOD}/libs/proj.kicad_symdir")(options "")(descr ""))\n)\n')
    r330 = lambda ref, ln="": ('\t(symbol (lib_id "proj:R330") ' + ln + p("Reference", ref) + " " +  # noqa: E731
                        p("Value", "330R 0603") + " " + p("MPN", "AF0603FR-07330RL") + " " +
                        p("LCSC", "C144102") + ")\n")
    _write(os.path.join(proj, "top.kicad_sch"),
           "(kicad_sch\n\t(lib_symbols\n"
           '\t\t(symbol "proj:R330"\n\t\t\t' + p("Value", "FRC0603F3300TS") +
           '\n\t\t\t' + p("MPN", "FRC0603F3300TS") + '\n\t\t\t' + p("LCSC Part", "C2933198") +
           '\n\t\t)\n\t\t(symbol "R330_1"\n\t\t\t' + p("Value", "FRC0603F3300TS") +
           '\n\t\t\t' + p("LCSC Part", "C2933198") +
           '\n\t\t)\n\t\t(symbol "proj:R"\n\t\t\t' + p("Value", "R") + '\n\t\t)\n'
           '\t\t(symbol "proj:Q"\n\t\t\t' + p("LCSC", "C1") + '\n\t\t)\n\t)\n' +
           r330("R13") + r330("R55", '(lib_name "R330_1") ') +
           '\t(symbol (lib_id "proj:R") ' + p("Reference", "R1") + " " + p("Value", "10k") + ")\n"
           '\t(symbol (lib_id "proj:R") ' + p("Reference", "R2") + " " + p("Value", "1k") + ")\n"
           '\t(symbol (lib_id "proj:Q") ' + p("Reference", "Q1") + " " + p("Datasheet", "~") + " " +
           p("LCSC", "C1") + ")\n"
           ")\n")
    return root, proj


def test_project_library_out_of_step_is_offered_and_updated():
    root, proj = _stale_project()
    try:
        rows = _sym_rows(proj)
        st = {r["ref"]: (r["status"], r["wanted"]) for r in rows}
        assert st == {"R13": ("update", True), "R55": ("update", True),
                      "R1": ("local", False), "R2": ("local", False),
                      "Q1": ("local", False)}, st
        sch_path = os.path.join(proj, "top.kicad_sch")
        before = _bytes(sch_path)
        s = portability.Session(proj)
        res = portability.apply_selection(s, [r for r in rows if r["wanted"]])
        s.save_manifest(res)
        assert res["symbol_updates"] == {"libraries": 1, "fields_changed": 4,
                                         "schematic_copies": 2}, res
        assert "symbols" not in res, "a project library is updated, not copied"
        assert sorted(os.listdir(os.path.join(proj, "libs"))) == ["proj.kicad_symdir"]
        lib = os.path.join(proj, "libs", "proj.kicad_symdir")
        defs, _ = portability.library_definitions(lib)
        assert defs["R330"][0] == {"Reference": "R", "Value": "330R 0603",
                                   "MPN": "AF0603FR-07330RL", "LCSC": "C144102"}, defs["R330"]
        assert '"ki_keywords"' in open(os.path.join(lib, "R330.kicad_sym"), encoding="utf-8").read()
        assert defs["R"][0]["Value"] == "R", "placed values disagree: left alone"
        sch = sexpr.parse(open(sch_path, encoding="utf-8").read())
        cache = {sy.children[1].value: {n: c.children[2].value
                                        for n, c in portability._fields(sy).items()}
                 for sy in sch.find("lib_symbols").find_all("symbol")}
        assert cache["proj:R330"] == {"Value": "330R 0603", "MPN": "AF0603FR-07330RL",
                                      "LCSC": "C144102"}, cache
        assert cache["R330_1"] == {"Value": "330R 0603", "MPN": "AF0603FR-07330RL",
                                   "LCSC": "C144102"}, \
            "a renamed cache copy (lib_name) follows its lib_id"
        assert cache["proj:R"] == {"Value": "R"}
        again = {r["ref"]: r["status"] for r in _sym_rows(proj)}
        assert set(again.values()) == {"local"}, again
        assert portability.revert(proj)["errors"] == []
        assert _bytes(sch_path) == before
        assert "C2933198" in open(os.path.join(lib, "R330.kicad_sym"), encoding="utf-8").read()
    finally:
        shutil.rmtree(root)


def test_schematic_copy_saved_back_over_an_update_is_offered_and_repaired():
    """KiCad saved its old lib_symbols copy back over a run: the library
    matches the placed symbols, but the schematic's copy doesn't."""
    root, proj = _stale_project()
    try:
        sch_path = os.path.join(proj, "top.kicad_sch")
        stale_sch = _bytes(sch_path)
        s = portability.Session(proj)
        s.save_manifest(portability.apply_selection(
            s, [r for r in _sym_rows(proj) if r["wanted"]]))
        with open(sch_path, "wb") as fh:     # what KiCad's save did
            fh.write(stale_sch)
        st = {r["ref"]: r["status"] for r in _sym_rows(proj)}
        assert st["R13"] == "update" and st["R55"] == "update", st
        assert st["Q1"] == "local" and st["R1"] == "local", st
        s = portability.Session(proj)
        res = portability.apply_selection(s, [r for r in _sym_rows(proj) if r["wanted"]])
        s.save_manifest(res)
        assert res["symbol_updates"]["fields_changed"] == 0, "the library was already right"
        assert res["symbol_updates"]["schematic_copies"] == 2, res
        sch = sexpr.parse(open(sch_path, encoding="utf-8").read())
        cache = {sy.children[1].value: {n: c.children[2].value
                                        for n, c in portability._fields(sy).items()}
                 for sy in sch.find("lib_symbols").find_all("symbol")}
        want = {"Value": "330R 0603", "MPN": "AF0603FR-07330RL", "LCSC": "C144102"}
        assert cache["proj:R330"] == want and cache["R330_1"] == want, cache
        assert {r["status"] for r in _sym_rows(proj)} == {"local"}
    finally:
        shutil.rmtree(root)


def test_kicad9_tilde_datasheet_becomes_empty():
    """A KiCad 9 library's Datasheet "~" means empty; in the KiCad 10 project
    library "~" would be literal, so it is emptied on the way in."""
    root, proj, _ = _project()
    try:
        one = [r for r in _sym_rows(proj) if r["ref"] == "U1"][0]["source"]
        _write(one, '(kicad_symbol_lib (version 20241209) (generator "test")\n'
               '\t(symbol "U" (property "Datasheet" "~") (property "Value" "U"))\n)\n')
        _run(proj, [r for r in _sym_rows(proj) if r["ref"] == "U1"])
        lib = open(os.path.join(proj, "libs", "proj.kicad_sym"), encoding="utf-8").read()
        assert '(property "Datasheet" "")' in lib and '"~"' not in lib, lib
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
