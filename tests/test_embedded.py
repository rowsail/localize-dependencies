# Embedded 3D models (kicad-embed://): scan, extract into packages3D/,
# remap, drop the embedded copies nothing uses, and revert exactly. The board
# is synthetic but its embedded_files block has the real KiCad shape: base64
# of a zstd stream, written as |...| over several indented lines.
#
# Run: python tests/test_embedded.py   (or pytest)
# Needs the 'zstandard' package or the 'zstd' tool to build the test data.

import base64
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins"))
sys.path.insert(0, HERE)

import portability  # noqa: E402
import sexpr  # noqa: E402
from test_safety import _bytes, _write  # noqa: E402

STEP_M = b"ISO-10303-21;\nmodel M\n" * 40
WRL_M = b"#VRML V2.0 utf8\nmodel M\n" * 40
STEP_F = b"ISO-10303-21;\nfootprint-level F\n" * 40
STEP_ORPHAN = b"ISO-10303-21;\nnobody uses this\n" * 40


def _zstd(data):
    try:
        import zstandard
        return zstandard.ZstdCompressor().compress(data)
    except ImportError:
        return subprocess.run(["zstd", "-c", "-q"], input=data,
                              capture_output=True, check=True).stdout


def _file_entry(name, content, indent, type_="model"):
    b64 = base64.b64encode(_zstd(content)).decode()
    lines = [b64[i:i + 76] for i in range(0, len(b64), 76)]
    data = ("\n" + indent + "\t\t").join(lines)
    return ('{i}(file\n{i}\t(name "{n}")\n{i}\t(type {t})\n'
            '{i}\t(data |{d}|)\n{i}\t(checksum "0")\n{i})\n').format(
                i=indent, n=name, t=type_, d=data)


def _project(nl="\n"):
    """Temp project whose board embeds M.step (+ its M.wrl twin), used by R1
    and R2; an unreferenced Orphan.step; an unreferenced datasheet; and a
    footprint-level F.step inside U1. R3 points at an embed that isn't there."""
    root = tempfile.mkdtemp()
    proj = os.path.join(root, "proj")
    fp_embed = ("\t\t(embedded_files\n" + _file_entry("F.step", STEP_F, "\t\t\t") +
                "\t\t)\n")
    pcb = ("(kicad_pcb\n"
           '\t(footprint "Lib:R" (property "Reference" "R1") (model "kicad-embed://M.step"))\n'
           '\t(footprint "Lib:R" (property "Reference" "R2") (model "kicad-embed://M.step"))\n'
           '\t(footprint "Lib:R" (property "Reference" "R3") (model "kicad-embed://Nope.step"))\n'
           '\t(footprint "Lib:U" (property "Reference" "U1")\n'
           '\t\t(model "kicad-embed://F.step")\n' + fp_embed + "\t)\n"
           "\t(embedded_files\n" +
           _file_entry("M.step", STEP_M, "\t\t") +
           _file_entry("M.wrl", WRL_M, "\t\t") +
           _file_entry("Orphan.step", STEP_ORPHAN, "\t\t") +
           _file_entry("sheet.pdf", b"%PDF-1.4", "\t\t", type_="datasheet") +
           "\t)\n)\n").replace("\n", nl)
    _write(os.path.join(proj, "board.kicad_pcb"), pcb)
    return root, proj


def _rows(proj, ref=None):
    return [r for r in portability.scan_items(proj)
            if r["type"] == "model" and r["status"] == "embedded"
            and (ref is None or r["ref"] == ref)]


def _run(proj, rows):
    s = portability.Session(proj)
    s.save_manifest(portability.apply_selection(s, rows))


def _emb(proj, name):
    return os.path.join(proj, "packages3D", name)


def _board(proj):
    with open(os.path.join(proj, "board.kicad_pcb"), encoding="utf-8", newline="") as fh:
        return fh.read()


def test_scan_marks_embedded_and_missing():
    root, proj = _project()
    try:
        st = {r["ref"]: r["status"] for r in portability.scan_items(proj)
              if r["type"] == "model"}
        assert st == {"R1": "embedded", "R2": "embedded", "R3": "missing",
                      "U1": "embedded"}, st
        assert not any(r["wanted"] for r in _rows(proj)), "never ticked by default"
    finally:
        shutil.rmtree(root)


