#!/usr/bin/env python
"""org2docx: export an Org document to a styled DOCX (and PDF) with pandoc and LibreOffice.

Usage:
  python org2docx.py INPUT.org [-o OUTDIR] [--profile NAME] [--pdf/--no-pdf] [--toc/--no-toc]
                     [--name BASENAME] [--resource-path DIR] [--keep-intermediate]
  python org2docx.py --list-profiles

Pipeline:
  1. pandoc -f org -t docx with the profile's reference.docx and org2docx.lua (heading numbers, appendix letters,
     numbered table/figure captions, PlantUML -> PNG, TOC field);
  2. DOCX post-processing: heading bookmarks moved into paragraphs, tables at full text width with content-based
     column widths, borders, shaded header row, rows that never split across pages, smaller code in tables and code
     blocks, tight list items at body size;
  3. LibreOffice (headless, private user profile): table of contents and fields refreshed, DOCX re-saved, PDF export.

Settings: profiles/<name>/profile.json; overridden per document by the header keywords
  #+DOCX_PROFILE: name      #+DOCX_TOC: t|nil      #+DOCX_PDF: t|nil
  #+DOCX_CAPTION_TABLE: w   #+DOCX_CAPTION_FIGURE: w   #+DOCX_TOC_TITLE: text
  #+LANGUAGE: xx (selects the profile's by_language overrides)   #+OPTIONS: num:… toc:…
and by the command line (highest priority).

This script reads the Org file with pandoc's Org reader, which does not implement every Org export feature
(#+MACRO, #+BIND, custom link types, :noexport: tags, COMMENT subtrees, some #+INCLUDE forms).  The Emacs command
org2docx-export first expands the buffer with Org's own exporter and then calls this script on the result.

Exit status: 0 success, 1 conversion failure, 2 usage error or missing dependency.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
PROFILES = HERE / "profiles"
LUA = HERE / "org2docx.lua"
DEFAULT_PROFILE = "formal"
MIN_PANDOC = (3, 1)


class Fail(Exception):
    """Conversion error: message for the user, exit status."""

    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def log(msg):
    print(f"org2docx: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------------- settings

TRUE = {"t", "true", "yes", "on", "1"}
FALSE = {"nil", "false", "no", "off", "0"}


def as_bool(value, keyword):
    v = value.strip().lower()
    if v in TRUE:
        return True
    if v in FALSE:
        return False
    raise Fail(f"#+{keyword}: expected t or nil, got {value!r}", 2)


def read_keywords(path: pathlib.Path) -> dict:
    """Org keywords (#+KEY: value) outside blocks; the first occurrence wins, except OPTIONS (joined)."""
    kw, in_block = {}, False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"\s*#\+begin_", line, re.I):
            in_block = True
        elif re.match(r"\s*#\+end_", line, re.I):
            in_block = False
        elif not in_block:
            m = re.match(r"\s*#\+([A-Za-z_][\w-]*):\s?(.*)$", line)
            if m:
                key, val = m.group(1).upper(), m.group(2).strip()
                if key == "OPTIONS":
                    kw["OPTIONS"] = (kw.get("OPTIONS", "") + " " + val).strip()
                else:
                    kw.setdefault(key, val)
    return kw


def org_options(options: str) -> dict:
    return dict(m.groups() for m in re.finditer(r"([\w^*<|:'\\-]+?):(\S+)", options or ""))


def profile_names():
    return sorted(p.name for p in PROFILES.iterdir() if (p / "profile.json").exists()) if PROFILES.is_dir() else []


def load_profile(name: str) -> tuple[dict, pathlib.Path]:
    d = PROFILES / name
    if not (d / "profile.json").exists():
        raise Fail(f"unknown profile {name!r}; available: {', '.join(profile_names()) or 'none'}", 2)
    cfg = json.loads((d / "profile.json").read_text(encoding="utf-8"))
    ref = d / "reference.docx"
    if not ref.exists():
        raise Fail(f"{ref} is missing; build it with: python {HERE / 'make_reference.py'} {name}", 2)
    return cfg, ref


