"""Collect the symbols a project's schematic actually uses into ONE project
symbol library, libs/<NICK>.kicad_sym -- or,
with --footprints, the footprints its boards place into ONE footprint
library, libs/<NICK>.pretty (schematic Footprint fields follow; a footprint
whose library file is gone is rebuilt from the board).

    python tools/onelib.py <project_dir> [--name NICK] [--root SHEET] [--apply] [--remove-old]

Without --apply it only prints the plan. With --apply (KiCad closed, project
saved and committed first):

- Every placed symbol in the schematic hierarchy (root sheet and the sheets
  it reaches) is collected, with any 'extends' parents. Each definition comes
  from the library sym-lib-table names for it (project or global, single file
  or folder); a symbol no library has is taken from the schematic's own
  lib_symbols cache copy.
- Same name from two libraries: identical -> one symbol; different -> the
  one with fewer placed instances is renamed <name>_<library>.
- A user field deleted from every placed symbol of a symbol (an LCSC
  number, say) is left out of the library and the schematic's cached copy,
  so *Update Symbols from Library* doesn't bring it back. Mandatory fields,
  ki_* properties and attributes such as DNP are never touched.
- Symbol names are kept as they are (one file holds them all). Footprint
  names, which are file names, are made portable: plain ASCII that Windows
  and macOS accept (Ω -> ohm, ± -> +-, µ -> u, ° -> deg, other non-ASCII
  reduced to ASCII, / \\ : | -> -, ? -> _, * -> x, < > -> lt gt, trailing
  space/dot dropped, reserved names get "_"); names that then clash only by
  case: lower-case 'm' -> 'milli', upper 'M' -> 'MEG' (100mohm / 100Mohm).
- kicad-cli writes libs/<NICK>.kicad_sym; the hierarchy's lib_id and
  lib_symbols names are rewritten to NICK:<name>; sym-lib-table gets NICK and
  loses the project-local (${KIPRJMOD}) libraries that were merged.
- --remove-old also deletes those merged project-local library files.

Sheets outside the hierarchy are not touched (and will no longer resolve if
they used a removed library). Value fields are not changed, only names.
"""
import argparse
import collections
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins"))
import portability  # noqa: E402
import sexpr  # noqa: E402

_WORDS = {"Ω": "ohm", "Ω": "ohm", "±": "+-", "µ": "u", "μ": "u", "°": "deg"}
_CHARS = {"/": "-", "\\": "-", ":": "-", "|": "-", "<": "lt", ">": "gt",
          '"': "", "?": "_", "*": "x"}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *("COM%d" % i for i in range(1, 10)),
             *("LPT%d" % i for i in range(1, 10))}


def portable(n):
    for a, b in _WORDS.items():
        n = n.replace(a, b)
    n = "".join(_CHARS.get(c, c) for c in n)
    n = "".join(c for c in unicodedata.normalize("NFKD", n) if 32 <= ord(c) < 128)
    n = n.rstrip(" .")
    if n.split(".")[0].upper() in _RESERVED:
        n += "_"
    return n


def unclash_case(n):
    n = re.sub(r"(?<=\d)m(?=ohm|Ω|R|$| )", "milli", n)
    return re.sub(r"(?<=\d)M(?=ohm|Ω|R|$| )", "MEG", n)


def _read(p):
    with open(p, encoding="utf-8", newline="") as fh:
        return fh.read()


def root_sheet(proj, given=None):
    """The root schematic: --root if given, else the one .kicad_pro whose
    .kicad_sch exists."""
    if given:
        return os.path.basename(given)
    roots = [os.path.splitext(os.path.basename(p))[0] + ".kicad_sch"
             for p in glob.glob(os.path.join(proj, "*.kicad_pro"))]
    roots = [r for r in roots if os.path.exists(os.path.join(proj, r))]
    if len(roots) != 1:
        sys.exit("can't tell the root sheet ({}); pass --root".format(", ".join(roots) or "none"))
    return roots[0]


def hierarchy(proj, root):
    seen = []

    def walk(f):
        if f in seen or not os.path.exists(os.path.join(proj, f)):
            return
        seen.append(f)
        for sf in re.findall(r'\(property "Sheetfile" "([^"]+)"', _read(os.path.join(proj, f))):
            walk(sf)
    walk(root)
    return sorted(seen)


