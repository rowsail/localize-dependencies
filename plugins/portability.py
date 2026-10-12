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
import math
import os
import re
import shutil
import subprocess

import sexpr

# --- layout ------------------------------------------------------------------

STATE_DIRNAME = ".portability"           # holds backups + manifest
MANIFEST_NAME = "manifest.json"
LOCAL_3D_DIRNAME = "packages3D"          # local 3D models (matches KiCad convention)
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
    "/usr/local/share/kicad",
    "/usr/share/kicad",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
)


# --- path resolution ---------------------------------------------------------

def _version_key(path):
    """Sort key: newest KiCad version first. A plain string sort ranks "9.0"
    and "6.0" above "10.0", which picks an old install's libraries."""
    m = re.search(r"(\d+)\.(\d+)", path)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _install_shares():
    """Every KiCad install's share dir, newest version first (an unversioned
    path such as /usr/local/share/kicad keeps its _INSTALL_GLOBS order). A
    machine can have more than one, e.g. a distribution's KiCad 9 in /usr and
    a KiCad 10 built into /usr/local."""
    hits = []
    for pat in _INSTALL_GLOBS:
        hits.extend(p for p in glob.glob(pat) if os.path.isdir(p) and p not in hits)
    hits.sort(key=_version_key, reverse=True)   # stable: keeps glob order on ties
    return hits


def _install_share():
    shares = _install_shares()
    return shares[0] if shares else None


def _in_install(path):
    flat = path.replace("\\", "/").lower()
    return any(s.replace("\\", "/").lower() in flat for s in _install_shares())


def kicad_path_vars():
    """The user's path variables from KiCad's settings (Preferences >
    Configure Paths: kicad_common.json environment.vars), newest config
    first; {} if none. The plugin may run without them in its environment."""
    for d in _config_dirs():
        path = os.path.join(d, "kicad_common.json")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    found = json.load(fh).get("environment", {}).get("vars") or {}
                return {k: v for k, v in found.items() if isinstance(v, str) and v}
            except (OSError, ValueError):
                return {}
    return {}


