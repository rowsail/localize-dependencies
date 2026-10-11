# "Use STEP instead of VRML": .wrl model references that have a .step/.stp
# twin beside them are pointed at it, in the boards and the project-local
# footprint libraries; .wrl-only models and other libraries are left alone.
#
# Run: python tests/test_step.py   (or pytest)

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
    m = os.path.join(proj, "packages3D")
    _write(os.path.join(m, "R.wrl"), "vrml R")
    _write(os.path.join(m, "R.step"), "step R")
    _write(os.path.join(m, "Q.wrl"), "vrml Q, no STEP twin")
    _write(os.path.join(m, "U.wrl"), "vrml U")
    _write(os.path.join(m, "U.STP"), "step U")
    ext = os.path.join(root, "ext.pretty")
    _write(os.path.join(ext, "X.kicad_mod"),
           '(footprint "X" (model "${KIPRJMOD}/packages3D/R.wrl"))\n')
    _write(os.path.join(proj, "libs", "Mine.pretty", "R.kicad_mod"),
           '(footprint "R"\n\t(model "${KIPRJMOD}/packages3D/R.wrl")\n)\n')
    _write(os.path.join(proj, "fp-lib-table"),
           '(fp_lib_table\n\t(version 7)\n'
           '\t(lib (name "Mine")(type "KiCad")(uri "${KIPRJMOD}/libs/Mine.pretty")(options "")(descr ""))\n'
           '\t(lib (name "Ext")(type "KiCad")(uri "%s")(options "")(descr ""))\n)\n'
           % ext.replace("\\", "/"))
    _write(os.path.join(proj, "board.kicad_pcb"),
           "(kicad_pcb\n"
           '\t(footprint "Mine:R" (property "Reference" "R1")'
           ' (model "${KIPRJMOD}/packages3D/R.wrl"))\n'
           '\t(footprint "Mine:Q" (property "Reference" "Q1")'
           ' (model "${KIPRJMOD}/packages3D/Q.wrl"))\n'
           '\t(footprint "Mine:U" (property "Reference" "U1")'
           ' (model "${KIPRJMOD}/packages3D/U.wrl"))\n'
           '\t(footprint "Mine:E" (property "Reference" "E1") (model "kicad-embed://E.wrl"))\n'
           '\t(embedded_files (file (name "E.wrl") (type model) (data |AA|) (checksum "0"))\n'
           '\t\t(file (name "E.step") (type model) (data |AA|) (checksum "0")))\n'
           ")\n")
    return root, proj, ext


def test_scan_finds_only_vrml_with_a_step_twin():
    root, proj, _ = _project()
    try:
        rows = portability.scan_vrml_models(proj)
        got = sorted((r["file"], r["step"].rsplit("/", 1)[-1]) for r in rows)
        assert got == [("board.kicad_pcb", "E.step"), ("board.kicad_pcb", "R.step"),
                       ("board.kicad_pcb", "U.STP"), ("libs/Mine.pretty/R.kicad_mod", "R.step")], got
    finally:
        shutil.rmtree(root)


def test_use_step_then_revert():
    root, proj, ext = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        lib = os.path.join(proj, "libs", "Mine.pretty", "R.kicad_mod")
        before = (_bytes(pcb), _bytes(lib), _bytes(os.path.join(ext, "X.kicad_mod")))
        s = portability.Session(proj)
        res = portability.use_step_models(s)
        s.save_manifest({"step_models": res})
        assert res == {"references": 4, "files": 2}, res
        board = open(pcb, encoding="utf-8").read()
        assert "packages3D/R.step" in board and "packages3D/U.STP" in board
        assert "packages3D/Q.wrl" in board, "no STEP twin: left alone"
        assert '"kicad-embed://E.step"' in board
        assert "R.step" in open(lib, encoding="utf-8").read()
        assert _bytes(os.path.join(ext, "X.kicad_mod")) == before[2], "not a project library"
        assert portability.scan_vrml_models(proj) == []
        assert os.path.exists(os.path.join(proj, "packages3D", "R.wrl")), \
            "the .wrl files are not deleted"
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert (_bytes(pcb), _bytes(lib)) == before[:2]
    finally:
        shutil.rmtree(root)


def test_kicad_path_variables_resolve():
    import json
    root, proj, _ = _project()
    d = tempfile.mkdtemp()
    saved = portability._config_dirs
    try:
        lib = os.path.join(root, "global3d")
        _write(os.path.join(lib, "G.wrl"), "vrml G")
        _write(os.path.join(lib, "G.step"), "step G")
        with open(os.path.join(d, "kicad_common.json"), "w") as fh:
            json.dump({"environment": {"vars": {"MY3D": lib}}}, fh)
        portability._config_dirs = lambda: [d]
        os.environ.pop("MY3D", None)
        pcb = os.path.join(proj, "board.kicad_pcb")
        with open(pcb, encoding="utf-8") as fh:
            t = fh.read()
        _write(pcb, t.replace('"Mine:Q" (property "Reference" "Q1") (model '
                              '"${KIPRJMOD}/packages3D/Q.wrl")',
                              '"Mine:Q" (property "Reference" "Q1") (model "${MY3D}/G.wrl")'))
        steps = [r["step"] for r in portability.scan_vrml_models(proj)]
        assert "${MY3D}/G.step" in steps, "a KiCad path variable (Configure Paths) resolves"
    finally:
        portability._config_dirs = saved
        shutil.rmtree(d)
        shutil.rmtree(root)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
