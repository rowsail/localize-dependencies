# Project portability engine (file-level, pure Python, no KiCad or wx imports).
#
# Localizes a KiCad project's external dependencies into project-local libraries
# so the project is self-contained and portable: 3D models, footprints, and
# symbols. Operates directly on the saved project files via the generic S-expr
# parser (sexpr.py), editing only the exact byte spans it targets.
#
# SAFETY: before any file is modified it is backed up once, and the manifest
# (.portability/manifest.json) is updated after every change, across all runs.
# Writes are atomic and existing files are never overwritten by copies.
# revert() restores the pre-localize state byte-for-byte, removes everything
# the runs created, and leaves alone files edited since (unless forced).
#
# License: GPL-3.0-or-later

import filecmp
import glob
import hashlib
import json
import base64
import os
import re
import shutil
import subprocess

import sexpr

# --- layout ------------------------------------------------------------------

STATE_DIRNAME = ".portability"           # holds backups + manifest
MANIFEST_NAME = "manifest.json"
LOCAL_3D_DIRNAME = "packages3D"          # local 3D models (matches KiCad convention)
LOCAL_EMBED_SUBDIR = "embedded"          # packages3D/embedded/ for extracted models
LOCAL_FP_SUFFIX = ".pretty"
TWIN_EXTS = (".step", ".stp", ".wrl")
EMBED_PREFIX = "kicad-embed://"

_MODEL_DIR_VARS = (
    "KISYS3DMOD", "KICAD6_3DMODEL_DIR", "KICAD7_3DMODEL_DIR",
    "KICAD8_3DMODEL_DIR", "KICAD9_3DMODEL_DIR", "KICAD10_3DMODEL_DIR",
)
_INSTALL_GLOBS = (
    r"C:\Program Files\KiCad\*\share\kicad",
    r"C:\Program Files (x86)\KiCad\*\share\kicad",
    "/usr/share/kicad",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
)


# --- path resolution ---------------------------------------------------------

def _version_key(path):
    """Sort key: newest KiCad version first. A plain string sort ranks "9.0"
    and "6.0" above "10.0", which picks an old install's libraries."""
    m = re.search(r"(\d+)\.(\d+)", path)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _install_share():
    hits = []
    for pat in _INSTALL_GLOBS:
        hits.extend(p for p in glob.glob(pat) if os.path.isdir(p))
    hits.sort(key=_version_key, reverse=True)
    return hits[0] if hits else None


def make_resolver(project_dir):
    """Return resolve(raw)->absolute path, expanding ${KIPRJMOD}, KiCad library
    dir vars, and any environment variables. Falls back to the KiCad install
    share dir for unset ${KICAD*_{3DMODEL,FOOTPRINT,SYMBOL}_DIR} (common: the
    plugin runs in a separate process without those set)."""
    share = _install_share()
    env_model = {v: os.environ[v] for v in _MODEL_DIR_VARS
                 if os.environ.get(v) and os.path.isdir(os.environ[v])}
    fallbacks = {}   # var-suffix -> dir
    if share:
        for sub, suffix in (("3dmodels", "_3DMODEL_DIR"),
                            ("footprints", "_FOOTPRINT_DIR"),
                            ("symbols", "_SYMBOL_DIR")):
            d = os.path.join(share, sub)
            if os.path.isdir(d):
                fallbacks[suffix] = d

    def resolve(raw):
        if not raw:
            return raw
        s = re.sub(r"\$\(([^)]+)\)", r"${\1}", raw)   # $(VAR) -> ${VAR}

        def repl(m):
            name = m.group(1)
            if name == "KIPRJMOD":
                return project_dir
            if name in env_model:
                return env_model[name]
            val = os.environ.get(name)
            if val:
                return val
            for suffix, d in fallbacks.items():
                if name.endswith(suffix):
                    return d
            return m.group(0)

        s = re.sub(r"\$\{([^}]+)\}", repl, s)
        return os.path.normpath(s)

    return resolve


# --- library tables ----------------------------------------------------------

def parse_lib_table(path):
    """nickname -> uri from a sym-lib-table / fp-lib-table file (or {} if none)."""
    out = {}
    if not os.path.isfile(path):
        return out
    root = sexpr.parse(_read(path))
    for lib in root.find_all("lib"):
        name = lib.find("name")
        uri = lib.find("uri")
        if name and uri and len(name.children) > 1 and len(uri.children) > 1:
            out[name.children[1].value] = uri.children[1].value
    return out


def _config_dirs():
    base = os.path.expandvars(r"%APPDATA%\kicad") if os.name == "nt" else \
        os.path.expanduser("~/.config/kicad")
    return sorted(glob.glob(os.path.join(base, "*")), key=_version_key,
                  reverse=True) if os.path.isdir(base) else []


