"""Check portability.rebuild_footprint against KiCad itself on a real board.

    PYTHONPATH=<dir with KiCad's pcbnew.py> python tools/verify_rebuild.py board.kicad_pcb [--flip left_right]

For every footprint on the board, KiCad's own library save (what "Export
Footprints to Library" uses) writes the reference .kicad_mod, and the
plugin's rebuild_footprint writes its own from the board text; kicad-cli
"fp upgrade" then re-saves ours so both are in KiCad's canonical form, and
the two are compared (with instance_fields=True: KiCad's export keeps the
instance's fields; the plugin by default writes a clean library footprint
instead). UUIDs are ignored (KiCad's save assigns new ones).
"(locked yes)" and component classes are ignored too, and embedded files are
compared by name on the raw rebuild: kicad-cli's re-save of a library drops
all three (rebuild_footprint keeps them when instance_fields=True). Files are
compared structurally, so line wrapping doesn't matter. The reference undoes
rotation and side, then moves the footprint to the origin, so the children
KiCad keeps in board coordinates (zones, points, dimensions) come out
local: KiCad's own export gets those wrong on rotated footprints. With
--flip left_right the reference is the top-bottom one with every child
turned 180 degrees about the origin (and the 3D model with it), which is
what rebuild_footprint produces for that setting. Two spellings are treated as equal: text angles that differ by 360 (KiCad
writes -90 or 270 depending on the path), and "*.Cu" on a through-hole pad
versus the board's own copper layers listed out.

Needs KiCad's pcbnew Python module (same KiCad version as kicad-cli).
Exits non-zero if any footprint differs.
"""
import concurrent.futures as cf
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins"))
import portability  # noqa: E402
import sexpr  # noqa: E402


