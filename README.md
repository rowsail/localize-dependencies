# Localize Dependencies

A KiCad 10 action plugin that makes a project self-contained. It copies the
external symbols, footprints, and 3D models a project uses into project-local
libraries and updates every reference to point at them. After that the
schematic, board, 3D view, and STEP export still work on any machine, even if
the original global or third-party libraries aren't installed.

You pick what to localize (symbols, footprints, 3D models, or any mix), run it,
and reload the project. Every run is backed up first, so you can revert.

![The Localize Dependencies dialog](docs/dialog.png)

## What makes it different

- Symbols, footprints, and 3D models, not only one of the three.
- It uses the IPC API, so it runs on KiCad 10 and will keep running on KiCad 11,
  where the old SWIG bindings are gone. SWIG-based plugins stop working there.
- Revert is exact. Each file is backed up before it's touched, and revert copies
  those backups straight back, so the project returns to precisely how it was.
- You can localize just the parts you want.
- Tick "include standard KiCad libraries" and it also pulls in the stock libs,
  which cuts the project loose from the global libraries completely.

## Why

KiCad only stores references to library symbols, footprints, and 3D models, not
the items themselves. Move the project to another PC, or send it to a colleague
or a fab house, and anything that lived in a global or third-party library breaks.
The 3D models go missing, footprints stop resolving, symbols get rescued. This
plugin pulls those items into the project so that stops happening.

## How it works

The plugin talks to KiCad over the IPC API (`kicad-python` / `kipy`) just long
enough to find the open project, so the removal of the SWIG bindings in KiCad 11
doesn't affect it. The editing happens on the project files directly, with a
small S-expression parser that only rewrites the references it needs to and
leaves the rest of each file byte-for-byte alone. Tokens it doesn't recognise
(something new in a future KiCad version, say) pass through unchanged.

Before it changes a file it copies the original into `.portability/`. Revert
reads that back and deletes anything the run created.

## Requirements

- KiCad 10.0 or newer, with the IPC API server enabled under
  *Preferences > Plugins*.
- `kicad-python` and `wxPython`, which KiCad installs for you on first run.

## Install

### From the Plugin and Content Manager

*Tools > Plugin and Content Manager > Plugins*, find Localize Dependencies,
install it, and restart the PCB editor.

### Manually

