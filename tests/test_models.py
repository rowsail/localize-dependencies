# 3D models in project footprint libraries, and the flat packages3D/ rule:
# models land directly in packages3D/ (never a subfolder), library footprints'
# models are localized like board models (sharing one file), and local models
# sitting in a packages3D subfolder are moved up.
#
# Run: python tests/test_models.py   (or pytest)

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
    root = tempfile.mkdtemp()
    proj = os.path.join(root, "proj")
    ext = os.path.join(root, "ext", "Parts.3dshapes").replace("\\", "/")
    _write(os.path.join(ext, "R.step"), "step R")
    _write(os.path.join(ext, "R.wrl"), "vrml R")
    _write(os.path.join(proj, "packages3D", "Old.3dshapes", "N.step"), "step N")
    _write(os.path.join(proj, "packages3D", "Old.3dshapes", "N.wrl"), "vrml N")
    _write(os.path.join(proj, "libs", "Mine.pretty", "R.kicad_mod"),
           '(footprint "R"\n\t(model "{}/R.step")\n)\n'.format(ext))
    _write(os.path.join(proj, "fp-lib-table"),
           '(fp_lib_table\n\t(version 7)\n\t(lib (name "Mine")(type "KiCad")'
           '(uri "${KIPRJMOD}/libs/Mine.pretty")(options "")(descr ""))\n)\n')
    _write(os.path.join(proj, "board.kicad_pcb"),
           "(kicad_pcb\n"
           '\t(footprint "Mine:R" (property "Reference" "R1") (model "{}/R.step"))\n'
           '\t(footprint "Mine:N" (property "Reference" "N1")'
           ' (model "${{KIPRJMOD}}/packages3D/Old.3dshapes/N.step"))\n)\n'.format(ext))
    return root, proj


def _models(proj):
    return [r for r in portability.scan_items(proj) if r["type"] == "model"]


def _run(proj, rows):
    s = portability.Session(proj)
    res = portability.apply_selection(s, rows)
    s.save_manifest(res)
    return res


def test_scan_lists_library_models_and_subfolder_models():
    root, proj = _project()
    try:
        st = sorted((r["ref"], r["status"], r["key"][0]) for r in _models(proj))
        assert st == [("Mine:R", "external", "libmodel"), ("N1", "nested", "model"),
                      ("R1", "external", "model")], st
        assert all(r["wanted"] for r in _models(proj))
    finally:
        shutil.rmtree(root)


def test_everything_lands_flat_and_shared():
    root, proj = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        lib = os.path.join(proj, "libs", "Mine.pretty", "R.kicad_mod")
        before = (_bytes(pcb), _bytes(lib))
        res = _run(proj, _models(proj))
        p3 = os.path.join(proj, "packages3D")
        assert sorted(os.listdir(p3)) == ["N.step", "N.wrl", "R.step", "R.wrl"], \
            "flat: no subfolder left"
        board = open(pcb, encoding="utf-8").read()
        assert '"${KIPRJMOD}/packages3D/R.step"' in board
        assert '"${KIPRJMOD}/packages3D/N.step"' in board
        assert '"${KIPRJMOD}/packages3D/R.step"' in open(lib, encoding="utf-8").read(), \
            "the library footprint shares the board's file"
        assert res["library_models"]["remapped"] == 1, res
        assert res["subfolders"]["files_removed"] == 2, res
        assert all(r["status"] == "local" for r in _models(proj))
        r_ = portability.revert(proj)
        assert r_["errors"] == [], r_
        assert (_bytes(pcb), _bytes(lib)) == before
        assert _bytes(os.path.join(p3, "Old.3dshapes", "N.wrl")) == b"vrml N", \
            "revert brings the subfolder copies back"
        assert sorted(os.listdir(p3)) == ["Old.3dshapes"]
    finally:
        shutil.rmtree(root)


def test_subfolder_copy_still_in_use_is_kept():
    root, proj = _project()
    try:
        # a second board reference to the nested model, not selected
        pcb = os.path.join(proj, "board.kicad_pcb")
        with open(pcb, encoding="utf-8") as fh:
            t = fh.read()
        _write(pcb, t.replace("\n)\n", '\n\t(footprint "Mine:N" (property "Reference" "N2")'
                                       ' (model "${KIPRJMOD}/packages3D/Old.3dshapes/N.step"))\n)\n'))
        _run(proj, [r for r in _models(proj) if r["ref"] == "N1"])
        assert os.path.isfile(os.path.join(proj, "packages3D", "Old.3dshapes", "N.step")), \
            "N2 still points at it"
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
