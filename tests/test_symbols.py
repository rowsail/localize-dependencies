# Symbol library localization: single-file .kicad_sym libraries, KiCad 10
# folder libraries (one .kicad_sym per symbol, *.kicad_symdir), and libraries
# whose source is missing (left alone, never remapped to a copy that doesn't
# exist).
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


def test_folder_library_is_copied_as_a_folder():
    root, proj, sdir = _project()
    try:
        _run(proj, [r for r in _sym_rows(proj) if r["ref"] == "R1"])
        local = os.path.join(proj, "libs", "Dir_local.kicad_symdir")
        assert sorted(os.listdir(local)) == ["C.kicad_sym", "R.kicad_sym"], \
            "every library file, and only library files"
        assert _bytes(os.path.join(local, "R.kicad_sym")) == \
            _bytes(os.path.join(sdir, "R.kicad_sym"))
        table = open(os.path.join(proj, "sym-lib-table"), encoding="utf-8").read()
        assert '(name "Dir_local")(type "KiCad")(uri "${KIPRJMOD}/libs/Dir_local.kicad_symdir")' in table
        sch = open(os.path.join(proj, "top.kicad_sch"), encoding="utf-8").read()
        assert '(lib_id "Dir_local:R")' in sch and '(lib_id "Dir_local:C")' in sch, \
            "the whole library moves together"
        assert '(symbol "Dir_local:C")' in sch, "lib_symbols cache follows"
        assert '(lib_id "One:U")' in sch, "unselected library untouched"
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert not os.path.exists(os.path.join(proj, "libs"))
    finally:
        shutil.rmtree(root)


def test_single_file_library_still_works():
    root, proj, _ = _project()
    try:
        _run(proj, [r for r in _sym_rows(proj) if r["ref"] == "U1"])
        assert os.path.isfile(os.path.join(proj, "libs", "One_local.kicad_sym"))
        table = open(os.path.join(proj, "sym-lib-table"), encoding="utf-8").read()
        assert '(uri "${KIPRJMOD}/libs/One_local.kicad_sym")' in table
        sch = open(os.path.join(proj, "top.kicad_sch"), encoding="utf-8").read()
        assert '(lib_id "One_local:U")' in sch
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
