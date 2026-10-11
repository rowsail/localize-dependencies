# The dialog's side of the data-safety fixes: what a user is told when a run
# fails part-way, and what each answer to the "changed since localize"
# question does to their files. Message boxes are scripted, so nothing is
# shown on screen.
#
# Needs wx and kipy, e.g. the plugin's own KiCad venv:
#   "$LOCALAPPDATA/KiCad/10.0/python-environments/com.github.jOaSbA.localize-dependencies/Scripts/python" tests/test_dialog.py
#
# License: GPL-3.0-or-later

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins"))
sys.path.insert(0, HERE)

import wx  # noqa: E402

import portability  # noqa: E402
from localize_dependencies_action import LocalizeDialog  # noqa: E402
from test_safety import _bytes, _project, _write  # noqa: E402

# One app for the whole run, or the second dialog dies with PyNoAppError.
_APP = wx.App()


def _dialog(proj, answers):
    """Dialog on `proj` whose message boxes return `answers` in order and
    record their text in dlg.shown."""
    dlg = LocalizeDialog(proj, portability.scan_items(proj))
    dlg.shown = []
    queue = list(answers)

    def msg(text, style):
        dlg.shown.append(text)
        return queue.pop(0) if queue else wx.OK
    dlg._msg = msg
    return dlg


def _localize(proj):
    dlg = _dialog(proj, [wx.YES])
    try:
        dlg.on_run(None)
    finally:
        dlg.Destroy()


def test_failed_run_tells_the_user_to_revert():
    root, proj = _project()
    real = portability._place_model

    def boom(*a, **k):
        raise RuntimeError("disk full")
    portability._place_model = boom
    try:
        dlg = _dialog(proj, [wx.YES])
        dlg.on_run(None)
        err = dlg.shown[-1]
        assert "disk full" in err and "Revert" in err, err
        assert "no changes kept" not in err
        assert dlg.btn_revert.IsEnabled(), "Revert must be offered after a failed run"
        dlg.Destroy()
    finally:
        portability._place_model = real
        shutil.rmtree(root)


def _edited_project():
    root, proj = _project()
    pcb = os.path.join(proj, "board.kicad_pcb")
    original = _bytes(pcb)
    _localize(proj)
    edited = _bytes(pcb).replace(b"R1", b"R9")
    _write(pcb, edited, "wb")
    return root, proj, pcb, original, edited


def test_revert_no_keeps_the_edited_file():
    root, proj, pcb, _, edited = _edited_project()
    try:
        dlg = _dialog(proj, [wx.YES, wx.NO])
        dlg.on_revert(None)
        assert "board.kicad_pcb" in dlg.shown[1], dlg.shown
        assert _bytes(pcb) == edited
        dlg.Destroy()
    finally:
        shutil.rmtree(root)


def test_revert_yes_restores_the_edited_file():
    root, proj, pcb, original, _ = _edited_project()
    try:
        dlg = _dialog(proj, [wx.YES, wx.YES])
        dlg.on_revert(None)
        assert _bytes(pcb) == original
        assert not portability.has_backup(proj)
        dlg.Destroy()
    finally:
        shutil.rmtree(root)


def test_revert_cancel_touches_nothing():
    root, proj, pcb, _, edited = _edited_project()
    try:
        dlg = _dialog(proj, [wx.YES, wx.CANCEL])
        dlg.on_revert(None)
        assert _bytes(pcb) == edited
        assert os.path.isdir(os.path.join(proj, "packages3D")), "cancel removed files"
        assert portability.has_backup(proj)
        dlg.Destroy()
    finally:
        shutil.rmtree(root)


def test_remove_unused_embedded_button():
    import test_embedded
    root, proj = test_embedded._project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        original = _bytes(pcb)
        dlg = _dialog(proj, [wx.NO])
        assert dlg.btn_unused.IsEnabled()
        assert dlg.btn_unused.GetLabel().startswith("Remove unused embedded (2, ")
        dlg.on_remove_unused(None)              # answered No: nothing changes
        assert _bytes(pcb) == original
        assert "Orphan.step" in dlg.shown[0] and "M.wrl" in dlg.shown[0]
        dlg.Destroy()

        dlg = _dialog(proj, [wx.YES])
        dlg.on_remove_unused(None)
        assert portability.scan_unused_embedded(proj) == []
        assert not dlg.btn_unused.IsEnabled(), "nothing left to remove"
        assert dlg.btn_revert.IsEnabled()
        assert "Removed 2 unused embedded" in dlg.shown[-1]
        dlg.Destroy()
        assert portability.revert(proj)["errors"] == []
        assert _bytes(pcb) == original
    finally:
        shutil.rmtree(root)


def test_use_step_button():
    import test_step
    root, proj, _ = test_step._project()
    try:
        dlg = _dialog(proj, [wx.YES])
        assert dlg.btn_step.IsEnabled() and dlg.btn_step.GetLabel().endswith("(4)")
        dlg.on_use_step(None)
        assert "R.wrl -> R.step" in dlg.shown[0]
        assert portability.scan_vrml_models(proj) == []
        assert not dlg.btn_step.IsEnabled()
        assert "4 model reference(s) in 2 file(s)" in dlg.shown[-1]
        dlg.Destroy()
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