1. Download the latest `localize-dependencies-x.y.z.zip` from the
   [Releases](https://github.com/jOaSbA/localize-dependencies/releases) page.
2. *Tools > Plugin and Content Manager > Install from File...* and pick the zip.
   You can also just unzip the `plugins/` contents into
   `Documents/KiCad/10.0/plugins/localize_dependencies/`.
3. Enable the IPC API server, then *Tools > External Plugins > Refresh Plugins*.

## Use

1. Save the project first (`Ctrl+S`).
2. Run Localize Dependencies and tick the items you want. By default it checks
   only the external and third-party dependencies. Tick "Also include standard
   KiCad libraries" if you want it fully independent of the global libraries.
3. Click Localize. External symbols and footprints land in `libs/<name>_local.*`
   (a KiCad 10 folder symbol library is copied as a folder,
   `libs/<name>_local.kicad_symdir`),
   3D models in `packages3D/`, the `sym-lib-table` and `fp-lib-table` get the new
   entries, and the references are updated, including the schematic symbols'
   Footprint fields, so *Update PCB from Schematic* keeps the local footprints.
4. Reload the project so KiCad reads the new files: close the Schematic Editor
   and PCB Editor **without saving**, then reopen the project (double-click the
   schematic/board in the project manager, or use File > Open Recent). KiCad has
   no single "reload" command, so this close-and-reopen is the way. Don't save
   from KiCad before reopening, or you'll write the old paths back over the new
   files.
5. To undo, run the plugin again and hit Revert.

### Footprints whose library is gone

A board file holds a complete copy of every footprint it places. When a
footprint's library (or just its `.kicad_mod`) can't be found, the row shows
*missing: rebuild from board* and is ticked: Localize rebuilds the library
footprint from the board's copy, the way KiCad's *Export Footprints to
Library* does (rotation undone, flipped back to the front, nets cleared,
reference `REF**`), into `libs/<name>_local.pretty`; if the library was
already a project library, the file is put back where it belongs and nothing
is remapped. The rebuilt footprint is a clean library footprint: Value is its
name and part-number fields (LCSC, MPN, ...) are left out, so one placed
part's numbers can't leak into every other part using that footprint. The
board's 3D model paths (and any embedded model) come along. Back-side parts are
flipped back the way KiCad is set to flip (*Preferences > PCB Editor >
Editing Options*, "flip left/right"); with left/right, the result is turned
180° as a whole, 3D model included, which recovers the footprint a left/right
flip was made from (KiCad's own export loses that turn).

### Embedded 3D models

KiCad 9 and later can embed a 3D model in the board file itself
(`kicad-embed://NAME.step`). Those rows show the status *embedded*. They are
never ticked by default, because the board already carries them, but you can
tick them (or click *Embedded 3D models*) to turn them into ordinary files:
Localize writes each one, with any embedded `.wrl`/`.step` twin, to
`packages3D/`, points the footprint at
`${KIPRJMOD}/packages3D/NAME`, and removes the embedded copy once no
footprint uses it. Embedded data is zstd-compressed; the plugin uses the
`zstandard` package (installed with it) or, failing that, the `zstd` tool.

![Summary after a localize run](docs/result.png)

## Safety

Nothing changes until you click Localize, and whatever it changes is copied into
`.portability/` first. Revert puts every modified file back as it was before the
first Localize and removes the local copies, however many times you ran it, and
also after a run that failed part-way. Copies never overwrite an existing file:
a 3D model that clashes with one already in `packages3D/` gets a `_1` suffix.
If you edited a file after localizing, Revert asks before throwing that edit
away. Since it works on files on disk, get in the habit of saving before you run
it and reloading after, and keep important projects under version control.

## Testing

Each test file runs on its own with `python tests/<file>.py`:

- `test_safety.py` and `test_versions.py`: the engine, pure Python.
- `test_embedded.py`: embedded 3D models. Needs `zstandard` or the `zstd` tool.
- `test_symbols.py`: symbol libraries, single-file and folder (`.kicad_symdir`).
- `test_footprints.py`: footprint localization updates the schematic's
  Footprint fields too, including symbols shared by reused sheets, and
  footprints are rebuilt from the board when their library is gone.
- `tools/verify_rebuild.py <board>`: checks the rebuild against KiCad's own
  library save, footprint by footprint (`--flip left_right` for that setting).
  Needs KiCad's `pcbnew` Python module.
  `tools/make_feature_board.py` writes a board with the harder cases (text
  boxes, footprint dimensions, net ties, both sides, 30° rotation) to run it on.
- `test_dialog.py`: the dialog's prompts. Needs wx and kipy, for example the
  plugin's own KiCad venv.
- `test_kicad_projects.py`: localizes KiCad's demo projects, then checks with
  `kicad-cli` that DRC, ERC and the STEP export give the same results with no
  global libraries configured, and that Revert restores every file exactly.
  Needs a KiCad install; set `KICAD_CLI` to test a specific one.

KiCad runs the installed copy of the plugin, not this checkout. Run
`python tools/install_dev.py` to copy `plugins/` there before trying a change
in KiCad. CI runs all of the above on every push.

## Tools

`tools/onelib.py` collects the symbols a schematic actually uses into one
project library, `libs/<project>.kicad_symdir` (a KiCad 10 folder library, one
file per symbol), and points the schematic at it:

```
python tools/onelib.py <project_dir>                        # plan only
python tools/onelib.py <project_dir> --apply                # do it
python tools/onelib.py <project_dir> --apply --remove-old   # and delete the merged local libraries
```

Symbols come from whichever library `sym-lib-table` names (or, if none has
them, the schematic's own cached copy). Two different symbols with one name
are kept apart (`<name>_<library>`). File names are made portable: plain
ASCII that Windows and macOS accept (`100kΩ 0805` becomes `100kohm 0805`), and
names that would then differ only in case use `milli` / `MEG` (`100mohm` →
`100milliohm`, `100Mohm` → `100MEGohm`). Only names change; Value fields don't.
Close KiCad and commit the project first. `kicad-cli` writes the library.

## Building the package

Run `python build.py`. It produces `dist/localize-dependencies-<version>.zip` in
the layout the Plugin and Content Manager wants and writes the archive's SHA-256
and sizes into `metadata.json`.

## License

[GPL-3.0-or-later](LICENSE).