def resolve_lib_uri(project_dir, table_name, nickname):
    """Resolve a library nickname to its uri, checking the project table first,
    then the newest global table."""
    proj = parse_lib_table(os.path.join(project_dir, table_name))
    if nickname in proj:
        return proj[nickname]
    for d in _config_dirs():
        g = parse_lib_table(os.path.join(d, table_name))
        if nickname in g:
            return g[nickname]
    return None


def _table_header(kind):
    return "fp_lib_table" if kind == "fp" else "sym_lib_table"


def _lib_entry(nickname, uri):
    return ('\t(lib (name "{}")(type "KiCad")(uri "{}")(options "")'
            '(descr "Localized"))').format(nickname, uri)


def add_lib_entries(session, kind, entries):
    """Add {nickname: uri} entries to the project's fp/sym lib table, creating
    the table file if needed. Skips nicknames already present."""
    if not entries:
        return
    table_name = "fp-lib-table" if kind == "fp" else "sym-lib-table"
    path = os.path.join(session.project_dir, table_name)
    existing = parse_lib_table(path)
    new = {n: u for n, u in entries.items() if n not in existing}
    if not new:
        return
    lines = "\n".join(_lib_entry(n, u) for n, u in sorted(new.items()))

    if os.path.isfile(path):
        text = _read(path)
        root = sexpr.parse(text)
        close = root.end - 1                      # offset of final ')'
        nl = "\r\n" if "\r\n" in text else "\n"   # match the file's line endings
        new_text = text[:close] + lines.replace("\n", nl) + nl + text[close:]
        write_validated(session, path, new_text)
    else:
        new_text = "({}\n\t(version 7)\n{}\n)\n".format(_table_header(kind), lines)
        write_validated(session, path, new_text, is_new=True)


def classify(raw, project_dir):
    """external / standard / local / missing for a raw model path. Embedded
    (kicad-embed://) models are classified by scan_items, which knows which
    embedded files the board carries."""
    if raw.startswith(EMBED_PREFIX):
        return "missing"
    if "${KIPRJMOD}" in raw:
        return "local"
    resolved = make_resolver(project_dir)(raw)
    is_std = bool(re.search(r"\$\{KICAD\d*_3DMODEL_DIR\}", raw)) or (
        _install_share() and _install_share().replace("\\", "/").lower()
        in resolved.replace("\\", "/").lower())
    if not os.path.isfile(resolved):
        return "missing"
    return "standard" if is_std else "external"