def make_resolver(project_dir):
    """Return resolve(raw)->absolute path, expanding ${KIPRJMOD}, KiCad library
    dir vars, and any environment variables, then the user's KiCad path
    variables (kicad_path_vars). Falls back to the KiCad install share dir for
    unset ${KICAD*_{3DMODEL,FOOTPRINT,SYMBOL}_DIR} (common: the plugin runs in
    a separate process without those set). With several installs, the first
    in which the resolved path exists wins (a KiCad 10 table names
    .kicad_symdir libraries that a KiCad 9 install doesn't have)."""
    kicad_vars = kicad_path_vars()
    env_model = {v: os.environ[v] for v in _MODEL_DIR_VARS
                 if os.environ.get(v) and os.path.isdir(os.environ[v])}
    shares = _install_shares()
    subdirs = (("3dmodels", "_3DMODEL_DIR"), ("footprints", "_FOOTPRINT_DIR"),
               ("symbols", "_SYMBOL_DIR"))

    def resolve_with(s, share):
        def repl(m):
            name = m.group(1)
            if name == "KIPRJMOD":
                return project_dir
            if name in env_model:
                return env_model[name]
            val = os.environ.get(name) or kicad_vars.get(name)
            if val:
                return val
            if share:
                for sub, suffix in subdirs:
                    if name.endswith(suffix) and os.path.isdir(os.path.join(share, sub)):
                        return os.path.join(share, sub)
            return m.group(0)

        return os.path.normpath(re.sub(r"\$\{([^}]+)\}", repl, s))

    def resolve(raw):
        if not raw:
            return raw
        s = re.sub(r"\$\(([^)]+)\)", r"${\1}", raw)   # $(VAR) -> ${VAR}
        candidates = [resolve_with(s, share) for share in shares] or [resolve_with(s, None)]
        for c in candidates:
            if os.path.exists(c):
                return c
        return candidates[0]

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
        flat = raw.replace("\\", "/")
        if re.match(r"\$\{KIPRJMOD\}/" + LOCAL_3D_DIRNAME + r"/.+/[^/]+$", flat):
            # local, but in a packages3D subfolder: Localize moves it up
            return "nested" if os.path.isfile(make_resolver(project_dir)(raw)) else "missing"
        return "local"
    resolved = make_resolver(project_dir)(raw)
    is_std = bool(re.search(r"\$\{KICAD\d*_3DMODEL_DIR\}", raw)) or _in_install(resolved)
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
            os.makedirs(os.path.dirname(dst), exist_ok=True)   # a removed folder
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

    local_nicks = {nick for nick, uri in parse_lib_table(
        os.path.join(project_dir, "sym-lib-table")).items() if "${KIPRJMOD}" in uri}
    stale = stale_symbols(project_dir, local_nicks)
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
            if cat == "local" and libid in stale:
                cat = "update"      # project library out of step with the parts
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
            if nick and not os.path.isfile(_footprint_file(project_dir, uri, fpid)):
                cat = "rebuild"     # library or file gone: rebuild from the board copy
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

    # 3D models named inside the project's own footprint libraries: a
    # library footprint pointing outside the project brings that path back
    # whenever a footprint is (re)placed from it.
    for nick, path in local_footprint_files(project_dir):
        rel = os.path.relpath(path, project_dir).replace(os.sep, "/")
        try:
            root = sexpr.parse(_read(path))
        except sexpr.SExprError:
            continue
        label = "{}:{}".format(nick, os.path.splitext(os.path.basename(path))[0])
        for mi, model in enumerate(root.find_all("model")):
            if not (model.children[1:] and model.children[1].kind == "atom"):
                continue
            raw = model.children[1].value
            if raw.startswith(EMBED_PREFIX):
                continue        # carried inside the footprint file itself
            mcat = classify(raw, project_dir)
            rows.append({"type": "model", "ref": label,
                         "name": os.path.basename(raw.replace("\\", "/")),
                         "source": raw, "status": mcat,
                         "key": ("libmodel", rel, mi),
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
    """Copy a model (and its twins) into packages3D/ -- flat, never into a
    subfolder -- and return (kiprjmod_path, files_copied).

    Never overwrites: a destination that already holds identical content is
    reused, and one holding different content (the user's own file, or a
    same-named model from another library) gets a _1, _2, ... suffix instead."""
    project_dir = session.project_dir
    parts = [LOCAL_3D_DIRNAME]
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


def localize_library_models(session, rows):
    """Copy the 3D models that project-local library footprints name into
    packages3D/ (the same place, and the same never-overwrite rule, as board
    models, so a library footprint and the board share one file) and point
    the footprints at them. `rows` are scan_items() rows keyed 'libmodel'."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)
    by_file = {}
    for r_ in rows:
        if r_["key"][0] == "libmodel":
            by_file.setdefault(r_["key"][1], set()).add(r_["key"][2])
    copied = remapped = 0
    placed = {}
    for rel, mis in by_file.items():
        path = os.path.join(project_dir, rel)
        text = _read(path)
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        for mi, model in enumerate(root.find_all("model")):
            if mi not in mis or not model.children[1:]:
                continue
            atom = model.children[1]
            resolved = resolve(atom.value)
            if not os.path.isfile(resolved):
                continue
            key = os.path.normcase(os.path.abspath(resolved))
            if key not in placed:
                placed[key], n = _place_model(session, resolved)
                copied += n
            editor.replace_atom(atom, placed[key], force_quote=True)
            remapped += 1
        write_validated(session, path, editor.result())
    return {"files_copied": copied, "remapped": remapped, "footprints": len(by_file)}


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
    the referenced model first, into packages3D/. Returns
    ({name: kiprjmod path}, files_written).

    Never overwrites, as _place_model: identical content is reused, and a
    destination holding different content gets a _1, _2, ... stem suffix (the
    same suffix for the model and its twins, so they stay paired)."""
    parts = [LOCAL_3D_DIRNAME]
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
    embedded twins, into packages3D/, remap their (model ...) paths
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

# --- STEP instead of VRML ----------------------------------------------------
#
# A .wrl (VRML) model is a mesh: the 3D viewer shows it, but a STEP export
# leaves the part out ("Cannot use VRML models when exporting to non-mesh
# formats") unless --subst-models is given. When a .step/.stp of the same
# name sits beside it, point the model at that instead.

_STEP_EXTS = (".step", ".stp", ".STEP", ".STP")


def _step_twin(raw, resolve, embed_names):
    """The STEP twin of a .wrl model path (same folder, same name), as the
    path to write, or None. Works for kicad-embed:// models too."""
    if raw.startswith(EMBED_PREFIX):
        name = raw[len(EMBED_PREFIX):]
        if name.lower().endswith(".wrl"):
            for ext in _STEP_EXTS:
                if name[:-4] + ext in embed_names:
                    return EMBED_PREFIX + name[:-4] + ext
        return None
    if not raw.lower().endswith(".wrl"):
        return None
    resolved = resolve(raw)
    for ext in _STEP_EXTS:
        if os.path.isfile(resolved[:-4] + ext):
            return raw[:-4] + ext
    return None


def local_footprint_files(project_dir):
    """[(library nickname, .kicad_mod path)] for the footprints of the
    project's own (${KIPRJMOD}) footprint libraries."""
    resolve = make_resolver(project_dir)
    table = parse_lib_table(os.path.join(project_dir, "fp-lib-table"))
    out = []
    for nick, uri in sorted(table.items()):
        if "${KIPRJMOD}" in uri:
            out += [(nick, p) for p in sorted(glob.glob(os.path.join(resolve(uri), "*.kicad_mod")))]
    return out


def _model_holders(project_dir):
    """Files whose (model ...) paths the project controls: its boards and the
    footprints of its project-local (${KIPRJMOD}) footprint libraries."""
    return (sorted(glob.glob(os.path.join(project_dir, "*.kicad_pcb")))
            + [p for _n, p in local_footprint_files(project_dir)])


def _vrml_swaps(project_dir, path, text, root):
    """[(model atom, new path)] for one board or footprint file."""
    resolve = make_resolver(project_dir)
    fps = root.find_all("footprint") if root.head() == "kicad_pcb" else [root]
    board = _embedded_names(root) if root.head() == "kicad_pcb" else set()
    out = []
    for fp in fps:
        names = board | _embedded_names(fp)
        for m in fp.find_all("model"):
            if m.children[1:] and m.children[1].kind == "atom":
                new = _step_twin(m.children[1].value, resolve, names)
                if new:
                    out.append((m.children[1], new))
    return out


def scan_vrml_models(project_dir):
    """Read-only: [{'file', 'model', 'step'}] for every .wrl model reference
    in the project's boards and local footprints that has a STEP twin."""
    rows = []
    for path in _model_holders(project_dir):
        text = _read(path)
        for atom, new in _vrml_swaps(project_dir, path, text, sexpr.parse(text)):
            rows.append({"file": os.path.relpath(path, project_dir).replace(os.sep, "/"),
                         "model": atom.value, "step": new})
    return rows


def use_step_models(session):
    """Point every .wrl model reference that has a STEP twin at the STEP file,
    in the boards and the project-local footprints, backing each file up."""
    refs = files = 0
    for path in _model_holders(session.project_dir):
        text = _read(path)
        swaps = _vrml_swaps(session.project_dir, path, text, sexpr.parse(text))
        if not swaps:
            continue
        editor = sexpr.Editor(text)
        for atom, new in swaps:
            editor.replace_atom(atom, new, force_quote=True)
        write_validated(session, path, editor.result())
        refs += len(swaps)
        files += 1
    return {"references": refs, "files": files}


LOCAL_LIB_DIRNAME = "libs"
LOCAL_SUFFIX = "_local"


def classify_lib(uri, project_dir):
    """external / standard / local / missing for a library uri."""
    if not uri:
        return "missing"
    if "${KIPRJMOD}" in uri:
        return "local"
    resolved = make_resolver(project_dir)(uri)
    is_std = bool(re.search(r"\$\{KICAD\d*_(FOOTPRINT|SYMBOL)_DIR\}", uri)) or _in_install(resolved)
    if not os.path.exists(resolved):
        return "missing"
    return "standard" if is_std else "external"


# Statuses ticked by default ("standard" only with the include-standard box).
DEFAULT_ON = ("external", "rebuild", "nested", "update")


def _wanted(cat, include_standard):
    return cat in DEFAULT_ON or (cat == "standard" and include_standard)


def _footprint_file(project_dir, uri, fpid):
    """Path the footprint's .kicad_mod would have ('' when unresolvable)."""
    if not uri or ":" not in fpid:
        return ""
    return os.path.join(make_resolver(project_dir)(uri), fpid.split(":", 1)[1] + ".kicad_mod")


# --- rebuilding a library footprint from its board copy ----------------------
#
# A .kicad_pcb holds a full copy of every footprint it places, so a footprint
# whose library is gone can be rebuilt from the board -- what KiCad's "Export
# Footprints to Library" does (PCB_IO_KICAD_SEXPR::FootprintSave): orientation
# to 0, flipped back to the front (top-bottom, KiCad's default), nets cleared,
# reference REF**, placement data dropped. In the board file a footprint's
# children are stored in its own frame, so the orientation changes only the
# absolute angles of pads and texts; a flip mirrors y, swaps F./B. layers,
# toggles the mirroring of text on front/back layers (other layers keep
# theirs), maps angles (text a -> 180 - a, pad a -> -a) and, on pads, swaps
# top/bottom chamfers and mirrors the drill offset and trapezoid delta. Zones, points and
# dimensions are stored in board coordinates and are made local first.
# Checked against KiCad 10's own FootprintSave on a 318-footprint board (all
# four rotations, both sides): identical apart from equal-valued angle
# spellings (-90 / 270) and "*.Cu" on through-hole pads (KiCad writes the
# board's own copper layers).

_REBUILD_DROP = {"at", "uuid", "path", "units", "net", "placed"}
_XY_KEYS = {"at", "start", "end", "center", "mid", "xy", "pos"}
_NUM = re.compile(r"-?\d+(\.\d+)?(e-?\d+)?$")


def _fmt(v):
    s = "{:.6f}".format(v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _half_turn(a):
    """a + 180 degrees, normalised to (-180, 180]."""
    a = (a + 360) % 360 - 180
    return 180.0 if a == -180 else a


def _flip_layer(name):
    if name.startswith("B."):
        return "F." + name[2:]
    if name.startswith("F."):
        return "B." + name[2:]
    return name


_KEEP_FIELDS = ("Reference", "Value", "Datasheet", "Description")


def flip_direction():
    """KiCad's flip direction for the user, as its own footprint export uses
    it: "left_right" when pcbnew's editing.flip_left_right is set in the
    newest KiCad config, else "top_bottom" (KiCad's default)."""
    for d in _config_dirs():
        path = os.path.join(d, "pcbnew.json")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
                return "left_right" if cfg.get("editing", {}).get("flip_left_right") else "top_bottom"
            except (OSError, ValueError):
                return "top_bottom"
    return "top_bottom"


def rebuild_footprint(text, fp, version, board_embeds=None, instance_fields=False,
                      flip="top_bottom"):
    """Library .kicad_mod text for the placed footprint `fp` (a list node of
    the board `text`). `board_embeds` ({name: file node}) supplies embedded
    3D models the footprint uses, copied into the footprint's own block.

    By default the result is a clean library footprint: Value is the
    footprint name, Datasheet and Description are empty, every other field
    (LCSC, MPN, Manufacturer, ...) and the sheet name/file are dropped -- part
    numbers belong in
    the schematic, and a library footprint carrying one instance's part
    number leaks it into every other part using that footprint.
    instance_fields=True keeps the instance's fields, as KiCad's "Export
    Footprints to Library" does.

    flip is the direction a back-side footprint is flipped back in, as in
    KiCad's settings (see flip_direction()). A left-right flip is a
    top-bottom flip plus a 180 degree turn (FOOTPRINT::Flip), so the result
    is the top-bottom one turned 180 degrees as a rigid whole: positions
    (-x, -y), absolute and text-box angles +180, 3D model offset (-x, -y) and
    z rotation +180; pad internals (chamfers, drill offset, primitives) stay.
    KiCad's own export does this turn after resetting the orientation and so
    loses it (pads and texts turn in place, the outline doesn't); this
    recovers the footprint a left-right flip was made from instead."""
    # Work on the footprint's own text: edits on the whole board text would
    # cost board-size per edit (minutes per footprint on a 70 MB board).
    board_text = text
    text = board_text[fp.start:fp.end]
    fp = sexpr.parse(text)
    orient, back = _placement(fp)
    at = fp.find("at")
    px = float(at.children[1].value) if at and at.children[2:] else 0.0
    py = float(at.children[2].value) if at and at.children[2:] else 0.0
    cos_t, sin_t = math.cos(math.radians(orient)), math.sin(math.radians(orient))
    ed = sexpr.Editor(text)

    def zone_local(x, y):
        """Footprint zones, points and dimensions are stored in board
        coordinates, unlike every other child:
        absolute = position + R(orientation) . local."""
        ax, ay = x - px, y - py
        lx, ly = ax * cos_t - ay * sin_t, ax * sin_t + ay * cos_t
        if back:
            ly = -ly
            if turn:
                lx, ly = -lx, -ly
        return lx, ly

    turn = back and flip == "left_right"

    def angle(a, kind):
        a -= orient
        if back:
            a = (180 - a) if kind == "text" else -a
            if turn:
                a += 180
        a = (a + 180) % 360 - 180
        return 180.0 if a == -180 else a

    def drop(node):
        start = node.start
        while start > 0 and text[start - 1] in " \t":
            start -= 1
        if start > 0 and text[start - 1] == "\n":
            start -= 1
        ed.replace_span(start, node.end, "")

    def _flip_mirror(t_node):
        """A flip toggles the mirroring of text on a front/back layer (back
        text normally comes to the front unmirrored, but a footprint drawn
        inconsistently keeps its quirk); text on any other layer (Dwgs.User,
        User.1, ...) keeps its mirroring, as KiCad does."""
        lay, eff = t_node.find("layer"), t_node.find("effects")
        if lay is None or eff is None or not lay.children[1:]:
            return
        old = lay.children[1].value
        if not old.startswith(("F.", "B.")):
            return
        j = eff.find("justify")
        has = j is not None and any(a_.value == "mirror" for a_ in j.children[1:])
        want = not has
        if want and not has:
            if j is None:
                ed.replace_span(eff.end - 1, eff.end - 1, " (justify mirror)")
            else:
                ed.replace_span(j.end - 1, j.end - 1, " mirror")
        elif has and not want:
            if all(a_.value == "mirror" for a_ in j.children[1:]):
                drop(j)         # (justify mirror) -> nothing
            else:
                for a_ in j.children[1:]:
                    if a_.value == "mirror":
                        i = a_.start
                        while i > 0 and text[i - 1] in " \t\n":
                            i -= 1
                        ed.replace_span(i, a_.end, "")

    def walk(node, ctx):
        for c in node.children:
            if c.kind != "list":
                continue
            h = c.head()
            if (node is fp and h in _REBUILD_DROP) or h == "net":
                drop(c)
                continue
            if node is fp and not instance_fields and (
                    h in ("sheetname", "sheetfile", "locked", "component_classes") or
                    (h == "property" and len(c.children) >= 3
                     and c.children[1].value not in _KEEP_FIELDS)):
                drop(c)         # placement data, part numbers: see the docstring
                continue
            if (h in _XY_KEYS and len(c.children) >= 3 and _NUM.match(c.children[1].value or "")
                    and _NUM.match(c.children[2].value or "")):
                if ctx == "zone":
                    lx, ly = zone_local(float(c.children[1].value), float(c.children[2].value))
                    ed.replace_atom(c.children[1], _fmt(lx))
                    ed.replace_atom(c.children[2], _fmt(ly))
                elif back:
                    if turn and ctx != "pad-primitive":
                        # top-bottom mirror (y) then the 180 degree turn (x, y)
                        ed.replace_atom(c.children[1], _fmt(-float(c.children[1].value)))
                    else:
                        ed.replace_atom(c.children[2], _fmt(-float(c.children[2].value)))
                if h == "at" and ctx in ("text", "pad"):
                    if len(c.children) >= 4 and _NUM.match(c.children[3].value or ""):
                        a = angle(float(c.children[3].value), ctx)
                        if a:
                            ed.replace_atom(c.children[3], _fmt(a))
                        else:   # KiCad leaves a zero angle out
                            ed.replace_span(c.children[2].end, c.children[3].end, "")
                    elif orient or back:
                        a = angle(0.0, ctx)
                        if a:
                            ed.replace_span(c.children[2].end, c.children[2].end, " " + _fmt(a))
                continue
            if back and ctx == "pad" and h == "chamfer":
                # PAD::Flip (top-bottom) swaps top and bottom corners
                swap = {"top_left": "bottom_left", "bottom_left": "top_left",
                        "top_right": "bottom_right", "bottom_right": "top_right"}
                for a_ in c.children[1:]:
                    if a_.value in swap:
                        ed.replace_atom(a_, swap[a_.value])
                continue
            if (back and ctx == "pad" and h in ("offset", "rect_delta") and len(c.children) >= 3
                    and _NUM.match(c.children[2].value or "")):
                ed.replace_atom(c.children[2], _fmt(-float(c.children[2].value)))
                continue
            if back and h == "layer" and c.children[1:]:
                ed.replace_atom(c.children[1], _flip_layer(c.children[1].value),
                                force_quote=c.children[1].quoted)
                continue
            if back and h == "layers":
                for a_ in c.children[1:]:
                    ed.replace_atom(a_, _flip_layer(a_.value), force_quote=a_.quoted)
                continue
            if back and h == "justify":
                continue        # handled at the text node, below
            if h == "model":
                if turn:    # the 3D model turns with the footprint
                    for key, fix in (("offset", lambda v: (-v[0], -v[1], v[2])),
                                     ("rotate", lambda v: (v[0], v[1], _half_turn(v[2])))):
                        node = c.find(key)
                        xyz = node.find("xyz") if node is not None else None
                        if xyz is not None and len(xyz.children) >= 4:
                            new = fix([float(a_.value) for a_ in xyz.children[1:4]])
                            for a_, v in zip(xyz.children[1:4], new):
                                ed.replace_atom(a_, _fmt(v))
                continue
            if back and h in ("property", "fp_text", "fp_text_box", "gr_text"):
                _flip_mirror(c)
            if back and h == "fp_text_box":
                # a text box's angle is relative to the footprint: flip only
                ang = c.find("angle")
                a = (180 - float(ang.children[1].value)) if ang is not None else 180.0
                a += 180 if turn else 0
                a = (a + 180) % 360 - 180
                a = 180.0 if a == -180 else a
                if ang is not None and not a:
                    drop(ang)
                elif ang is not None:
                    ed.replace_atom(ang.children[1], _fmt(a))
                else:
                    lay = c.find("layer")
                    ed.replace_span(lay.start, lay.start, "(angle {}) ".format(_fmt(a)))
            if back and h == "dimension":
                hgt = c.find("height")
                if hgt is not None and _NUM.match(hgt.children[1].value or ""):
                    ed.replace_atom(hgt.children[1], _fmt(-float(hgt.children[1].value)))
            walk(c, "zone" if (h in ("zone", "point", "dimension") or ctx == "zone") else
                 "pad-primitive" if (h == "primitives" or ctx == "pad-primitive") else
                 "pad" if h == "pad" else "text" if h in ("property", "fp_text") else ctx)

    walk(fp, "fp")
    bare = fp.children[1].value.split(":", 1)[-1]
    for c in fp.children:
        if not (c.kind == "list" and c.head() == "property" and len(c.children) >= 3):
            continue
        field = c.children[1].value
        if field == "Reference":
            ed.replace_atom(c.children[2], "REF**", force_quote=True)
        elif instance_fields:
            continue
        elif field == "Value":
            ed.replace_atom(c.children[2], bare, force_quote=True)
        elif field in ("Datasheet", "Description"):
            ed.replace_atom(c.children[2], "", force_quote=True)
    if back:
        ed.replace_atom(fp.find("layer").children[1], "F.Cu", force_quote=True)
    name = fp.children[1]
    ed.replace_atom(name, name.value.split(":", 1)[-1], force_quote=True)
    ed.replace_span(name.end, name.end, '\n\t\t(version {})\n\t\t(generator "pcbnew")\n'
                    '\t\t(generator_version "10.0")'.format(version))
    if board_embeds and fp.find("embedded_files") is None:
        wanted = [m.children[1].value[len(EMBED_PREFIX):] for m in fp.find_all("model")
                  if m.children[1:] and m.children[1].value.startswith(EMBED_PREFIX)]
        files = [board_embeds[n] for n in wanted if n in board_embeds]
        if files:
            ed.replace_span(fp.end - 1, fp.end - 1, "\t(embedded_files\n" + "".join(
                "\t\t\t" + board_text[f.start:f.end] + "\n" for f in files) + "\t\t)\n\t")
    out = ed.result()
    # the block was indented one level inside the board; a library file isn't
    return re.sub(r"\n\t", "\n", out) + "\n"


def _placement(fp):
    """(orientation in degrees, on the back?) of a placed footprint."""
    at, lay = fp.find("at"), fp.find("layer")
    orient = float(at.children[3].value) if at and len(at.children) > 3 else 0.0
    return orient, bool(lay and lay.children[1:] and lay.children[1].value == "B.Cu")


def _write_rebuilt(session, path, mod):
    sexpr.parse(mod)
    session.wrote_new_file(path)
    tmp = path + ".portability-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(mod)
    os.replace(tmp, path)
    session.written(path)


def _best_instances(root):
    """{footprint id: placed footprint node to rebuild from}: an instance on
    the front at 0 degrees if there is one (nothing to undo), else on the
    front, else any."""
    best = {}

    def rank(fp):
        orient, back = _placement(fp)
        return (2 if back else 0) + (0 if orient == 0 else 1)
    for fp in root.find_all("footprint"):
        if fp.children[1:] and fp.children[1].kind == "atom":
            fid = fp.children[1].value
            if fid not in best or rank(fp) < rank(best[fid]):
                best[fid] = fp
    return best


def localize_footprints(session, rows):
    """Copy the selected footprints into ${KIPRJMOD}/libs/<nick>_local.pretty,
    register those libraries in fp-lib-table, and remap the selected footprint
    IDs. Per-item: only the checked footprints move, and both the original and
    the _local nickname stay valid, so unselected footprints are untouched.

    The schematic symbols' Footprint fields follow (see
    _sync_schematic_footprints), or the next "Update PCB from Schematic" would
    swap the footprints straight back to the global library. A footprint whose
    library file can't be found is rebuilt from its copy in the board
    (rebuild_footprint)."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)
    keys = {r["key"] for r in rows if r["type"] == "footprint"}
    if not keys:
        return {"libraries": 0, "files_copied": 0, "remapped": 0}

    by_pcb = {}
    for k in keys:
        by_pcb.setdefault(k[1], set()).add(k)

    entries = {}
    src_dir_cache = {}   # nick -> (resolved library dir, inside the project?)
    restored = set()     # footprint ids rebuilt into their own project library
    flip = flip_direction()
    copied = remapped = rebuilt = 0
    moved = {}          # board reference -> (old fpid, new fpid)
    for rel, kset in by_pcb.items():
        pcb = os.path.join(project_dir, rel)
        text = _read(pcb)
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        best = _best_instances(root)
        ver = root.find("version")
        version = ver.children[1].value if ver and ver.children[1:] else "20260206"
        board_embeds = _embedded_files(root)
        for fi, fp in enumerate(root.find_all("footprint")):
            if ("footprint", rel, fi) not in kset:
                continue
            atom = fp.children[1]
            if ":" not in atom.value:
                continue
            nick, name = atom.value.split(":", 1)
            if nick not in src_dir_cache:
                uri = resolve_lib_uri(project_dir, "fp-lib-table", nick) or ""
                src_dir_cache[nick] = (resolve(uri), "${KIPRJMOD}" in uri)
            src_dir, in_project = src_dir_cache[nick]
            src_file = os.path.join(src_dir, name + ".kicad_mod")
            if in_project:
                # already a project library: never re-localized. If it has
                # lost this footprint, put the rebuilt file back where the
                # table says it lives; the footprint id is already right.
                if not os.path.isfile(src_file) and atom.value not in restored:
                    session.mkdir(src_dir)
                    _write_rebuilt(session, src_file,
                                   rebuild_footprint(text, best[atom.value], version, board_embeds,
                                                     flip=flip))
                    restored.add(atom.value)
                    rebuilt += 1
                continue
            local_nick = nick + LOCAL_SUFFIX
            local_dir = os.path.join(project_dir, LOCAL_LIB_DIRNAME,
                                     local_nick + LOCAL_FP_SUFFIX)
            session.mkdir(local_dir)
            dst_file = os.path.join(local_dir, name + ".kicad_mod")
            if not (os.path.isfile(src_file) or os.path.isfile(dst_file)):
                # library file gone: rebuild it from the board's copy
                _write_rebuilt(session, dst_file,
                               rebuild_footprint(text, best[atom.value], version, board_embeds,
                                                     flip=flip))
                rebuilt += 1
            elif os.path.isfile(src_file) and not os.path.isfile(dst_file):
                session.wrote_new_file(dst_file)
                shutil.copy2(src_file, dst_file)
                session.written(dst_file)
                copied += 1
            editor.replace_atom(atom, "{}:{}".format(local_nick, name), force_quote=True)
            entries[local_nick] = "${{KIPRJMOD}}/{}/{}{}".format(
                LOCAL_LIB_DIRNAME, local_nick, LOCAL_FP_SUFFIX)
            remapped += 1
            ref = _prop(fp, "Reference")
            if ref:
                moved[ref] = (atom.value, "{}:{}".format(local_nick, name))
        write_validated(session, pcb, editor.result())

    add_lib_entries(session, "fp", entries)
    sch = _sync_schematic_footprints(session, moved)
    return {"libraries": len(entries), "files_copied": copied, "remapped": remapped,
            "rebuilt_from_board": rebuilt,
            "schematic_fields": sch["updated"], "schematic_skipped": sch["skipped"]}


def _symbol_refs(sym):
    """Every reference designator a placed symbol carries: its Reference field
    plus one per sheet instance (a symbol in a reused sheet has several)."""
    refs = set()
    r = _prop(sym, "Reference")
    if r:
        refs.add(r)
    inst = sym.find("instances")
    if inst is not None:
        for lst in inst.iter_lists():
            if lst.head() == "reference" and lst.children[1:]:
                refs.add(lst.children[1].value)
    return refs


def _sync_schematic_footprints(session, moved):
    """Point the schematic symbols of moved footprints at the localized
    footprint: (property "Footprint" "Lib:X") -> "Lib_local:X". A symbol in a
    reused sheet has one Footprint field for all its instances, so it changes
    only when the board now has every one of its references on that same
    localized footprint (this run or an earlier one); otherwise it is left and
    counted as skipped."""
    updated = skipped = 0
    if not moved:
        return {"updated": 0, "skipped": 0}
    on_board = {}       # reference -> footprint id, as the boards are now
    for pcb in glob.glob(os.path.join(session.project_dir, "*.kicad_pcb")):
        for fp in sexpr.parse(_read(pcb)).find_all("footprint"):
            ref = _prop(fp, "Reference")
            if ref and fp.children[1:] and fp.children[1].kind == "atom":
                on_board[ref] = fp.children[1].value
    for sch in sorted(glob.glob(os.path.join(session.project_dir, "*.kicad_sch"))):
        text = _read(sch)
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        touched = False
        for sym in _symbol_instances(root):
            refs = _symbol_refs(sym) & set(moved)
            if not refs:
                continue
            field = None
            for c in sym.children:
                if (c.kind == "list" and c.head() == "property" and len(c.children) >= 3
                        and c.children[1].value == "Footprint"):
                    field = c.children[2]
            changes = {moved[r] for r in refs}
            if field is None or len(changes) != 1:
                skipped += 1
                continue
            old, new = changes.pop()
            if any(on_board.get(x) != new for x in _symbol_refs(sym) if x in on_board):
                skipped += 1    # a sheet instance still uses another footprint
                continue
            if field.value == old:
                editor.replace_atom(field, new, force_quote=True)
                updated += 1
                touched = True
            elif field.value != new:
                skipped += 1    # the schematic named some other footprint
        if touched:
            write_validated(session, sch, editor.result())
    return {"updated": updated, "skipped": skipped}


# Fields every symbol has; never removed. ki_* properties (keywords,
# footprint filters) live only in libraries, so instances never carry them.
MANDATORY_FIELDS = ("Reference", "Value", "Footprint", "Datasheet", "Description")


def _fields(sym):
    """{name: property node} of a symbol's own fields, ki_* left out."""
    out = {}
    for c in sym.children:
        if (c.kind == "list" and c.head() == "property" and c.children[2:]
                and c.children[1].kind == "atom"
                and not c.children[1].value.startswith("ki_")):
            out[c.children[1].value] = c
    return out


def user_fields(sym):
    """A symbol's own user fields: {name: property node}, leaving out the
    mandatory fields and KiCad's internal ki_* properties."""
    return {n: c for n, c in _fields(sym).items() if n not in MANDATORY_FIELDS}


def placed_symbols(sch_paths):
    """{lib_id: [{field: value} for each placed symbol]} over the given
    schematics. Attributes such as (dnp yes) are not fields and play no
    part."""
    out = {}
    for sch in sch_paths:
        for sym in _symbol_instances(sexpr.parse(_read(sch))):
            lid = sym.find("lib_id")
            if lid and lid.children[1:]:
                out.setdefault(lid.children[1].value, []).append(
                    {n: c.children[2].value for n, c in _fields(sym).items()})
    return out


def placed_fields(sch_paths):
    """{lib_id: names of the fields its placed symbols carry}."""
    return {lid: set().union(*insts)
            for lid, insts in placed_symbols(sch_paths).items()}


def dropped_fields(defs, placed):
    """The user fields to remove from a library's definitions, {name: set}.

    defs: {name: (own user field names, extends parent or None)}.
    placed: {name: field names its placed symbols carry} for this library.

    A field deleted from every placed symbol (an LCSC number, say) would come
    back with the next *Update Symbols from Library*, so the library loses it
    too. A definition keeps whatever any placed symbol using it (directly or
    through 'extends') still has; definitions nothing placed uses are left
    alone."""
    keep = {}
    for name, fields in placed.items():
        seen = set()
        while name in defs and name not in seen:
            seen.add(name)
            keep.setdefault(name, set()).update(fields)
            name = defs[name][1]
    return {n: defs[n][0] - k for n, k in keep.items() if defs[n][0] - k}


def cache_drops(name, cached_fields, defs, drops):
    """Of the user fields in the schematic's flattened lib_symbols copy of
    `name`, those that go: each follows the definition in the extends chain
    that provides it, so the cache stays equal to the library."""
    out, chain, n = set(), [], name
    while n in defs and n not in chain:
        chain.append(n)
        n = defs[n][1]
    for f in cached_fields:
        owner = next((d for d in chain if f in defs[d][0]), None)
        if owner is not None and f in drops.get(owner, ()):
            out.add(f)
    return out


def _field_value(v):
    """A field value as KiCad reads it: "~" is its old marker for empty,
    read as empty in a schematic but literally in a library file."""
    return "" if v == "~" else v


def symbol_updates(lib_defs, placed):
    """What a library's definitions need so they match their placed symbols.

    lib_defs: {name: ({field: value}, extends parent or None)}, own fields.
    placed: {name: [{field: value} per placed symbol]} for this library.

    Returns {name: {"remove": set, "set": {field: value}, "add": {field:
    value}}}, only for definitions that need something:
    - remove: user fields no placed symbol carries any more (dropped_fields);
    - set: fields (all but Reference) every placed symbol has, with one value
      that differs from the library's -- an LCSC number changed on the parts;
    - add: user fields every placed symbol has, with one value, that the
      library lacks.
    A field the placed symbols disagree on (a generic resistor's Value, say)
    is left as it is. Values are only set or added on a definition that has
    no 'extends' parent and that no placed symbol extends, so a change never
    reaches symbols it wasn't made on."""
    defs = {n: (set(f) - set(MANDATORY_FIELDS), p) for n, (f, p) in lib_defs.items()}
    drops = dropped_fields(defs, {n: set().union(*i) for n, i in placed.items() if i})
    parents = set()
    for n in placed:
        p = lib_defs.get(n, ({}, None))[1]
        while p and p in lib_defs and p not in parents:
            parents.add(p)
            p = lib_defs[p][1]
    out = {}
    for n, (own, parent) in lib_defs.items():
        upd = {"remove": drops.get(n, set()), "set": {}, "add": {}}
        insts = placed.get(n)
        if insts and parent is None and n not in parents:
            for f in set.intersection(*(set(i) for i in insts)) - {"Reference"}:
                vals = {_field_value(i[f]) for i in insts}
                if len(vals) != 1:
                    continue
                v = vals.pop()
                if f in own:
                    if _field_value(own[f]) != v:
                        upd["set"][f] = v
                elif f not in MANDATORY_FIELDS:
                    upd["add"][f] = v
        if upd["remove"] or upd["set"] or upd["add"]:
            out[n] = upd
    return out


def _new_property(text, sym, name, value):
    """Text for a hidden property, laid out like the symbol's last one, to
    insert just after it. Returns (offset, text)."""
    props = sym.find_all("property")
    last = props[-1] if props else None
    at = last.end if last else sym.children[1].end
    line = text.rfind("\n", 0, last.start if last else sym.start) + 1
    ind = re.match(r"[ \t]*", text[line:]).group(0) + ("" if last else "\t")
    i2, i3, i4 = ind + "\t", ind + "\t\t", ind + "\t\t\t"
    return at, ("\n{0}(property {1} {2}\n{3}(at 0 0 0)\n{3}(hide yes)\n{3}(effects\n"
                "{4}(font\n{5}(size 1.27 1.27)\n{4})\n{3})\n{0})").format(
                    ind, sexpr.quote(name), sexpr.quote(value), i2, i3, i4)


def apply_symbol_update(editor, text, sym, upd, remove=None):
    """Queue `upd` (one symbol_updates() entry) against `sym` in `editor`.
    `remove` overrides the fields to delete (for a flattened cache copy).
    Returns the number of fields changed."""
    n = 0
    remove = upd["remove"] if remove is None else remove
    for name, node in _fields(sym).items():
        if name in remove and name not in MANDATORY_FIELDS:
            _delete_span(editor, text, node)
            n += 1
        elif name in upd["set"]:
            editor.replace_atom(node.children[2], upd["set"][name], force_quote=True)
            n += 1
    have = set(_fields(sym))
    for name, value in sorted(upd["add"].items()):
        if name not in have:
            at, new = _new_property(text, sym, name, value)
            editor.replace_span(at, at, new)
            n += 1
    return n


def drop_properties(text, sym, names):
    """`text` with the named user fields of `sym` (parsed from it) removed."""
    editor = sexpr.Editor(text)
    apply_symbol_update(editor, text, sym, {"remove": set(names), "set": {}, "add": {}})
    return editor.result()


def _library_files(lib):
    return ([os.path.join(lib, f) for f in sorted(os.listdir(lib))
             if f.lower().endswith(".kicad_sym")] if os.path.isdir(lib) else [lib])


def library_definitions(lib):
    """{name: ({field: value}, extends parent)} of a .kicad_sym file or a
    folder library, plus the parsed files [(path, text, root)]."""
    parsed, defs = [], {}
    for f in _library_files(lib):
        text = _read(f)
        try:
            root = sexpr.parse(text)
        except sexpr.SExprError:
            continue
        parsed.append((f, text, root))
        for s in root.find_all("symbol"):
            if s.children[1:] and s.children[1].kind == "atom":
                ext = s.find("extends")
                defs[s.children[1].value] = (
                    {n: c.children[2].value for n, c in _fields(s).items()},
                    ext.children[1].value if ext and ext.children[1:] else None)
    return defs, parsed


def _sync_library(session, lib, placed):
    """Bring library `lib` in line with its placed symbols (symbol_updates).
    `placed` is {symbol name: [fields per placed symbol]}. Returns
    (defs, updates, fields changed)."""
    defs, parsed = library_definitions(lib)
    updates = symbol_updates(defs, placed)
    changed = 0
    for f, text, root in parsed:
        editor = sexpr.Editor(text)
        for s in root.find_all("symbol"):
            upd = updates.get(s.children[1].value if s.children[1:] else None)
            if upd:
                changed += apply_symbol_update(editor, text, s, upd)
        new = editor.result()
        if new != text:
            write_validated(session, f, new)
    return defs, updates, changed


def _updated_fields(own, upd):
    """A definition's own fields once `upd` (from symbol_updates) is applied."""
    out = {k: v for k, v in own.items() if k not in upd["remove"]}
    out.update(upd["set"])
    for k, v in upd["add"].items():
        out.setdefault(k, v)
    return out


def cache_fix(entry, target):
    """The update (symbol_updates-style) that makes a schematic lib_symbols
    entry's fields equal `target`, the library's {field: value} for a symbol
    with no 'extends' parent; None when they already agree. Reference and
    ki_* properties are left alone; "~" counts as empty."""
    have = {n: c.children[2].value for n, c in _fields(entry).items()}
    upd = {"remove": {n for n in have if n not in target and n not in MANDATORY_FIELDS},
           "set": {}, "add": {}}
    for n, v in target.items():
        if n == "Reference":
            continue
        if n in have:
            if _field_value(have[n]) != _field_value(v):
                upd["set"][n] = v
        elif n not in MANDATORY_FIELDS:
            upd["add"][n] = v
    return upd if (upd["remove"] or upd["set"] or upd["add"]) else None


def _sync_cache_entry(editor, text, entry, name, defs, updates, exact=False):
    """Make one schematic lib_symbols entry follow its library's updates.
    The entry is flattened: a removed field goes with the definition in the
    extends chain that provides it; values are only set or added where the
    definition has no parent (and so equals the entry). With `exact`, such
    an entry is made equal to the updated library symbol outright, which
    also repairs a copy KiCad wrote back over an earlier update."""
    if exact and name in defs and defs[name][1] is None:
        target = _updated_fields(defs[name][0], updates.get(name, {"remove": set(), "set": {}, "add": {}}))
        upd = cache_fix(entry, target)
        return apply_symbol_update(editor, text, entry, upd) if upd else 0
    udefs = {n: (set(f) - set(MANDATORY_FIELDS), p) for n, (f, p) in defs.items()}
    drops = {n: u["remove"] for n, u in updates.items()}
    remove = cache_drops(name, user_fields(entry), udefs, drops)
    upd = updates.get(name, {"remove": set(), "set": {}, "add": {}})
    if name in defs and defs[name][1] is not None:
        upd = {"remove": set(), "set": {}, "add": {}}
    return apply_symbol_update(editor, text, entry, upd, remove=remove)


def _placed_by_library(schematics):
    """{nick: {symbol name: [fields per placed symbol]}}."""
    out = {}
    for lid, insts in placed_symbols(schematics).items():
        if ":" in lid:
            nick, name = lid.split(":", 1)
            out.setdefault(nick, {})[name] = insts
    return out


def stale_symbols(project_dir, nicks):
    """lib_ids, among the given library nicknames, whose library definition
    no longer matches its placed symbols (see symbol_updates), or whose
    copy in a schematic's lib_symbols no longer matches the library (KiCad
    saved its old copy over an update, say)."""
    resolve = make_resolver(project_dir)
    schematics = glob.glob(os.path.join(project_dir, "*.kicad_sch"))
    out = set()
    libs = {}               # nick -> (defs, updates)
    for nick, placed in _placed_by_library(schematics).items():
        if nick not in nicks:
            continue
        uri = resolve_lib_uri(project_dir, "sym-lib-table", nick)
        path = resolve(uri) if uri else ""
        if not (os.path.isfile(path) or os.path.isdir(path)):
            continue
        defs, _parsed = library_definitions(path)
        updates = symbol_updates(defs, placed)
        libs[nick] = (defs, updates)
        for name in updates:
            if name in placed:
                out.add("{}:{}".format(nick, name))
            else:   # a parent losing a field: flag the symbols that extend it
                out.update("{}:{}".format(nick, n) for n in placed
                           if defs.get(n, ({}, None))[1] == name)
    for sch in schematics:
        root = sexpr.parse(_read(sch))
        alias = _lib_name_aliases(root)
        ls = root.find("lib_symbols")
        for entry in (ls.find_all("symbol") if ls else []):
            if not entry.children[1:]:
                continue
            lid = alias.get(entry.children[1].value, entry.children[1].value)
            nick, _, name = lid.partition(":")
            if nick not in libs or lid in out:
                continue
            defs, updates = libs[nick]
            if name in defs and defs[name][1] is None and cache_fix(
                    entry, _updated_fields(defs[name][0], updates.get(name, {"remove": set(), "set": {}, "add": {}}))):
                out.add(lid)
    return out


def _lib_name_aliases(root):
    """{lib_symbols entry name: lib_id} for placed symbols whose cached copy
    KiCad renamed: they carry (lib_name "X_1"), an entry with no "nick:"."""
    alias = {}
    for sym in _symbol_instances(root):
        ln = sym.find("lib_name")
        if ln and ln.children[1:]:
            alias[ln.children[1].value] = sym.find("lib_id").children[1].value
    return alias


def _sync_schematic_caches(session, schematics, synced, rename=None, exact=False):
    """Apply `synced` ({nick: (defs, updates)}) to every schematic's
    lib_symbols entries; `rename(atom)` may also remap the entry's name.
    Returns (lib_id atoms `rename` changed, lib_symbols entries changed)."""
    renamed = entries = 0
    for sch in schematics:
        text = _read(sch)
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        touched = False
        alias = _lib_name_aliases(root)
        for lst in root.iter_lists():
            head = lst.head()
            if head == "lib_id" and lst.children[1:] and rename:
                if rename(editor, lst.children[1]):
                    renamed += 1
                    touched = True
            elif head == "lib_symbols":
                for child in lst.children:
                    if not (child.kind == "list" and child.head() == "symbol"
                            and child.children[1:]):
                        continue
                    if rename and rename(editor, child.children[1]):
                        renamed += 1
                        touched = True
                    entry = child.children[1].value
                    nick, _, name = alias.get(entry, entry).partition(":")
                    if nick in synced:
                        defs, updates = synced[nick]
                        if _sync_cache_entry(editor, text, child, name, defs, updates,
                                             exact=exact):
                            entries += 1
                            touched = True
        if touched:
            write_validated(session, sch, editor.result())
    return renamed, entries


def update_symbols(session, rows):
    """Update project libraries whose symbols no longer match their placed
    symbols (rows with status 'update'): fields changed, added or deleted on
    every placed symbol are changed, added or deleted in the library and in
    the schematics' lib_symbols copies, so *Update Symbols from Library*
    keeps them."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)
    nicks = {r["name"].split(":", 1)[0] for r in rows
             if r["type"] == "symbol" and ":" in r["name"]}
    schematics = glob.glob(os.path.join(project_dir, "*.kicad_sch"))
    placed = _placed_by_library(schematics)
    synced, changed = {}, 0
    for nick in sorted(nicks):
        uri = resolve_lib_uri(project_dir, "sym-lib-table", nick)
        path = resolve(uri) if uri else ""
        if os.path.isfile(path) or os.path.isdir(path):
            defs, updates, n = _sync_library(session, path, placed.get(nick, {}))
            synced[nick] = (defs, updates)
            changed += n
    # Cached copies are made equal to the library, which also repairs those
    # KiCad saved back over an earlier run.
    _, entries = _sync_schematic_caches(session, schematics, synced, exact=True)
    return {"libraries": len(synced), "fields_changed": changed,
            "schematic_copies": entries}


def localize_symbols(session, rows):
    """Localize the libraries of the selected symbols. `rows` are the selected
    scan_items() rows of type 'symbol'.

    Symbols localize per library, not per instance: selecting any symbol of a
    library copies the whole .kicad_sym (so derived symbols keep their 'extends'
    parents) and remaps every instance of that library plus its lib_symbols
    cache entries together. Splitting one library across two nicknames would
    leave the embedded symbol cache inconsistent, so we keep it whole.

    A folder library (KiCad 10: one .kicad_sym per symbol, usually named
    *.kicad_symdir) is copied as a folder of its top-level *.kicad_sym files,
    which is what KiCad reads. A library whose source can't be found is left
    alone rather than remapped to a copy that doesn't exist."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)

    nicks = set()
    for r in rows:
        if r["type"] == "symbol" and ":" in r["name"]:
            nicks.add(r["name"].split(":", 1)[0])
    if not nicks:
        return {"libraries": 0, "files_copied": 0, "remapped": 0}

    src_file = {}   # nick -> resolved source .kicad_sym file or library folder
    for nick in nicks:
        uri = resolve_lib_uri(project_dir, "sym-lib-table", nick)
        if uri:
            path = resolve(uri)
            if os.path.isfile(path) or os.path.isdir(path):
                src_file[nick] = path
    if not src_file:
        return {"libraries": 0, "files_copied": 0, "remapped": 0}

    entries = {}
    local_libs = {}         # nick -> the local copy
    copied = 0
    for nick, sfile in src_file.items():
        local_nick = nick + LOCAL_SUFFIX
        if os.path.isdir(sfile):
            ext = os.path.splitext(sfile.rstrip("/\\"))[1] or ".kicad_symdir"
            local_name = local_nick + ext
            local_lib = os.path.join(project_dir, LOCAL_LIB_DIRNAME, local_name)
            if not os.path.exists(local_lib):
                session.mkdir(local_lib)
                for f in sorted(os.listdir(sfile)):
                    src = os.path.join(sfile, f)
                    if f.lower().endswith(".kicad_sym") and os.path.isfile(src):
                        dst = os.path.join(local_lib, f)
                        session.wrote_new_file(dst)
                        shutil.copy2(src, dst)
                        session.written(dst)
                        copied += 1
        else:
            local_name = local_nick + ".kicad_sym"
            local_lib = os.path.join(project_dir, LOCAL_LIB_DIRNAME, local_name)
            session.mkdir(os.path.dirname(local_lib))
            if not os.path.isfile(local_lib):
                session.wrote_new_file(local_lib)
                shutil.copy2(sfile, local_lib)
                session.written(local_lib)
                copied += 1
        entries[local_nick] = "${{KIPRJMOD}}/{}/{}".format(LOCAL_LIB_DIRNAME, local_name)
        local_libs[nick] = local_lib

    # The local copy follows the placed symbols: fields changed, added or
    # deleted on every one of them (symbol_updates).
    schematics = glob.glob(os.path.join(project_dir, "*.kicad_sch"))
    placed = _placed_by_library(schematics)
    synced = {}             # nick -> (defs, updates)
    fields_changed = 0
    for nick, lib in local_libs.items():
        defs, updates, n = _sync_library(session, lib, placed.get(nick, {}))
        synced[nick] = (defs, updates)
        fields_changed += n

    def remap(editor, atom):
        if atom.kind != "atom" or ":" not in atom.value:
            return False
        nick, name = atom.value.split(":", 1)
        if nick not in src_file:
            return False
        editor.replace_atom(atom, "{}:{}".format(nick + LOCAL_SUFFIX, name),
                            force_quote=True)
        return True

    remapped, _ = _sync_schematic_caches(session, schematics, synced, rename=remap)

    add_lib_entries(session, "sym", entries)
    return {"libraries": len(entries), "files_copied": copied, "remapped": remapped,
            "fields_changed": fields_changed}


# --- dispatcher --------------------------------------------------------------

def apply_selection(session, rows):
    """Localize the given selection of scan_items() rows. Symbols and footprints
    are file-level; models are handled here too (file-level) unless the caller
    localizes them live over IPC instead. Returns {scope: summary}."""
    result = {}
    syms = [r for r in rows if r["type"] == "symbol" and r["status"] != "update"]
    stale = [r for r in rows if r["type"] == "symbol" and r["status"] == "update"]
    fps = [r for r in rows if r["type"] == "footprint"]
    libmodels = [r for r in rows if r["type"] == "model" and r["key"][0] == "libmodel"]
    rows = [r for r in rows if r["key"][0] != "libmodel"]
    models = [r for r in rows if r["type"] == "model" and r["status"] != "embedded"]
    embedded = [r for r in rows if r["type"] == "model" and r["status"] == "embedded"]
    # Footprints first: they may rewrite the schematic Footprint fields that
    # the symbol libraries are then brought in line with.
    if fps:
        result["footprints"] = localize_footprints(session, fps)
    if syms:
        result["symbols"] = localize_symbols(session, syms)
    if stale:
        result["symbol_updates"] = update_symbols(session, stale)
    if models:
        result["models"] = localize_models(session, models)
    if embedded:
        result["embedded"] = localize_embedded_models(session, embedded)
    if libmodels:
        result["library_models"] = localize_library_models(session, libmodels)
    nested = [r for r in models + libmodels if r["status"] == "nested"]
    if nested:
        result["subfolders"] = _drop_unused_nested(session, nested)
    return result


def _drop_unused_nested(session, rows):
    """After models were moved up out of packages3D subfolders: remove the
    subfolder copies (and their twins) that no model references any more,
    backing each up so Revert puts it back, then any emptied subfolder."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)
    norm = lambda p: os.path.normcase(os.path.abspath(p))  # noqa: E731
    used = set()
    for path in _model_holders(project_dir):
        for m in sexpr.parse(_read(path)).iter_lists():
            if m.head() == "model" and m.children[1:] and m.children[1].kind == "atom":
                used.add(norm(resolve(m.children[1].value)))
    removed, dirs = 0, set()
    for r_ in rows:
        for src in _model_sources(resolve(r_["source"])):
            if os.path.isfile(src) and norm(src) not in used:
                session.backup(src)
                os.remove(src)
                removed += 1
                dirs.add(os.path.dirname(src))
    for d in sorted(dirs, key=len, reverse=True):
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    return {"files_removed": removed}