def library_symbols(path):
    """{name: (text, parent)} from a .kicad_sym file or a folder library
    ("~" field values of pre-KiCad 10 files emptied: see
    portability._symbol_blocks)."""
    return portability._symbol_blocks(path)


def rename_block(t, old, new):
    if old == new:
        return t
    t = t.replace('(symbol "{}"'.format(old), '(symbol "{}"'.format(new), 1)
    return re.sub(r'\(symbol "{}_(\d+_\d+)"'.format(re.escape(old)),
                  lambda m: '(symbol "{}_{}"'.format(new, m.group(1)), t)


def plan(proj, nick, root):
    sheets = hierarchy(proj, root)
    used = collections.Counter()
    cache = {}
    placed = portability.placed_fields([os.path.join(proj, f) for f in sheets])
    for f in sheets:
        text = _read(os.path.join(proj, f))
        root = sexpr.parse(text)
        for s in root.find_all("symbol"):
            lid = s.find("lib_id")
            if lid and lid.children[1:]:
                used[tuple(lid.children[1].value.split(":", 1))] += 1
        ls = root.find("lib_symbols")
        if ls:
            for s in ls.find_all("symbol"):
                full = s.children[1].value
                if ":" in full:
                    cache.setdefault(tuple(full.split(":", 1)), text[s.start:s.end])

    resolve = portability.make_resolver(proj)
    libs, lib_paths = {}, {}
    for n in {k[0] for k in used}:
        uri = portability.resolve_lib_uri(proj, "sym-lib-table", n)
        path = resolve(uri) if uri else None
        if path and os.path.exists(path):
            libs[n] = library_symbols(path)
            lib_paths[n] = (uri, path)

    need, todo = {}, list(used)
    while todo:
        k = todo.pop()
        if k in need:
            continue
        if k[1] in libs.get(k[0], {}):
            txt, parent = libs[k[0]][k[1]]
            need[k] = (txt, "library")
            if parent:
                todo.append((k[0], parent))
        elif k in cache:
            need[k] = (cache[k].replace('(symbol "{}:{}"'.format(*k),
                                        '(symbol "{}"'.format(k[1]), 1), "schematic cache")
        else:
            sys.exit("no definition anywhere for {}:{}".format(*k))

    # Fields deleted from every placed symbol are left out of the library
    # (and the cache), per library, following 'extends'.
    cache_drop, dropped = {}, {}
    for n in {k[0] for k in need}:
        defs = {}
        for k, (txt, _src) in need.items():
            if k[0] == n:
                s = sexpr.parse(txt)
                ext = s.find("extends")
                defs[k[1]] = (set(portability.user_fields(s)),
                              ext.children[1].value if ext else None)
        drops = portability.dropped_fields(
            defs, {k[1]: placed.get(":".join(k), set()) for k in used if k[0] == n})
        for k in list(need):
            if k[0] != n:
                continue
            txt, src = need[k]
            if drops.get(k[1]):
                dropped[":".join(k)] = sorted(drops[k[1]])
                need[k] = (portability.drop_properties(txt, sexpr.parse(txt), drops[k[1]]), src)
            if k in cache:
                cache_drop[k] = portability.cache_drops(
                    k[1], portability.user_fields(sexpr.parse(cache[k])), defs, drops)

    newname = {k: k[1] for k in need}
    renamed = {}
    by = collections.defaultdict(list)
    for k in need:
        by[k[1]].append(k)
    body = lambda t: re.sub(r'^\(symbol "[^"]*"', "", t)  # noqa: E731
    for name, ks in by.items():
        if len(ks) > 1 and len({body(need[k][0]) for k in ks}) > 1:
            ks.sort(key=lambda k: -used.get(k, 0))
            for k in ks[1:]:
                newname[k] = "{}_{}".format(name, k[0].replace("_local", ""))
    taken = set()
    for k in sorted(newname, key=lambda k: (newname[k] != k[1], newname[k])):
        q, i = newname[k], 1
        while q in taken:
            i += 1
            q = "{}_{}".format(newname[k], i)
        newname[k] = q
        taken.add(q)
    for k in need:
        if newname[k] != k[1]:
            renamed[":".join(k)] = newname[k]
    return sheets, used, need, newname, renamed, lib_paths, cache_drop, dropped