# --- backup / manifest / revert ---------------------------------------------

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read(path):
    """Read a KiCad text file exactly: no newline translation, so CRLF files
    are written back as CRLF."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


class Session:
    """Tracks localize runs for safe, exact revert.

    The manifest accumulates across runs: a file is backed up only the first
    time any run touches it, so Revert always returns to the state before the
    first localize. The manifest is rewritten after every change, so a run that
    fails part-way can still be reverted."""

    def __init__(self, project_dir):
        self.project_dir = os.path.abspath(project_dir)
        self.state_dir = os.path.join(self.project_dir, STATE_DIRNAME)
        self._backed = {}      # rel original path -> backup rel path
        self._created_files = []
        self._created_dirs = []
        self._hashes = {}      # rel path -> sha256 after our last write
        self._unverified = []  # backed-up paths Revert cannot vouch for
        self.summary = {}
        man = _load_manifest(self.project_dir)
        if man:
            for e in man.get("backed_up", []):
                self._backed[e["path"]] = e["backup"]
            self._created_files = list(man.get("created_files", []))
            self._created_dirs = list(man.get("created_dirs", []))
            self._hashes = dict(man.get("hashes", {}))
            self._unverified = list(man.get("unverified", []))
            self.summary = man.get("summary", {})

    def _rel(self, path):
        return os.path.relpath(os.path.abspath(path),
                               self.project_dir).replace(os.sep, "/")

    def backup(self, path):
        """Copy `path` into the backup store once, before it is modified."""
        rel = self._rel(path)
        if rel in self._backed and os.path.isfile(path):
            # Backed up by an earlier run. If the file changed since we last
            # wrote it (or that run kept no hash), Revert would silently throw
            # those in-between edits away with this run's: flag it so Revert asks.
            if (rel not in self._unverified
                    and self._hashes.get(rel) != _sha256(path)):
                self._unverified.append(rel)
                self._save()
            return
        if rel in self._created_files or not os.path.isfile(path):
            return
        dest = os.path.join(self.state_dir, "backup", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        self._backed[rel] = "backup/" + rel
        self._save()

    def mkdir(self, path):
        missing = []   # every level makedirs will create, so Revert removes all
        p = os.path.abspath(path)
        while not os.path.isdir(p):
            missing.append(self._rel(p))
            p = os.path.dirname(p)
        if missing:
            os.makedirs(path, exist_ok=True)
            self._created_dirs.extend(missing)
            self._save()

    def wrote_new_file(self, path):
        """Record `path` as created by us. Call BEFORE writing it, so a write
        that dies half-way still gets cleaned up by Revert."""
        rel = self._rel(path)
        if rel not in self._created_files:
            self._created_files.append(rel)
            self._save()

    def written(self, path):
        """Record the content we left in `path`, so Revert can tell whether
        the user changed it afterwards."""
        self._hashes[self._rel(path)] = _sha256(path)
        self._save()

    def _save(self):
        os.makedirs(self.state_dir, exist_ok=True)
        manifest = {
            "backed_up": [{"path": p, "backup": b} for p, b in self._backed.items()],
            "created_files": self._created_files,
            "created_dirs": self._created_dirs,
            "hashes": self._hashes,
            "unverified": self._unverified,
            "summary": self.summary,
        }
        mpath = os.path.join(self.state_dir, MANIFEST_NAME)
        with open(mpath + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        os.replace(mpath + ".tmp", mpath)

    def save_manifest(self, summary):
        self.summary = summary
        self._save()


def _load_manifest(project_dir):
    mpath = os.path.join(project_dir, STATE_DIRNAME, MANIFEST_NAME)
    if not os.path.isfile(mpath):
        return None
    with open(mpath, "r", encoding="utf-8") as fh:
        return json.load(fh)


def has_backup(project_dir):
    return os.path.isfile(os.path.join(project_dir, STATE_DIRNAME, MANIFEST_NAME))


def revert_conflicts(project_dir):
    """Files Revert cannot restore without possibly discarding edits:
    - changed since we last wrote them (edited by the user, or by KiCad
      saving over them);
    - edited between two runs (marked 'unverified' when the later run
      touched them);
    - backed up with no recorded hash (a manifest from a version that kept
      none), so there is no way to tell."""
    man = _load_manifest(os.path.abspath(project_dir)) or {}
    hashes = man.get("hashes", {})
    out = set()
    for rel, sha in hashes.items():
        p = os.path.join(project_dir, rel)
        if os.path.isfile(p) and _sha256(p) != sha:
            out.add(rel)
    for rel in man.get("unverified", []):
        if os.path.isfile(os.path.join(project_dir, rel)):
            out.add(rel)
    for e in man.get("backed_up", []):
        if e["path"] not in hashes and os.path.isfile(os.path.join(project_dir, e["path"])):
            out.add(e["path"])
    return sorted(out)


def revert(project_dir, force=False):
    """Restore backed-up files byte-for-byte and remove created files/dirs.
    Files changed since localize (see revert_conflicts) are left alone and
    reported unless `force`. Returns a summary dict."""
    project_dir = os.path.abspath(project_dir)
    state_dir = os.path.join(project_dir, STATE_DIRNAME)
    man = _load_manifest(project_dir)
    if man is None:
        return {"restored": 0, "removed": 0, "errors": ["No portability backup found."]}

    restored = removed = 0
    errors = []
    skip = set() if force else set(revert_conflicts(project_dir))
    for rel in sorted(skip):
        errors.append("skipped {}: changed since localize".format(rel))

    kept_backed, kept_created = [], []   # still pending after this revert
    for entry in man.get("backed_up", []):
        if entry["path"] in skip:
            kept_backed.append(entry)
            continue
        src = os.path.join(state_dir, entry["backup"])
        dst = os.path.join(project_dir, entry["path"])
        try:
            shutil.copy2(src, dst)
            restored += 1
        except OSError as exc:
            kept_backed.append(entry)
            errors.append("restore {}: {}".format(entry["path"], exc))

    for rel in man.get("created_files", []):
        if rel in skip:
            kept_created.append(rel)
            continue
        p = os.path.join(project_dir, rel)
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
        except OSError as exc:
            kept_created.append(rel)
            errors.append("remove {}: {}".format(rel, exc))

    # deepest dirs first so they are empty when removed
    for rel in sorted(man.get("created_dirs", []), key=len, reverse=True):
        p = os.path.join(project_dir, rel)
        try:
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        except OSError:
            pass

    if not kept_backed and not kept_created:
        shutil.rmtree(state_dir, ignore_errors=True)
    else:
        # keep only what is still pending, so a later (forced) Revert can
        # finish the job without re-flagging files we already restored
        kept = {e["path"] for e in kept_backed} | set(kept_created)
        man["backed_up"] = kept_backed
        man["created_files"] = kept_created
        man["hashes"] = {p: h for p, h in man.get("hashes", {}).items() if p in kept}
        man["unverified"] = [p for p in man.get("unverified", []) if p in kept]
        mpath = os.path.join(state_dir, MANIFEST_NAME)
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump(man, fh, indent=2)

    return {"restored": restored, "removed": removed, "errors": errors}


# --- validation --------------------------------------------------------------

def write_validated(session, path, new_text, is_new=False):
    """Write new_text to path, but only after confirming it re-parses. Backs up
    an existing file first. Raises on parse failure (leaving the original).
    The write is atomic: a crash leaves either the old or the new file."""
    sexpr.parse(new_text)  # will raise SExprError if we produced garbage
    if is_new:
        session.wrote_new_file(path)
    else:
        session.backup(path)
    tmp = path + ".portability-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    os.replace(tmp, path)
    session.written(path)


def _prop(node, name):
    """Value of a (property "name" "value" ...) child, or None."""
    for c in node.children:
        if (c.kind == "list" and c.head() == "property" and len(c.children) >= 3
                and c.children[1].value == name):
            return c.children[2].value
    return None


def _symbol_instances(root):
    """Top-level schematic symbol instances (the ones carrying a lib_id).
    Excludes the definitions cached under (lib_symbols ...)."""
    return [s for s in root.find_all("symbol") if s.find("lib_id")]


def scan_items(project_dir, include_standard=False):
    """One row per symbol, footprint, and 3D model, each with a stable selection
    key. Read-only. A row is a dict: type, ref, name, source, status, key, wanted.

    Keys encode document order so apply() can find the same node again:
      symbol    ('symbol', sch_rel, instance_index)
      footprint ('footprint', pcb_rel, footprint_index)
      model     ('model', pcb_rel, footprint_index, model_index)
    """
    rows = []

    for sch in sorted(glob.glob(os.path.join(project_dir, "*.kicad_sch"))):
        rel = os.path.relpath(sch, project_dir).replace(os.sep, "/")
        root = sexpr.parse(_read(sch))
        for i, sym in enumerate(_symbol_instances(root)):
            lid = sym.find("lib_id")
            if not (lid and lid.children[1:]):
                continue
            libid = lid.children[1].value
            nick = libid.split(":", 1)[0] if ":" in libid else ""
            uri = resolve_lib_uri(project_dir, "sym-lib-table", nick) if nick else None
            cat = classify_lib(uri, project_dir) if nick else "local"
            rows.append({"type": "symbol", "ref": _prop(sym, "Reference") or "?",
                         "name": libid, "source": uri or "", "status": cat,
                         "key": ("symbol", rel, i),
                         "wanted": _wanted(cat, include_standard)})

    for pcb in sorted(glob.glob(os.path.join(project_dir, "*.kicad_pcb"))):
        rel = os.path.relpath(pcb, project_dir).replace(os.sep, "/")
        root = sexpr.parse(_read(pcb))
        board_embeds = _embedded_names(root)
        for fi, fp in enumerate(root.find_all("footprint")):
            if not (fp.children[1:] and fp.children[1].kind == "atom"):
                continue
            fpid = fp.children[1].value
            ref = _prop(fp, "Reference") or "?"
            nick = fpid.split(":", 1)[0] if ":" in fpid else ""
            uri = resolve_lib_uri(project_dir, "fp-lib-table", nick) if nick else None
            cat = classify_lib(uri, project_dir) if nick else "local"
            rows.append({"type": "footprint", "ref": ref, "name": fpid,
                         "source": uri or "", "status": cat,
                         "key": ("footprint", rel, fi),
                         "wanted": _wanted(cat, include_standard)})
            for mi, model in enumerate(fp.find_all("model")):
                if not (model.children[1:] and model.children[1].kind == "atom"):
                    continue
                raw = model.children[1].value
                if raw.startswith(EMBED_PREFIX):
                    ename = raw[len(EMBED_PREFIX):]
                    mcat = "embedded" if (ename in _embedded_names(fp)
                                          or ename in board_embeds) else "missing"
                else:
                    mcat = classify(raw, project_dir)
                rows.append({"type": "model", "ref": ref,
                             "name": os.path.basename(raw.replace("\\", "/")),
                             "source": raw, "status": mcat,
                             "key": ("model", rel, fi, mi),
                             "wanted": _wanted(mcat, include_standard)})
    return rows


# --- 3D model localization ---------------------------------------------------

def _model_sources(resolved):
    """The model file plus its same-stem twins (.step/.wrl/...), one per name."""
    stem = os.path.splitext(os.path.basename(resolved))[0]
    src_dir = os.path.dirname(resolved)
    todo = [resolved]
    for ext in TWIN_EXTS:
        for e in (ext, ext.upper()):
            t = os.path.join(src_dir, stem + e)
            if os.path.isfile(t) and t != resolved:
                todo.append(t)
    out, seen = [], set()
    for src in todo:
        if os.path.basename(src).lower() not in seen:
            seen.add(os.path.basename(src).lower())
            out.append(src)
    return out


def _place_model(session, resolved):
    """Copy a model (and its twins) into packages3D/<*.3dshapes>/ and return
    (kiprjmod_path, files_copied).

    Never overwrites: a destination that already holds identical content is
    reused, and one holding different content (the user's own file, or a
    same-named model from another library) gets a _1, _2, ... suffix instead."""
    project_dir = session.project_dir
    subdir = os.path.basename(os.path.dirname(resolved))
    parts = [LOCAL_3D_DIRNAME] + ([subdir] if subdir else [])
    dest_dir = os.path.join(project_dir, *parts)
    stem, ext = os.path.splitext(os.path.basename(resolved))
    srcs = _model_sources(resolved)

    def dst_for(src, s):
        return os.path.join(dest_dir, s + os.path.splitext(src)[1])

    def fits(s):
        return all(not os.path.exists(dst_for(src, s))
                   or filecmp.cmp(src, dst_for(src, s), shallow=False)
                   for src in srcs)

    n, new_stem = 0, stem
    while not fits(new_stem):
        n += 1
        new_stem = "{}_{}".format(stem, n)

    session.mkdir(dest_dir)
    copied = 0
    for src in srcs:
        dst = dst_for(src, new_stem)
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            continue
        if os.path.exists(dst):   # identical content, reuse it
            continue
        session.wrote_new_file(dst)
        shutil.copy2(src, dst)
        session.written(dst)
        copied += 1
    return "${KIPRJMOD}/" + "/".join(parts + [new_stem + ext]), copied


def localize_models(session, rows):
    """Copy the selected 3D models into packages3D/ and remap their (model "...")
    paths. `rows` are the selected scan_items() rows of type 'model'. File-level
    path; the live IPC path lives in the action layer."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)
    keys = {r["key"] for r in rows if r["type"] == "model"}
    if not keys:
        return {"files_copied": 0, "remapped": 0, "unique_sources": 0}

    by_pcb = {}
    for k in keys:
        by_pcb.setdefault(k[1], set()).add(k)

    files_copied = remapped = 0
    placed = {}   # normcased source -> new ${KIPRJMOD} path
    for rel, kset in by_pcb.items():
        pcb = os.path.join(project_dir, rel)
        text = _read(pcb)
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        for fi, fp in enumerate(root.find_all("footprint")):
            for mi, model in enumerate(fp.find_all("model")):
                if ("model", rel, fi, mi) not in kset or not model.children[1:]:
                    continue
                atom = model.children[1]
                resolved = resolve(atom.value)
                src_key = os.path.normcase(os.path.abspath(resolved))
                if src_key not in placed:
                    placed[src_key], n = _place_model(session, resolved)
                    files_copied += n
                editor.replace_atom(atom, placed[src_key], force_quote=True)
                remapped += 1
        write_validated(session, pcb, editor.result())

    return {"files_copied": files_copied, "remapped": remapped,
            "unique_sources": len(placed)}