def main():
    import pcbnew
    board_path = os.path.abspath(sys.argv[1])
    flip = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--flip" else "top_bottom"
    work = tempfile.mkdtemp(prefix="verify_rebuild-")
    cli = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli") or "kicad-cli"
    try:
        # KiCad's reference output, one library per reference designator
        plug = pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP)
        board = pcbnew.LoadBoard(board_path)
        for i, fp in enumerate(board.GetFootprints()):
            lib = os.path.join(work, "kicad", "%04d.pretty" % i)
            os.makedirs(lib)
            cp = pcbnew.FOOTPRINT(fp)
            cp.SetReference("REF**")
            # Undo rotation and side here, then move zones to the origin:
            # KiCad's own export moves zones first and rotates them about the
            # old position afterwards, which misplaces the zones of rotated
            # footprints. FootprintSave then has nothing left to change.
            pos = cp.GetPosition()
            cp.SetOrientation(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
            back = cp.GetLayer() != pcbnew.F_Cu
            if back:
                cp.Flip(pos, pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
            cp.SetPosition(pcbnew.VECTOR2I(0, 0))   # zones, points, dimensions follow
            if back and flip == "left_right":
                # the 180 degree turn of a left-right flip, baked into every
                # child (FootprintSave would undo a turn of the footprint)
                half, origin = pcbnew.EDA_ANGLE(180, pcbnew.DEGREES_T), pcbnew.VECTOR2I(0, 0)
                items = (list(cp.Pads()) + list(cp.GraphicalItems()) + list(cp.Zones())
                         + list(cp.GetFields()))
                for item in items:
                    item.Rotate(origin, half)
                models = []
                for m in cp.Models():
                    n = pcbnew.FP_3DMODEL()
                    n.m_Filename, n.m_Show, n.m_Opacity = m.m_Filename, m.m_Show, m.m_Opacity
                    n.m_Scale.x, n.m_Scale.y, n.m_Scale.z = m.m_Scale.x, m.m_Scale.y, m.m_Scale.z
                    # (+ 0.0 so that a zero offset stays 0, not -0)
                    n.m_Offset.x, n.m_Offset.y = -m.m_Offset.x + 0.0, -m.m_Offset.y + 0.0
                    n.m_Offset.z = m.m_Offset.z
                    z = (m.m_Rotation.z + 180 + 180) % 360 - 180
                    n.m_Rotation.x, n.m_Rotation.y = m.m_Rotation.x, m.m_Rotation.y
                    n.m_Rotation.z = 180.0 if z == -180 else z
                    models.append(n)
                cp.Models().clear()
                for n in models:
                    cp.Add3DModel(n)
            plug.FootprintSave(lib, cp)

        # ours, from the board text
        text = open(board_path, encoding="utf-8").read()
        root = sexpr.parse(text)
        ver = root.find("version").children[1].value
        embeds = portability._embedded_files(root)
        jobs = []
        for i, fp in enumerate(root.find_all("footprint")):
            ref = "%04d" % i
            label = portability._prop(fp, "Reference") or "?"
            name = fp.children[1].value.split(":", 1)[-1]
            lib = os.path.join(work, "ours", ref + ".pretty")
            os.makedirs(lib)
            with open(os.path.join(lib, name + ".kicad_mod"), "w", encoding="utf-8") as fh:
                fh.write(portability.rebuild_footprint(text, fp, ver, embeds,
                                                       instance_fields=True, flip=flip))
            at = fp.find("at")
            jobs.append((ref, label, name, fp.find("layer").children[1].value,
                         at.children[3].value if len(at.children) > 3 else "0"))

        os.makedirs(os.path.join(work, "canon"))

        def canon(job):
            out = os.path.join(work, "canon", job[0] + ".pretty")
            p = subprocess.run([cli, "fp", "upgrade", "--force", "-o", out,
                                os.path.join(work, "ours", job[0] + ".pretty")],
                               capture_output=True, text=True)
            return job, p.returncode

        def embedded_names(path):
            return sorted(re.findall(r'\(file\s+\(name "([^"]+)"', open(path, encoding="utf-8").read()))

        def lines(path):
            """The file as one line per list, compared structurally: UUIDs,
            "(locked yes)" and embedded files dropped (see the module notes),
            angles mod 360, through-hole copper layers as "*.Cu", a text box's two
            corners as an unordered rectangle, arc midpoints to 1 um, -0 as 0."""
            out = []

            def emit(node, depth):
                h = node.head()
                if h in ("uuid", "embedded_files") or (
                        depth == 1 and h in ("locked", "component_classes")):
                    return
                atoms = ["0" if a.value == "-0" else a.value
                         for a in node.children if a.kind == "atom"]
                if h == "at" and len(atoms) == 4:
                    atoms[3] = "%g" % (float(atoms[3]) % 360)
                if h == "mid" and len(atoms) == 3:
                    # KiCad's Rotate() recomputes arc midpoints from centre and
                    # angles (nanometre rounding); 1 um is far below any meaning
                    atoms[1:] = ["%.3f" % float(x) for x in atoms[1:]]
                if h == "layers":
                    items = atoms[1:]
                    if "F.Cu" in items and "B.Cu" in items:
                        items = ["*.Cu"] + [x for x in items if not x.endswith(".Cu")]
                    atoms = ["layers"] + sorted(items)
                out.append("  " * depth + " ".join(atoms))
                kids = [c for c in node.children if c.kind == "list"]
                if h == "fp_text_box":
                    # corner order is immaterial: KiCad places the text from
                    # GetCornersInSequence(), which sorts the corners
                    se = {c.head(): c for c in kids if c.head() in ("start", "end")}
                    if len(se) == 2:
                        xs = sorted(float(se[k].children[1].value) for k in se)
                        ys = sorted(float(se[k].children[2].value) for k in se)
                        out.append("  " * (depth + 1) + "box %g %g %g %g" % (xs[0], ys[0], xs[1], ys[1]))
                        kids = [c for c in kids if c.head() not in ("start", "end")]
                for c in kids:
                    emit(c, depth + 1)
            emit(sexpr.parse(open(path, encoding="utf-8").read()), 0)
            return out

        bad = 0
        with cf.ThreadPoolExecutor(8) as ex:
            for (ref, label, name, layer, angle), rc in ex.map(canon, jobs):
                ours = os.path.join(work, "canon", ref + ".pretty", name + ".kicad_mod")
                theirs = os.path.join(work, "kicad", ref + ".pretty", name + ".kicad_mod")
                if rc or not os.path.exists(ours):
                    bad += 1
                    print("FAIL {} ({} {}): KiCad could not load the rebuilt footprint".format(label, layer, angle))
                    continue
                a, b = lines(theirs), lines(ours)
                raw = os.path.join(work, "ours", ref + ".pretty", name + ".kicad_mod")
                if embedded_names(theirs) != embedded_names(raw):
                    b = b + ["embedded files: {}".format(embedded_names(raw))]
                    a = a + ["embedded files: {}".format(embedded_names(theirs))]
                if a != b:
                    bad += 1
                    print("DIFF {} {} ({} {}):".format(label, name, layer, angle))
                    print("\n".join(list(difflib.unified_diff(a, b, "kicad", "rebuilt", n=1,
                                                              lineterm=""))[:20]))
        print("{} of {} footprints identical to KiCad's library save ({} flip)".format(
            len(jobs) - bad, len(jobs), flip.replace("_", "-")))
        sys.exit(1 if bad else 0)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