def apply(proj, nick, sheets, need, newname, lib_paths, remove_old, cache_drop=None):
    out = os.path.join(proj, "libs", nick + ".kicad_sym")
    if os.path.exists(out):
        sys.exit(out + " already exists")
    seen, blocks = set(), []
    for k in sorted(need, key=lambda k: newname[k].lower()):
        n = newname[k]
        if n in seen:
            continue
        seen.add(n)
        t = rename_block(need[k][0], k[1], n)
        m = re.search(r'\(extends "([^"]+)"\)', t)
        if m and (k[0], m.group(1)) in newname:
            t = t.replace(m.group(0), '(extends "{}")'.format(newname[(k[0], m.group(1))]))
        blocks.append("\t" + t)
    combined = os.path.join(proj, nick + ".onelib-tmp.kicad_sym")
    with open(combined, "w", encoding="utf-8") as fh:
        fh.write('(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "kicad_symbol_editor")\n'
                 '\t(generator_version "10.0")\n' + "\n".join(blocks) + "\n)\n")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cli = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli") or "kicad-cli"
    subprocess.run([cli, "sym", "upgrade", "--force", "-o", out, combined],
                   check=True, capture_output=True)
    os.remove(combined)

    for f in sheets:
        path = os.path.join(proj, f)
        text = _read(path)
        root = sexpr.parse(text)
        ed = sexpr.Editor(text)
        for s in root.find_all("symbol"):
            lid = s.find("lib_id")
            if lid and lid.children[1:]:
                k = tuple(lid.children[1].value.split(":", 1))
                ed.replace_atom(lid.children[1], "{}:{}".format(nick, newname[k]), force_quote=True)
        ls = root.find("lib_symbols")
        if ls:
            for s in ls.find_all("symbol"):
                k = tuple(s.children[1].value.split(":", 1))
                if k not in newname:
                    continue
                nb = text[s.start:s.end]
                if cache_drop and cache_drop.get(k):
                    nb = portability.drop_properties(nb, sexpr.parse(nb), cache_drop[k])
                nb = nb.replace(
                    '(symbol "{}"'.format(":".join(k)),
                    '(symbol "{}:{}"'.format(nick, newname[k]), 1)
                if newname[k] != k[1]:
                    nb = re.sub(r'\(symbol "{}_(\d+_\d+)"'.format(re.escape(k[1])),
                                lambda m: '(symbol "{}_{}"'.format(newname[k], m.group(1)), nb)
                ed.replace_span(s.start, s.end, nb)
        out = ed.result()
        sexpr.parse(out)
        if out != text:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(out)

    table = os.path.join(proj, "sym-lib-table")
    t = _read(table) if os.path.exists(table) else "(sym_lib_table\n\t(version 7)\n)\n"
    merged_local = {n for n, (uri, _p) in lib_paths.items() if "${KIPRJMOD}" in uri}
    for n in merged_local:
        t = re.sub(r'[ \t]*\(lib \(name "{}"\)[^\n]*\n'.format(re.escape(n)), "", t)
    entry = ('\t(lib (name "{}")(type "KiCad")(uri "${{KIPRJMOD}}/libs/{}.kicad_sym")'
             '(options "")(descr "Project symbols: only those the schematic uses"))\n').format(nick, nick)
    close = t.rstrip().rfind(")")
    t = t[:close] + entry + t[close:]
    with open(table, "w", encoding="utf-8", newline="") as fh:
        fh.write(t)

    removed = []
    if remove_old:
        for n in sorted(merged_local):
            p = lib_paths[n][1]
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.isfile(p):
                os.remove(p)
            removed.append(os.path.relpath(p, proj))
    return len(seen), sorted(merged_local), removed


# --- footprints ---------------------------------------------------------------

def _fp_body(text):
    """A footprint file compared without its name and UUIDs."""
    t = re.sub(r'\(uuid "[^"]*"\)', "", text)
    t = re.sub(r'^\(footprint "[^"]*"', "(footprint", t.lstrip())
    return " ".join(t.split())