# --- embedded 3D models (kicad-embed://) -------------------------------------
#
# KiCad 9+ can embed a model in the board: the footprint says
# (model "kicad-embed://NAME.step") and the file itself sits in an
# (embedded_files (file (name "NAME.step") (type model) (data |...|) ...)) block,
# at board level or inside the footprint. The data is base64 of a zstd stream.

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _embedded_files(node):
    """{name: (file node)} for the (embedded_files ...) child of `node`."""
    out = {}
    if node is None:
        return out
    block = node.find("embedded_files")
    if block is None:
        return out
    for f in block.find_all("file"):
        name = f.find("name")
        if name is not None and name.children[1:]:
            out[name.children[1].value] = f
    return out


def _embedded_names(node):
    return set(_embedded_files(node))


def _zstd_decompress(data):
    try:
        import io
        import zstandard
        with zstandard.ZstdDecompressor().stream_reader(
                io.BytesIO(data), read_across_frames=True) as rd:
            return rd.read()
    except ImportError:
        pass
    try:
        from compression import zstd   # Python 3.14+
        return zstd.decompress(data)
    except ImportError:
        pass
    exe = shutil.which("zstd")
    if exe:
        return subprocess.run([exe, "-d", "-c", "-q"], input=data,
                              capture_output=True, check=True).stdout
    raise RuntimeError("Cannot decompress embedded files: install the Python "
                       "'zstandard' package or the 'zstd' command-line tool.")