def test_extract_all_then_revert():
    root, proj = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        original = _bytes(pcb)
        _run(proj, _rows(proj))
        assert _bytes(_emb(proj, "M.step")) == STEP_M
        assert _bytes(_emb(proj, "M.wrl")) == WRL_M, "twin extracted with its model"
        assert _bytes(_emb(proj, "F.step")) == STEP_F, "footprint-level embed"
        text = _board(proj)
        assert text.count("${KIPRJMOD}/packages3D/M.step") == 2
        assert "${KIPRJMOD}/packages3D/F.step" in text
        files = portability._embedded_files(sexpr.parse(text))
        assert set(files) == {"Orphan.step", "sheet.pdf"}, \
            "only the extracted entries are dropped"
        assert "(embedded_files" in text and text.count("(embedded_files") == 1, \
            "the emptied footprint-level block goes"
        assert not os.path.exists(_emb(proj, "Orphan.step"))
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert _bytes(pcb) == original
        assert not os.path.exists(os.path.join(proj, "packages3D"))
        assert not os.path.exists(os.path.join(proj, ".portability"))
    finally:
        shutil.rmtree(root)


def test_shared_embed_stays_while_still_used():
    root, proj = _project()
    try:
        _run(proj, _rows(proj, "R1"))
        text = _board(proj)
        assert "${KIPRJMOD}/packages3D/M.step" in text
        assert 'kicad-embed://M.step' in text, "R2 still uses the embedded copy"
        assert "M.step" in portability._embedded_files(sexpr.parse(text))
        _run(proj, _rows(proj, "R2"))
        assert "M.step" not in portability._embedded_files(sexpr.parse(_board(proj)))
    finally:
        shutil.rmtree(root)


def test_extract_never_overwrites():
    root, proj = _project()
    try:
        _write(_emb(proj, "M.step"), b"the user's own file", "wb")
        _run(proj, _rows(proj, "R1"))
        assert _bytes(_emb(proj, "M.step")) == b"the user's own file"
        assert _bytes(_emb(proj, "M_1.step")) == STEP_M
        assert _bytes(_emb(proj, "M_1.wrl")) == WRL_M, "twin keeps the same suffix"
        assert "${KIPRJMOD}/packages3D/M_1.step" in _board(proj)
        portability.revert(proj)
        assert _bytes(_emb(proj, "M.step")) == b"the user's own file"
        assert not os.path.exists(_emb(proj, "M_1.step"))
    finally:
        shutil.rmtree(root)


def test_crlf_board_survives_extraction():
    root, proj = _project(nl="\r\n")
    try:
        _run(proj, _rows(proj))
        text = _board(proj)
        assert "\n" not in text.replace("\r\n", ""), "no bare LF introduced"
        sexpr.parse(text)
    finally:
        shutil.rmtree(root)


def test_remove_unused_keeps_used_and_non_models():
    root, proj = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        original = _bytes(pcb)
        unused = portability.scan_unused_embedded(proj)
        assert [u["name"] for u in unused] == ["M.wrl", "Orphan.step"], unused
        s = portability.Session(proj)
        res = portability.remove_unused_embedded(s)
        s.save_manifest({"unused_embedded": res})
        assert res["removed"] == 2 and res["boards"] == 1, res
        assert res["bytes"] == len(original) - len(_bytes(pcb)) - 2 * 3, \
            "bytes = the entries' text (plus the 2 newline+indent each)"
        text = _board(proj)
        root_node = sexpr.parse(text)
        assert set(portability._embedded_files(root_node)) == {"M.step", "sheet.pdf"}, \
            "used model and the datasheet stay"
        assert portability.scan_unused_embedded(proj) == []
        assert {r["ref"] for r in _rows(proj)} == {"R1", "R2", "U1"}, "still resolvable"
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert _bytes(pcb) == original
    finally:
        shutil.rmtree(root)


def test_remove_unused_after_extract_then_revert_both():
    root, proj = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        original = _bytes(pcb)
        _run(proj, _rows(proj))
        s = portability.Session(proj)
        s.save_manifest({"unused_embedded": portability.remove_unused_embedded(s)})
        files = portability._embedded_files(sexpr.parse(_board(proj)))
        assert set(files) == {"sheet.pdf"}, files
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert _bytes(pcb) == original, "revert undoes both runs"
        assert not os.path.exists(os.path.join(proj, "packages3D"))
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