def plan_footprints(proj, nick):
    """Footprints the project's boards place: {fpid: (source text, how)},
    the new names, renames, and the project-local libraries they came from."""
    resolve = portability.make_resolver(proj)
    used = collections.Counter()
    boards = {}
    for pcb in sorted(glob.glob(os.path.join(proj, "*.kicad_pcb"))):
        text = _read(pcb)
        root = sexpr.parse(text)
        boards[pcb] = (text, root)
        for fp in root.find_all("footprint"):
            if fp.children[1:] and ":" in (fp.children[1].value or ""):
                used[fp.children[1].value] += 1
    need, lib_paths = {}, {}
    for fid in used:
        lib, name = fid.split(":", 1)
        uri = portability.resolve_lib_uri(proj, "fp-lib-table", lib)
        path = os.path.join(resolve(uri), name + ".kicad_mod") if uri else ""
        if uri and os.path.isdir(resolve(uri)):
            lib_paths[lib] = (uri, resolve(uri))
        if path and os.path.isfile(path):
            need[fid] = (_read(path), "library")
        else:                       # library file gone: rebuild from a board copy
            for text, root in boards.values():
                best = portability._best_instances(root).get(fid)
                if best is not None:
                    ver = root.find("version").children[1].value
                    need[fid] = (portability.rebuild_footprint(
                        text, best, ver, portability._embedded_files(root),
                        flip=portability.flip_direction()), "rebuilt from board")
                    break
    newname = {fid: fid.split(":", 1)[1] for fid in need}
    by = collections.defaultdict(list)
    for fid in need:
        by[newname[fid]].append(fid)
    for name, fids in by.items():
        if len(fids) > 1 and len({_fp_body(need[f][0]) for f in fids}) > 1:
            fids.sort(key=lambda f: -used[f])
            for f in fids[1:]:
                newname[f] = "{}_{}".format(name, f.split(":", 1)[0].replace("_local", ""))
    taken = set()
    for fid in sorted(newname, key=lambda f: (newname[f] != portable(newname[f]), newname[f])):
        p, q, i = portable(newname[fid]), None, 1
        q = p
        while q in taken and q != newname[fid]:
            i += 1
            q = "{}_{}".format(p, i)
        newname[fid] = q
        taken.add(q)
    low = collections.defaultdict(set)
    for v in newname.values():
        low[v.lower()].add(v)
    for group in low.values():
        if len(group) > 1:
            for f, v in list(newname.items()):
                if v in group:
                    newname[f] = unclash_case(v)
    renamed = {f: newname[f] for f in need if newname[f] != f.split(":", 1)[1]}
    return boards, used, need, newname, renamed, lib_paths