def embedded_file_bytes(file_node):
    """Decoded content of one (file ...) entry of an embedded_files block."""
    data = file_node.find("data")
    if data is None or not data.children[1:]:
        raise ValueError("embedded file has no data")
    # The base64 text is written as |...| split over several lines, so the
    # parser sees it as several atoms; join them back up.
    text = "".join(a.raw for a in data.children[1:] if a.kind == "atom")
    payload = base64.b64decode(text.strip("|"))
    return _zstd_decompress(payload) if payload[:4] == ZSTD_MAGIC else payload


def _place_embedded(session, names_contents):
    """Write an embedded model and its embedded twins, [(name, bytes), ...] with
    the referenced model first, into packages3D/embedded/. Returns
    ({name: kiprjmod path}, files_written).

    Never overwrites, as _place_model: identical content is reused, and a
    destination holding different content gets a _1, _2, ... stem suffix (the
    same suffix for the model and its twins, so they stay paired)."""
    parts = [LOCAL_3D_DIRNAME, LOCAL_EMBED_SUBDIR]
    dest_dir = os.path.join(session.project_dir, *parts)
    stem = os.path.splitext(os.path.basename(names_contents[0][0].replace("\\", "/")))[0]

    def dst_for(name, s):
        return os.path.join(dest_dir, s + os.path.splitext(name)[1])

    def same(path, content):
        with open(path, "rb") as fh:
            return fh.read() == content

    def fits(s):
        return all(not os.path.exists(dst_for(n, s)) or same(dst_for(n, s), c)
                   for n, c in names_contents)

    n, new_stem = 0, stem
    while not fits(new_stem):
        n += 1
        new_stem = "{}_{}".format(stem, n)

    session.mkdir(dest_dir)
    paths, written = {}, 0
    for name, content in names_contents:
        dst = dst_for(name, new_stem)
        paths[name] = "${KIPRJMOD}/" + "/".join(parts + [os.path.basename(dst)])
        if os.path.exists(dst):   # identical content, reuse it
            continue
        session.wrote_new_file(dst)
        tmp = dst + ".portability-tmp"
        with open(tmp, "wb") as fh:
            fh.write(content)
        os.replace(tmp, dst)
        session.written(dst)
        written += 1
    return paths, written


