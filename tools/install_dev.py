#!/usr/bin/env python3
"""Copy plugins/ over the copy KiCad actually runs.

    python tools/install_dev.py [kicad-version]    (default 10.0)

KiCad loads the plugin from its 3rdparty folder, never from this checkout, so
until this runs, trying a change in KiCad tests the old code. Restart KiCad (or
reload plugins) afterwards.
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDENT = "com_github_jOaSbA_localize-dependencies"


def kicad_user_dir():
    if os.name == "nt" or sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Documents", "KiCad")
    return os.path.expanduser("~/.local/share/kicad")


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "10.0"
    dest = os.path.join(kicad_user_dir(), version, "3rdparty", "plugins", IDENT)
    if not os.path.isdir(os.path.dirname(dest)):
        sys.exit("no KiCad {} plugin folder at {}".format(version, os.path.dirname(dest)))
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(os.path.join(HERE, "plugins"), dest,
                    ignore=shutil.ignore_patterns("__pycache__"))
    print("installed to " + dest)


if __name__ == "__main__":
    main()