def apply_footprints(proj, nick, sheets, boards, need, newname, lib_paths, remove_old):
    folder = os.path.join(proj, "libs", nick + ".pretty")
    if os.path.exists(folder):
        sys.exit(folder + " already exists")
    os.makedirs(folder)
    written = set()
    for fid, (text, _how) in need.items():
        n = newname[fid]
        if n in written:
            continue
        written.add(n)
        t = re.sub(r'^(\s*\(footprint\s+)"[^"]*"', lambda m: m.group(1) + '"%s"' % n, text, count=1)
        sexpr.parse(t)
        with open(os.path.join(folder, n + ".kicad_mod"), "w", encoding="utf-8", newline="") as fh:
            fh.write(t)
    new_id = {fid: "{}:{}".format(nick, newname[fid]) for fid in need}
    for pcb, (text, root) in boards.items():
        ed = sexpr.Editor(text)
        for fp in root.find_all("footprint"):
            a = fp.children[1] if fp.children[1:] else None
            if a is not None and a.value in new_id:
                ed.replace_atom(a, new_id[a.value], force_quote=True)
        out = ed.result()
        sexpr.parse(out)
        with open(pcb, "w", encoding="utf-8", newline="") as fh:
            fh.write(out)
    # Footprint fields follow: placed symbols, the schematics' lib_symbols
    # caches and the project's own symbol libraries (a symbol's default
    # footprint, used when it is placed afresh).
    resolve = portability.make_resolver(proj)
    targets = [os.path.join(proj, f) for f in sheets]
    stable = portability.parse_lib_table(os.path.join(proj, "sym-lib-table"))
    for uri in stable.values():
        if "${KIPRJMOD}" in uri:
            p_ = resolve(uri)
            targets += ([os.path.join(p_, x) for x in sorted(os.listdir(p_)) if x.endswith(".kicad_sym")]
                        if os.path.isdir(p_) else [p_])
    for path in targets:
        if not os.path.isfile(path):
            continue
        text = _read(path)
        root = sexpr.parse(text)
        ed = sexpr.Editor(text)
        for c in root.iter_lists():
            if (c.head() == "property" and len(c.children) >= 3
                    and c.children[1].value == "Footprint" and c.children[2].value in new_id):
                ed.replace_atom(c.children[2], new_id[c.children[2].value], force_quote=True)
        out = ed.result()
        if out != text:
            sexpr.parse(out)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(out)
    table = os.path.join(proj, "fp-lib-table")
    t = _read(table) if os.path.exists(table) else "(fp_lib_table\n\t(version 7)\n)\n"
    merged_local = {n for n, (uri, _p) in lib_paths.items() if "${KIPRJMOD}" in uri}
    for n in merged_local:
        t = re.sub(r'[ \t]*\(lib \(name "{}"\)[^\n]*\n'.format(re.escape(n)), "", t)
    entry = ('\t(lib (name "{}")(type "KiCad")(uri "${{KIPRJMOD}}/libs/{}.pretty")'
             '(options "")(descr "Project footprints: only those the board uses"))\n').format(nick, nick)
    close = t.rstrip().rfind(")")
    t = t[:close] + entry + t[close:]
    with open(table, "w", encoding="utf-8", newline="") as fh:
        fh.write(t)
    removed = []
    if remove_old:
        for n in sorted(merged_local):
            p = lib_paths[n][1]
            if os.path.isdir(p) and os.path.abspath(p) != os.path.abspath(folder):
                shutil.rmtree(p)
                removed.append(os.path.relpath(p, proj))
    return len(written), sorted(merged_local), removed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project")
    ap.add_argument("--name", help="library nickname (default: the root sheet's name)")
    ap.add_argument("--root", help="root .kicad_sch (default: found from the .kicad_pro)")
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--remove-old", action="store_true",
                    help="with --apply, delete the merged project-local libraries")
    ap.add_argument("--footprints", action="store_true",
                    help="collect the board's footprints into libs/<NICK>.pretty instead of symbols")
    a = ap.parse_args()
    proj = os.path.abspath(a.project)
    root = root_sheet(proj, a.root)
    nick = a.name or os.path.splitext(root)[0]
    if a.footprints:
        sheets = hierarchy(proj, root)
        boards, used, need, newname, renamed, lib_paths = plan_footprints(proj, nick)
        report = {
            "library": "libs/{}.pretty".format(nick),
            "placed_footprints": sum(used.values()), "unique_used": len(used),
            "footprints": len(set(newname.values())),
            "rebuilt_from_board": sorted(f for f, v in need.items() if v[1] != "library"),
            "renamed": renamed,
        }
        if a.apply:
            n, merged, removed = apply_footprints(proj, nick, sheets, boards, need, newname,
                                                  lib_paths, a.remove_old)
            report.update({"written": n, "table_entries_replaced": merged, "removed": removed})
        print(json.dumps(report, indent=1, ensure_ascii=False))
        return
    sheets, used, need, newname, renamed, lib_paths, cache_drop, dropped = plan(proj, nick, root)
    report = {
        "library": "libs/{}.kicad_sym".format(nick),
        "sheets": sheets,
        "placed_symbols": sum(used.values()), "unique_used": len(used),
        "symbols": len(set(newname.values())),
        "from_schematic_cache": sorted(":".join(k) for k, v in need.items() if v[1] != "library"),
        "renamed": renamed,
        "fields_dropped": dropped,
        "outside_hierarchy": sorted(set(os.path.basename(f) for f in glob.glob(
            os.path.join(proj, "*.kicad_sch"))) - set(sheets)),
    }
    if a.apply:
        n, merged, removed = apply(proj, nick, sheets, need, newname, lib_paths, a.remove_old,
                                   cache_drop)
        report.update({"written": n, "table_entries_replaced": merged, "removed": removed})
    print(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