def _delete_span(editor, text, node):
    """Remove a list node and the whitespace before it on its own line."""
    start = node.start
    while start > 0 and text[start - 1] in " \t":
        start -= 1
    if start > 0 and text[start - 1] == "\n":
        start -= 1
        if start > 0 and text[start - 1] == "\r":
            start -= 1
    editor.replace_span(start, node.end, "")


def localize_embedded_models(session, rows):
    """Extract the selected embedded (kicad-embed://) models, with their
    embedded twins, into packages3D/embedded/, remap their (model ...) paths
    there, and drop the embedded copies nothing references any more."""
    project_dir = session.project_dir
    keys = {r["key"] for r in rows if r["type"] == "model"}
    if not keys:
        return {"files_extracted": 0, "remapped": 0, "embeds_removed": 0}

    by_pcb = {}
    for k in keys:
        by_pcb.setdefault(k[1], set()).add(k)

    extracted = remapped = removed = 0
    for rel, kset in by_pcb.items():
        pcb = os.path.join(project_dir, rel)
        text = _read(pcb)
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        board_files = _embedded_files(root)
        done = {}           # (owner, name) -> new path
        still_used = set()  # (owner, name) referenced by a model left embedded
        for fi, fp in enumerate(root.find_all("footprint")):
            fp_files = _embedded_files(fp)
            for mi, model in enumerate(fp.find_all("model")):
                if not model.children[1:]:
                    continue
                atom = model.children[1]
                if not atom.value.startswith(EMBED_PREFIX):
                    continue
                name = atom.value[len(EMBED_PREFIX):]
                owner = ("fp", fi) if name in fp_files else ("board", 0)
                files = fp_files if owner[0] == "fp" else board_files
                if ("model", rel, fi, mi) not in kset or name not in files:
                    still_used.add((owner, name))
                    continue
                if (owner, name) not in done:
                    stem = os.path.splitext(name)[0]
                    group = [name] + [o for o in files
                                      if o != name and os.path.splitext(o)[0] == stem
                                      and os.path.splitext(o)[1].lower() in TWIN_EXTS
                                      and (owner, o) not in done]
                    paths, n = _place_embedded(
                        session, [(g, embedded_file_bytes(files[g])) for g in group])
                    for g in group:
                        done[(owner, g)] = paths[g]
                    extracted += n
                editor.replace_atom(atom, done[(owner, name)], force_quote=True)
                remapped += 1

        # Drop the embedded copies that no model references any more.
        def prune(owner, node, files):
            nonlocal removed
            gone = [n for n in files if (owner, n) in done and (owner, n) not in still_used]
            if not gone:
                return
            if len(gone) == len(files):
                _delete_span(editor, text, node.find("embedded_files"))
            else:
                for n in gone:
                    _delete_span(editor, text, files[n])
            removed += len(gone)

        for fi, fp in enumerate(root.find_all("footprint")):
            prune(("fp", fi), fp, _embedded_files(fp))
        prune(("board", 0), root, board_files)
        write_validated(session, pcb, editor.result())

    return {"files_extracted": extracted, "remapped": remapped,
            "embeds_removed": removed}