def resolve_settings(args, src: pathlib.Path) -> tuple[dict, pathlib.Path, str]:
    kw = read_keywords(src)
    name = args.profile or kw.get("DOCX_PROFILE") or args.default_profile or DEFAULT_PROFILE
    cfg, ref = load_profile(name)
    s = {k: v for k, v in cfg.items() if k not in ("reference", "by_language", "description")}
    lang = (kw.get("LANGUAGE") or "").split("-")[0].split("_")[0].lower()
    s.update(cfg.get("by_language", {}).get(lang, {}))

    opts = org_options(kw.get("OPTIONS", ""))
    if "num" in opts:
        v = opts["num"]
        s["number_headings"] = int(v) if v.isdigit() else v.lower() != "nil"
    if "toc" in opts:
        v = opts["toc"]
        s["toc"] = v.lower() != "nil"
        if v.isdigit():
            s["toc_levels"] = int(v)
            s["toc"] = int(v) > 0
    if "DOCX_TOC" in kw:
        s["toc"] = as_bool(kw["DOCX_TOC"], "DOCX_TOC")
    if "DOCX_PDF" in kw:
        s["pdf"] = as_bool(kw["DOCX_PDF"], "DOCX_PDF")
    for key in ("CAPTION_TABLE", "CAPTION_FIGURE", "TOC_TITLE"):
        if f"DOCX_{key}" in kw:
            s[key.lower()] = kw[f"DOCX_{key}"]
    if args.toc is not None:
        s["toc"] = args.toc
    if args.pdf is not None:
        s["pdf"] = args.pdf
    return s, ref, name


# --------------------------------------------------------------------------------------------- dependencies

def check_pandoc():
    exe = shutil.which("pandoc")
    if not exe:
        raise Fail("pandoc is not installed or not on PATH (pandoc >= 3.1 is required)", 2)
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"pandoc\S*\s+(\d+)\.(\d+)", out)
    if m and (int(m.group(1)), int(m.group(2))) < MIN_PANDOC:
        raise Fail(f"pandoc {m.group(1)}.{m.group(2)} is too old; >= {MIN_PANDOC[0]}.{MIN_PANDOC[1]} is required", 2)
    return exe


