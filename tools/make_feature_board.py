"""Write a board that exercises the footprint features verify_rebuild.py has
to get right and the demo boards don't all have: text boxes, footprint
dimensions, net-tie pad groups, text on a layer with no side, on both sides
at 0/90/180/-90 and a non-right angle (30 degrees).

    PYTHONPATH=<dir with KiCad's pcbnew.py> python tools/make_feature_board.py out.kicad_pcb
    PYTHONPATH=... python tools/verify_rebuild.py out.kicad_pcb
"""
import sys

import pcbnew


def main(path):
    mm = pcbnew.FromMM
    board = pcbnew.BOARD()
    n = 0
    for side in ("F", "B"):
        for angle in (0, 90, 180, -90, 30):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetFPID(pcbnew.LIB_ID("Synth", "Feature_%s%d" % (side, angle)))
            fp.SetReference("X%d" % n)
            fp.SetValue("feat")
            n += 1
            for i, (x, y) in enumerate(((-2, 0), (2, 0))):
                pad = pcbnew.PAD(fp)
                pad.SetNumber(str(i + 1))
                pad.SetShape(pcbnew.PAD_SHAPE_RECT)
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pad.SMDMask())
                pad.SetSize(pcbnew.VECTOR2I(mm(1), mm(1.5)))
                pad.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
                fp.Add(pad)
            fp.AddNetTiePadGroup("1,2")
            box = pcbnew.PCB_TEXTBOX(fp)
            box.SetLayer(pcbnew.F_SilkS)
            box.SetStart(pcbnew.VECTOR2I(mm(-1.5), mm(1.5)))
            box.SetEnd(pcbnew.VECTOR2I(mm(1.5), mm(3)))
            box.SetText("BOX")
            fp.Add(box)
            dim = pcbnew.PCB_DIM_ALIGNED(fp, pcbnew.PCB_DIM_ALIGNED_T)
            dim.SetLayer(pcbnew.F_Fab)
            dim.SetStart(pcbnew.VECTOR2I(mm(-2), mm(-2)))
            dim.SetEnd(pcbnew.VECTOR2I(mm(2), mm(-2)))
            dim.SetHeight(mm(-1))
            fp.Add(dim)
            note = pcbnew.PCB_TEXT(fp)
            note.SetLayer(pcbnew.Dwgs_User)
            note.SetText("note")
            note.SetPosition(pcbnew.VECTOR2I(mm(0), mm(4)))
            fp.Add(note)
            line = pcbnew.PCB_SHAPE(fp, pcbnew.SHAPE_T_SEGMENT)
            line.SetLayer(pcbnew.F_SilkS)
            line.SetStart(pcbnew.VECTOR2I(mm(-3), mm(-1)))
            line.SetEnd(pcbnew.VECTOR2I(mm(3), mm(-1)))
            fp.Add(line)
            board.Add(fp)
            fp.SetPosition(pcbnew.VECTOR2I(mm(20 + 15 * (n % 5)), mm(40 if side == "B" else 20)))
            fp.SetOrientationDegrees(angle)
            if side == "B":
                fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    board.Save(path)
    print("wrote", n, "footprints to", path)


if __name__ == "__main__":
    main(sys.argv[1])