def _embed_refs(text):
    """Every name referenced as kicad-embed://NAME anywhere in a file's text."""
    return set(re.findall(r'kicad-embed://((?:[^"\\]|\\.)*)"', text))


def _unused_in(text, root):
    """[(block node, {name: file node} to drop, all files of the block)] for
    the embedded 3D models nothing in `text` references. Only (type model)
    entries are considered: fonts, datasheets and drawing sheets can be
    referenced from outside the board file."""
    refs = _embed_refs(text)
    out = []
    for owner in [root] + root.find_all("footprint"):
        files = _embedded_files(owner)
        drop = {}
        for name, f in files.items():
            ty = f.find("type")
            if (ty is not None and ty.children[1:] and ty.children[1].value == "model"
                    and name not in refs):
                drop[name] = f
        if drop:
            out.append((owner.find("embedded_files"), drop, files))
    return out


def scan_unused_embedded(project_dir):
    """Read-only: [{'pcb', 'name', 'bytes'}] for every embedded 3D model in the
    project's boards that no kicad-embed:// reference uses. 'bytes' is the
    board-file text the entry takes, i.e. what removing it saves."""
    rows = []
    for pcb in sorted(glob.glob(os.path.join(project_dir, "*.kicad_pcb"))):
        rel = os.path.relpath(pcb, project_dir).replace(os.sep, "/")
        text = _read(pcb)
        for _blk, drop, _files in _unused_in(text, sexpr.parse(text)):
            for name, f in drop.items():
                rows.append({"pcb": rel, "name": name, "bytes": f.end - f.start})
    return rows


def remove_unused_embedded(session):
    """Delete every embedded 3D model nothing references (see _unused_in),
    backing each board up first. Returns {'removed', 'bytes', 'boards'}."""
    removed = nbytes = boards = 0
    for pcb in sorted(glob.glob(os.path.join(session.project_dir, "*.kicad_pcb"))):
        text = _read(pcb)
        found = _unused_in(text, sexpr.parse(text))
        if not found:
            continue
        editor = sexpr.Editor(text)
        for block, drop, files in found:
            if len(drop) == len(files):
                _delete_span(editor, text, block)
            else:
                for f in drop.values():
                    _delete_span(editor, text, f)
            removed += len(drop)
            nbytes += sum(f.end - f.start for f in drop.values())
        write_validated(session, pcb, editor.result())
        boards += 1
    return {"removed": removed, "bytes": nbytes, "boards": boards}


# --- footprint / symbol library localization ---------------------------------

LOCAL_LIB_DIRNAME = "libs"
LOCAL_SUFFIX = "_local"


def classify_lib(uri, project_dir):
    """external / standard / local / missing for a library uri."""
    if not uri:
        return "missing"
    if "${KIPRJMOD}" in uri:
        return "local"
    resolved = make_resolver(project_dir)(uri)
    share = _install_share()
    is_std = bool(re.search(r"\$\{KICAD\d*_(FOOTPRINT|SYMBOL)_DIR\}", uri)) or (
        share and share.replace("\\", "/").lower() in resolved.replace("\\", "/").lower())
    if not os.path.exists(resolved):
        return "missing"
    return "standard" if is_std else "external"


def _wanted(cat, include_standard):
    return cat == "external" or (cat == "standard" and include_standard)


