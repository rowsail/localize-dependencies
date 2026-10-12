# With several KiCad versions installed side by side, the plugin must use the
# newest one's libraries and library tables. A string sort ranks "9.0" and
# "6.0" above "10.0", which silently copied KiCad 6 footprints into a
# KiCad 10 project.
#
# Run: python tests/test_versions.py   (or pytest)
#
# License: GPL-3.0-or-later

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))
import portability  # noqa: E402

VERSIONS = ("6.0", "9.0", "10.0", "8.0")


def test_install_share_picks_newest():
    root = tempfile.mkdtemp()
    real = portability._INSTALL_GLOBS
    try:
        for v in VERSIONS:
            os.makedirs(os.path.join(root, v, "share", "kicad"))
        portability._INSTALL_GLOBS = (os.path.join(root, "*", "share", "kicad"),)
        assert portability._install_share() == os.path.join(root, "10.0", "share", "kicad")
    finally:
        portability._INSTALL_GLOBS = real
        shutil.rmtree(root)


def test_resolver_uses_the_install_that_has_the_library():
    """A distribution's KiCad 9 in /usr and a KiCad 10 in /usr/local: the
    KiCad 10 table names folder libraries (power.kicad_symdir) that only the
    KiCad 10 install has, whichever install is listed first."""
    root = tempfile.mkdtemp()
    real = portability._INSTALL_GLOBS
    real_vars = portability.kicad_path_vars
    saved = {k: os.environ.pop(k) for k in list(os.environ)
             if k.startswith("KICAD") and k.endswith("_DIR")}
    try:
        old = os.path.join(root, "usr", "share", "kicad")
        new = os.path.join(root, "usr", "local", "share", "kicad")
        os.makedirs(os.path.join(old, "symbols", "power.kicad_sym"))
        os.makedirs(os.path.join(new, "symbols", "power.kicad_symdir"))
        portability._INSTALL_GLOBS = (old, new)
        portability.kicad_path_vars = lambda: {}
        resolve = portability.make_resolver(root)
        assert resolve("${KICAD10_SYMBOL_DIR}/power.kicad_symdir") == \
            os.path.join(new, "symbols", "power.kicad_symdir")
        assert resolve("${KICAD9_SYMBOL_DIR}/power.kicad_sym") == \
            os.path.join(old, "symbols", "power.kicad_sym")
        assert portability.classify_lib("${KICAD10_SYMBOL_DIR}/power.kicad_symdir",
                                        root) == "standard"
    finally:
        portability._INSTALL_GLOBS = real
        portability.kicad_path_vars = real_vars
        os.environ.update(saved)
        shutil.rmtree(root)


def test_config_dirs_newest_first():
    root = tempfile.mkdtemp()
    var = "APPDATA" if os.name == "nt" else "HOME"
    base = os.path.join(root, "kicad") if os.name == "nt" else \
        os.path.join(root, ".config", "kicad")
    real = os.environ.get(var)
    try:
        for v in VERSIONS:
            os.makedirs(os.path.join(base, v))
        os.environ[var] = root
        got = [os.path.basename(d) for d in portability._config_dirs()]
        assert got == ["10.0", "9.0", "8.0", "6.0"], got
    finally:
        if real is None:
            del os.environ[var]
        else:
            os.environ[var] = real
        shutil.rmtree(root)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
