"""Collect the symbols a project's schematic actually uses into ONE project
symbol library (a KiCad 10 folder library, one .kicad_sym per symbol).

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
- Names are made portable, since KiCad saves each symbol as <name>.kicad_sym:
  plain ASCII that Windows and macOS accept (Ω -> ohm, ± -> +-, µ -> u,
  ° -> deg, other non-ASCII reduced to ASCII, / \\ : | -> -, ? -> _, * -> x,
  < > -> lt gt, trailing space/dot dropped, reserved names get "_").
- Names that then clash only by case: lower-case 'm' -> 'milli', upper 'M'
  -> 'MEG' (resistor values, e.g. 100mohm / 100Mohm).
- kicad-cli writes libs/<NICK>.kicad_symdir; the hierarchy's lib_id and
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
    """{name: (text, parent)} from a .kicad_sym file or a folder library."""
    files = ([os.path.join(path, f) for f in sorted(os.listdir(path))
              if f.lower().endswith(".kicad_sym")] if os.path.isdir(path) else [path])
    out = {}
    for f in files:
        t = _read(f)
        for s in sexpr.parse(t).find_all("symbol"):
            ext = s.find("extends")
            out[s.children[1].value] = (t[s.start:s.end],
                                        ext.children[1].value if ext else None)
    return out


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
    for k in sorted(newname, key=lambda k: (newname[k] != portable(newname[k]), newname[k])):
        p = portable(newname[k])
        q, i = p, 1
        while q in taken and q != newname[k]:
            i += 1
            q = "{}_{}".format(p, i)
        newname[k] = q
        taken.add(q)
    low = collections.defaultdict(set)
    for v in newname.values():
        low[v.lower()].add(v)
    for group in low.values():
        if len(group) > 1:
            for k, v in list(newname.items()):
                if v in group:
                    newname[k] = unclash_case(v)
    for k in need:
        if newname[k] != k[1]:
            renamed[":".join(k)] = newname[k]
    return sheets, used, need, newname, renamed, lib_paths


def apply(proj, nick, sheets, need, newname, lib_paths, remove_old):
    folder = os.path.join(proj, "libs", nick + ".kicad_symdir")
    if os.path.exists(folder):
        sys.exit(folder + " already exists")
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
    os.makedirs(folder)
    cli = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli") or "kicad-cli"
    subprocess.run([cli, "sym", "upgrade", "--force", "-o", folder, combined],
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
                nb = text[s.start:s.end].replace(
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
    entry = ('\t(lib (name "{}")(type "KiCad")(uri "${{KIPRJMOD}}/libs/{}.kicad_symdir")'
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project")
    ap.add_argument("--name", help="library nickname (default: the root sheet's name)")
    ap.add_argument("--root", help="root .kicad_sch (default: found from the .kicad_pro)")
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--remove-old", action="store_true",
                    help="with --apply, delete the merged project-local libraries")
    a = ap.parse_args()
    proj = os.path.abspath(a.project)
    root = root_sheet(proj, a.root)
    nick = a.name or os.path.splitext(root)[0]
    sheets, used, need, newname, renamed, lib_paths = plan(proj, nick, root)
    report = {
        "library": "libs/{}.kicad_symdir".format(nick),
        "sheets": sheets,
        "placed_symbols": sum(used.values()), "unique_used": len(used),
        "symbols": len(set(newname.values())),
        "from_schematic_cache": sorted(":".join(k) for k, v in need.items() if v[1] != "library"),
        "renamed": renamed,
        "outside_hierarchy": sorted(set(os.path.basename(f) for f in glob.glob(
            os.path.join(proj, "*.kicad_sch"))) - set(sheets)),
    }
    if a.apply:
        n, merged, removed = apply(proj, nick, sheets, need, newname, lib_paths, a.remove_old)
        report.update({"written": n, "table_entries_replaced": merged, "removed": removed})
    print(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