def find_soffice():
    for name in ("soffice", "libreoffice"):
        exe = shutil.which(name)
        if exe:
            return exe
    for p in ("/usr/lib/libreoffice/program/soffice", "/opt/libreoffice/program/soffice",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if os.access(p, os.X_OK):
            return p
    return None


# --------------------------------------------------------------------------------------------- pandoc

def run_pandoc(pandoc, src, out, ref, settings, workdir, resource_dirs):
    env = dict(os.environ, ORG2DOCX_SETTINGS=json.dumps(settings, ensure_ascii=False))
    cmd = [pandoc, "-f", "org", "-t", "docx", "--reference-doc", str(ref), "--lua-filter", str(LUA),
           "--syntax-highlighting=none", "--resource-path", os.pathsep.join(str(d) for d in resource_dirs),
           "-o", str(out), str(src)]
    r = subprocess.run(cmd, cwd=src.parent, env=env, capture_output=True, text=True)
    for line in r.stderr.splitlines():
        log(f"pandoc: {line}")
    if r.returncode:
        raise Fail(f"pandoc failed with exit status {r.returncode}")


# --------------------------------------------------------------------------------------------- DOCX post-processing

def read_docx(path):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def write_docx(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in files.items():
            z.writestr(n, data)


def text_width(ref: pathlib.Path) -> int:
    """Text block width of the reference document in twips (page width minus left and right margins)."""
    xml = read_docx(ref)["word/document.xml"].decode()
    sect = re.findall(r"<w:sectPr.*?</w:sectPr>", xml, re.S)[-1]
    page = float(re.search(r'<w:pgSz [^>]*w:w="([\d.]+)"', sect).group(1))
    left = float(re.search(r'<w:pgMar [^>]*w:left="([\d.]+)"', sect).group(1))
    right = float(re.search(r'<w:pgMar [^>]*w:right="([\d.]+)"', sect).group(1))
    return int(page - left - right)


def fix_bookmarks(xml: str) -> str:
    """Pandoc writes heading bookmarks at body level; LibreOffice drops those that follow a table.
    Move each one into the next paragraph as a zero-length bookmark."""
    ends = []

    def move(m):
        start, bid, pbody = m.group(1), m.group(2), m.group(3)
        ends.append(bid)
        ppr = re.match(r"(<w:pPr>.*?</w:pPr>|<w:pPr\s*/>)?", pbody, re.S).group(0) or ""
        return f"<w:p>{ppr}{start}<w:bookmarkEnd w:id=\"{bid}\"/>{pbody[len(ppr):]}"

    xml = re.sub(r'(<w:bookmarkStart w:id="(\d+)" w:name="[^"]*"\s*/>)\s*<w:p>(.*?</w:p>)', move, xml, flags=re.S)
    for bid in ends:
        if xml.count(f'<w:bookmarkEnd w:id="{bid}"') > 1:
            xml = xml.replace(f'<w:bookmarkEnd w:id="{bid}" />', "", 1)
    return xml


def _cell_runs(cell):
    """[(text, is_mono)] of a table cell."""
    out = []
    for r in re.findall(r"<w:r>.*?</w:r>", cell, re.S):
        mono = 'w:val="VerbatimChar"' in r
        t = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", r))
        if t:
            out.append((t, mono))
    return out


def column_widths(rows, ncol, width, tcfg):
    """Minimum width = longest unbreakable token; the rest is shared in proportion to the text length."""
    prop, mono_w, pad = tcfg.get("cell_char_twips", 125), tcfg.get("cell_mono_twips", 120), tcfg.get("cell_pad_twips", 260)
    need = [0.0] * ncol
    pref = [0.0] * ncol
    for r in rows:
        cells = re.findall(r"<w:tc>.*?</w:tc>", r, re.S)
        for i, c in enumerate(cells[:ncol]):
            total = 0.0
            for t, mono in _cell_runs(c):
                cw = mono_w if mono else prop
                total += len(t) * cw
                for tok in t.split():
                    need[i] = max(need[i], len(tok) * cw + pad)
            pref[i] = max(pref[i], min(total + pad, width * 0.7))
    need = [max(n, 900) for n in need]
    if sum(need) >= width:
        return [int(width * n / sum(need)) for n in need]
    extra = width - sum(need)
    want = [max(p - n, 0) for p, n in zip(pref, need)]
    tw = sum(want) or 1
    return [int(n + extra * w / tw) for n, w in zip(need, want)]


def postprocess(xml: str, width: int, settings: dict) -> tuple[str, int]:
    """Tables: full width, content-based column widths, borders, shaded header, rows kept on one page, smaller inline
    code. Code blocks: profile size. Tight lists outside tables: ListBody (body text size)."""
    tcfg = settings.get("table", {})
    color, bsz = tcfg.get("border_color", "000000"), tcfg.get("border_size", 8)
    fill = tcfg.get("header_fill", "EFEFEF")
    code_hp = int(round(float(tcfg.get("inline_code_size_pt", 10)) * 2))
    block_hp = int(round(float(settings.get("code_block_size_pt", 8)) * 2))
    borders = ('<w:tblBorders>' + "".join(f'<w:{b} w:val="single" w:sz="{bsz}" w:space="0" w:color="{color}"/>'
               for b in ("top", "left", "bottom", "right", "insideH", "insideV")) + '</w:tblBorders>'
               '<w:tblCellMar><w:top w:w="60" w:type="dxa"/><w:left w:w="100" w:type="dxa"/>'
               '<w:bottom w:w="60" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar>')
    count = 0

    def one(tbl):
        nonlocal count
        if "<w:tbl>" in tbl[5:]:
            return tbl  # nested tables (e.g. a raw-openxml title page) are left as is
        rows = re.findall(r"<w:tr>.*?</w:tr>", tbl, re.S)
        ncol = len(re.findall(r"<w:gridCol ", tbl))
        if not rows or ncol == 0:
            return tbl
        count += 1
        widths = column_widths(rows, ncol, width, tcfg)
        grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths) + "</w:tblGrid>"
        tbl = re.sub(r"<w:tblGrid>.*?</w:tblGrid>", grid, tbl, count=1, flags=re.S)
        tbl = re.sub(r'<w:tblW [^>]*/>',
                     f'<w:tblW w:type="dxa" w:w="{width}"/><w:tblLayout w:type="fixed"/>{borders}', tbl, count=1)
        tbl = re.sub(r'<w:jc w:val="left" /><w:tblInd [^>]*/>', '', tbl, count=1)
        head = re.search(r"<w:tr><w:trPr><w:tblHeader[^>]*/>.*?</w:tr>", tbl, re.S)
        if head:
            shaded = head.group(0).replace(
                "<w:tcPr />", f'<w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/></w:tcPr>')
            tbl = tbl.replace(head.group(0), shaded, 1)
        tbl = tbl.replace("<w:trPr>", "<w:trPr><w:cantSplit/>")
        tbl = re.sub(r"<w:tr>(?!<w:trPr>)", "<w:tr><w:trPr><w:cantSplit/></w:trPr>", tbl)
        tbl = tbl.replace('<w:rStyle w:val="VerbatimChar" />',
                          f'<w:rStyle w:val="VerbatimChar" /><w:sz w:val="{code_hp}" /><w:szCs w:val="{code_hp}" />')
        return tbl

    parts = re.split(r'(<w:tbl><w:tblPr><w:tblStyle w:val="Table" />.*?</w:tbl>)', xml, flags=re.S)
    for k, part in enumerate(parts):
        if k % 2:
            parts[k] = one(part)
        else:
            parts[k] = part.replace('<w:pStyle w:val="Compact" />', '<w:pStyle w:val="ListBody" />')
    xml = "".join(parts)
    xml = re.sub(r'(<w:p><w:pPr><w:pStyle w:val="SourceCode" />.*?</w:p>)',
                 lambda m: m.group(1).replace(
                     '<w:rStyle w:val="VerbatimChar" />',
                     f'<w:rStyle w:val="VerbatimChar" /><w:sz w:val="{block_hp}" /><w:szCs w:val="{block_hp}" />'),
                 xml, flags=re.S)
    return xml, count


# --------------------------------------------------------------------------------------------- LibreOffice

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lo_uno(soffice, docx_in, docx_out, pdf_out, timeout=180):
    """Open in LibreOffice through UNO, refresh indexes (TOC) and fields twice, save DOCX and optionally PDF."""
    import uno
    from com.sun.star.beans import PropertyValue

    def prop(name, value):
        p = PropertyValue()
        p.Name, p.Value = name, value
        return p

    profile = pathlib.Path(tempfile.mkdtemp(prefix="org2docx-lo-"))
    port = free_port()
    proc = subprocess.Popen(
        [soffice, "--headless", "--invisible", "--nologo", "--norestore", "--nolockcheck", "--nodefault",
         f"-env:UserInstallation={profile.as_uri()}", f"--accept=socket,host=127.0.0.1,port={port};urp;"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    desktop = None
    try:
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        ctx, deadline = None, time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise Fail(f"LibreOffice exited during start-up (status {proc.returncode})")
            try:
                ctx = resolver.resolve(f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext")
                break
            except Exception:
                time.sleep(0.5)
        if ctx is None:
            raise Fail("LibreOffice did not accept a connection within 60 s")
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(str(docx_in)), "_blank", 0,
                                           (prop("Hidden", True),))
        if doc is None:
            raise Fail("LibreOffice could not open the intermediate DOCX")
        for _ in range(2):  # the TOC itself changes pagination
            idx = doc.getDocumentIndexes()
            for i in range(idx.getCount()):
                idx.getByIndex(i).update()
            doc.getTextFields().refresh()
        doc.storeToURL(uno.systemPathToFileUrl(str(docx_out)), (prop("FilterName", "MS Word 2007 XML"),))
        if pdf_out:
            doc.storeToURL(uno.systemPathToFileUrl(str(pdf_out)),
                           (prop("FilterName", "writer_pdf_Export"),
                            prop("FilterData", uno.Any("[]com.sun.star.beans.PropertyValue", tuple([
                                prop("ExportBookmarks", True), prop("ExportLinksRelativeFsys", False),
                                prop("ConvertOOoTargetToPDFTarget", True),
                                prop("ExportBookmarksToPDFDestination", True),
                            ])))))
        doc.close(True)
    finally:
        try:
            if desktop is not None:
                desktop.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def lo_convert(soffice, docx_in, docx_out, pdf_out, workdir):
    """Fallback without the UNO bridge: plain --convert-to (fields and the TOC are not refreshed)."""
    profile = pathlib.Path(tempfile.mkdtemp(prefix="org2docx-lo-"))
    try:
        shutil.copyfile(docx_in, docx_out)
        if pdf_out:
            out = workdir / "lo-pdf"
            out.mkdir(exist_ok=True)
            r = subprocess.run([soffice, "--headless", "--norestore", f"-env:UserInstallation={profile.as_uri()}",
                                "--convert-to", "pdf", "--outdir", str(out), str(docx_in)],
                               capture_output=True, text=True, timeout=600)
            produced = out / (pathlib.Path(docx_in).stem + ".pdf")
            if r.returncode or not produced.exists():
                raise Fail(f"LibreOffice PDF conversion failed: {r.stderr.strip() or r.stdout.strip()}")
            shutil.move(str(produced), pdf_out)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


# --------------------------------------------------------------------------------------------- main

def has_plantuml(src: pathlib.Path) -> bool:
    return bool(re.search(r"^\s*#\+begin_src\s+(plantuml|puml)\b", src.read_text(encoding="utf-8", errors="replace"),
                          re.I | re.M))


def convert(args) -> int:
    src = pathlib.Path(args.input).expanduser().resolve()
    if not src.is_file():
        raise Fail(f"input file not found: {src}", 2)
    settings, ref, profile = resolve_settings(args, src)
    name = args.name or src.stem
    outdir = pathlib.Path(args.outdir).expanduser().resolve() if args.outdir else src.parent
    outdir.mkdir(parents=True, exist_ok=True)
    docx, pdf = outdir / f"{name}.docx", outdir / f"{name}.pdf"

    pandoc = check_pandoc()
    need_lo = settings.get("pdf") or settings.get("toc")
    soffice = find_soffice() if need_lo else None
    if need_lo and not soffice:
        raise Fail("LibreOffice (soffice) is not installed or not on PATH; it is needed for the PDF and the table "
                   "of contents. Install it, or run with --no-pdf --no-toc for a DOCX only.", 2)
    settings["plantuml"] = bool(shutil.which("plantuml"))
    if has_plantuml(src) and not settings["plantuml"]:
        log("WARNING: plantuml is not installed; PlantUML blocks stay as source code")

    if args.keep_intermediate:
        workdir = outdir / f"{name}.org2docx-build"
        workdir.mkdir(exist_ok=True)
    else:
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="org2docx-"))
    settings["diagram_dir"] = str(workdir / "diagrams")
    (workdir / "diagrams").mkdir(exist_ok=True)
    log(f"profile {profile}; toc {'on' if settings.get('toc') else 'off'}; pdf {'on' if settings.get('pdf') else 'off'}")

    try:
        raw = workdir / f"{name}.pandoc.docx"
        resource = [src.parent] + [pathlib.Path(p).expanduser().resolve() for p in args.resource_path] + [workdir]
        run_pandoc(pandoc, src, raw, ref, settings, workdir, resource)

        files = read_docx(raw)
        xml = fix_bookmarks(files["word/document.xml"].decode())
        xml, ntables = postprocess(xml, text_width(ref), settings)
        files["word/document.xml"] = xml.encode()
        write_docx(raw, files)

        if not need_lo:
            shutil.copyfile(raw, docx)
            if pdf.exists():
                log(f"note: {pdf} is from an earlier run (PDF export is off)")
        else:
            pdf_target = pdf if settings.get("pdf") else None
            try:
                import uno  # noqa: F401
                have_uno = True
            except ImportError:
                have_uno = False
                log(f"WARNING: the LibreOffice Python bridge (import uno) is not available to {sys.executable}; "
                    "falling back to soffice --convert-to: the table of contents is not refreshed")
            if have_uno:
                lo_uno(soffice, raw, docx, pdf_target)
            else:
                lo_convert(soffice, raw, docx, pdf_target, workdir)

        if "@startuml" in xml:
            log("WARNING: PlantUML source remains in the document (plantuml missing or failed)")
        anchors = set(re.findall(r'w:name="([^"]+)"', xml))
        missing = sorted(set(re.findall(r'w:anchor="([^"]+)"', xml)) - anchors)
        if missing:
            log(f"WARNING: {len(missing)} internal links without a target: {missing[:10]}")
        print(f"DOCX: {docx}")
        if settings.get("pdf"):
            pages = ""
            if shutil.which("pdfinfo"):
                info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
                m = re.search(r"Pages:\s+(\d+)", info)
                pages = f" ({m.group(1)} pages)" if m else ""
            print(f"PDF: {pdf}{pages}")
        log(f"{ntables} tables formatted")
        return 0
    finally:
        if not args.keep_intermediate:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            log(f"intermediate files kept in {workdir}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="org2docx", description=__doc__.split("\n\n")[0],
                                 epilog="See README.md for profiles and Org keywords.")
    ap.add_argument("input", nargs="?", help="Org file")
    ap.add_argument("-o", "--outdir", help="output directory (default: the input file's directory)")
    ap.add_argument("--name", help="output base name (default: the input file name without .org)")
    ap.add_argument("--profile", help="profile name; overrides #+DOCX_PROFILE")
    ap.add_argument("--default-profile", help=f"profile used when the file has no #+DOCX_PROFILE "
                                              f"(default: {DEFAULT_PROFILE})")
    ap.add_argument("--pdf", action=argparse.BooleanOptionalAction, default=None, help="export a PDF as well")
    ap.add_argument("--toc", action=argparse.BooleanOptionalAction, default=None, help="insert a table of contents")
    ap.add_argument("--resource-path", action="append", default=[],
                    help="extra directory to search for images (repeatable)")
    ap.add_argument("--keep-intermediate", action="store_true",
                    help="keep the pandoc DOCX and rendered diagrams in OUTDIR/<name>.org2docx-build/")
    ap.add_argument("--list-profiles", action="store_true", help="list the available profiles and exit")
    args = ap.parse_args(argv)
    try:
        if args.list_profiles:
            for n in profile_names():
                cfg = json.loads((PROFILES / n / "profile.json").read_text(encoding="utf-8"))
                print(f"{n}\t{cfg.get('description', '')}")
            return 0
        if not args.input:
            ap.print_usage(sys.stderr)
            raise Fail("an input .org file is required", 2)
        return convert(args)
    except Fail as e:
        log(f"ERROR: {e}")
        return e.code
    except subprocess.TimeoutExpired as e:
        log(f"ERROR: timed out: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 - report any other failure as a conversion error
        log(f"ERROR: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