def localize_footprints(session, rows):
    """Copy the selected footprints into ${KIPRJMOD}/libs/<nick>_local.pretty,
    register those libraries in fp-lib-table, and remap the selected footprint
    IDs. Per-item: only the checked footprints move, and both the original and
    the _local nickname stay valid, so unselected footprints are untouched."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)
    keys = {r["key"] for r in rows if r["type"] == "footprint"}
    if not keys:
        return {"libraries": 0, "files_copied": 0, "remapped": 0}

    by_pcb = {}
    for k in keys:
        by_pcb.setdefault(k[1], set()).add(k)

    entries = {}
    src_dir_cache = {}
    copied = remapped = 0
    for rel, kset in by_pcb.items():
        pcb = os.path.join(project_dir, rel)
        text = _read(pcb)
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        for fi, fp in enumerate(root.find_all("footprint")):
            if ("footprint", rel, fi) not in kset:
                continue
            atom = fp.children[1]
            if ":" not in atom.value:
                continue
            nick, name = atom.value.split(":", 1)
            if nick not in src_dir_cache:
                src_dir_cache[nick] = resolve(
                    resolve_lib_uri(project_dir, "fp-lib-table", nick) or "")
            local_nick = nick + LOCAL_SUFFIX
            local_dir = os.path.join(project_dir, LOCAL_LIB_DIRNAME,
                                     local_nick + LOCAL_FP_SUFFIX)
            session.mkdir(local_dir)
            src_file = os.path.join(src_dir_cache[nick], name + ".kicad_mod")
            dst_file = os.path.join(local_dir, name + ".kicad_mod")
            if os.path.isfile(src_file) and not os.path.isfile(dst_file):
                session.wrote_new_file(dst_file)
                shutil.copy2(src_file, dst_file)
                session.written(dst_file)
                copied += 1
            editor.replace_atom(atom, "{}:{}".format(local_nick, name), force_quote=True)
            entries[local_nick] = "${{KIPRJMOD}}/{}/{}{}".format(
                LOCAL_LIB_DIRNAME, local_nick, LOCAL_FP_SUFFIX)
            remapped += 1
        write_validated(session, pcb, editor.result())

    add_lib_entries(session, "fp", entries)
    return {"libraries": len(entries), "files_copied": copied, "remapped": remapped}


def localize_symbols(session, rows):
    """Localize the libraries of the selected symbols. `rows` are the selected
    scan_items() rows of type 'symbol'.

    Symbols localize per library, not per instance: selecting any symbol of a
    library copies the whole .kicad_sym (so derived symbols keep their 'extends'
    parents) and remaps every instance of that library plus its lib_symbols
    cache entries together. Splitting one library across two nicknames would
    leave the embedded symbol cache inconsistent, so we keep it whole."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)

    nicks = set()
    for r in rows:
        if r["type"] == "symbol" and ":" in r["name"]:
            nicks.add(r["name"].split(":", 1)[0])
    if not nicks:
        return {"libraries": 0, "files_copied": 0, "remapped": 0}

    src_file = {}   # nick -> resolved source .kicad_sym
    for nick in nicks:
        uri = resolve_lib_uri(project_dir, "sym-lib-table", nick)
        if uri:
            src_file[nick] = resolve(uri)
    if not src_file:
        return {"libraries": 0, "files_copied": 0, "remapped": 0}

    entries = {}
    copied = 0
    for nick, sfile in src_file.items():
        local_nick = nick + LOCAL_SUFFIX
        local_lib = os.path.join(project_dir, LOCAL_LIB_DIRNAME, local_nick + ".kicad_sym")
        session.mkdir(os.path.dirname(local_lib))
        if os.path.isfile(sfile) and not os.path.isfile(local_lib):
            session.wrote_new_file(local_lib)
            shutil.copy2(sfile, local_lib)
            session.written(local_lib)
            copied += 1
        entries[local_nick] = "${{KIPRJMOD}}/{}/{}.kicad_sym".format(
            LOCAL_LIB_DIRNAME, local_nick)

    remapped = 0
    for sch in glob.glob(os.path.join(project_dir, "*.kicad_sch")):
        text = _read(sch)
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        touched = False

        def remap(atom):
            nonlocal remapped, touched
            if atom.kind != "atom" or ":" not in atom.value:
                return
            nick, name = atom.value.split(":", 1)
            if nick in src_file:
                editor.replace_atom(atom, "{}:{}".format(nick + LOCAL_SUFFIX, name),
                                    force_quote=True)
                remapped += 1
                touched = True

        for lst in root.iter_lists():
            head = lst.head()
            if head == "lib_id" and lst.children[1:]:
                remap(lst.children[1])
            elif head == "lib_symbols":
                # direct child (symbol "nick:name" ...) entries only
                for child in lst.children:
                    if child.kind == "list" and child.head() == "symbol" and child.children[1:]:
                        remap(child.children[1])
        if touched:
            write_validated(session, sch, editor.result())

    add_lib_entries(session, "sym", entries)
    return {"libraries": len(entries), "files_copied": copied, "remapped": remapped}


# --- dispatcher --------------------------------------------------------------

def apply_selection(session, rows):
    """Localize the given selection of scan_items() rows. Symbols and footprints
    are file-level; models are handled here too (file-level) unless the caller
    localizes them live over IPC instead. Returns {scope: summary}."""
    result = {}
    syms = [r for r in rows if r["type"] == "symbol"]
    fps = [r for r in rows if r["type"] == "footprint"]
    models = [r for r in rows if r["type"] == "model" and r["status"] != "embedded"]
    embedded = [r for r in rows if r["type"] == "model" and r["status"] == "embedded"]
    if syms:
        result["symbols"] = localize_symbols(session, syms)
    if fps:
        result["footprints"] = localize_footprints(session, fps)
    if models:
        result["models"] = localize_models(session, models)
    if embedded:
        result["embedded"] = localize_embedded_models(session, embedded)
    return result
