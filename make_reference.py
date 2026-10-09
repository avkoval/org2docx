#!/usr/bin/env python
"""Build profiles/<name>/reference.docx from the "reference" section of profiles/<name>/profile.json.

Usage:  python make_reference.py                 # rebuild every profile that has a profile.json
        python make_reference.py formal internal # rebuild the named profiles only

The reference document starts from pandoc's own default reference.docx (``pandoc --print-default-data-file
reference.docx``), so every style pandoc emits exists.  On top of it:

  * docDefaults get the body font, size, line spacing, first-line indent and language of the profile;
  * the styles pandoc uses (Normal, Body Text, headings, title block, captions, code, TOC, footnotes ...) are
    redefined from the profile, all based on Normal so they inherit its typography;
  * ``ListBody`` is added: org2docx.py maps pandoc's ``Compact`` (tight list items) outside tables to it, so list
    items use the body text size while ``Compact`` stays the compact table-cell style;
  * page size, margins and a footer with the page number come from the profile;
  * document properties (title, author, company) are left empty.

Only the standard library is used.
"""
import json
import pathlib
import re
import subprocess
import sys
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
PROFILES = HERE / "profiles"

W_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')


def hp(pt):
    """Points to half-points (the unit of w:sz)."""
    return int(round(float(pt) * 2))


def fonts(name):
    return f'<w:rFonts w:ascii="{name}" w:hAnsi="{name}" w:eastAsia="{name}" w:cs="{name}"/>'


def size(pt):
    return f'<w:sz w:val="{hp(pt)}"/><w:szCs w:val="{hp(pt)}"/>'


def spacing(before=0, after=0, line=240):
    return f'<w:spacing w:before="{before}" w:after="{after}" w:line="{line}" w:lineRule="auto"/>'


def para_style(sid, name, ppr="", rpr="", based="Normal", nxt=None, default=False):
    attrs = ' w:default="1"' if default else ' w:customStyle="1"'
    nxt = f'<w:next w:val="{nxt}"/>' if nxt else ""
    based = f'<w:basedOn w:val="{based}"/>' if based else ""
    return (f'<w:style w:type="paragraph"{attrs} w:styleId="{sid}"><w:name w:val="{name}"/>'
            f'{based}{nxt}<w:qFormat/><w:pPr>{ppr}</w:pPr><w:rPr>{rpr}</w:rPr></w:style>')


def char_style(sid, name, rpr):
    return (f'<w:style w:type="character" w:customStyle="1" w:styleId="{sid}"><w:name w:val="{name}"/>'
            f'<w:basedOn w:val="DefaultParagraphFont"/><w:rPr>{rpr}</w:rPr></w:style>')


def heading_style(level, h, ref):
    ppr = '<w:keepNext/><w:keepLines/>'
    if h.get("page_break_before"):
        ppr += '<w:pageBreakBefore/>'
    ppr += spacing(h.get("before", 240), h.get("after", 120), h.get("line", 240))
    ppr += f'<w:ind w:left="0" w:firstLine="0"/><w:jc w:val="{h.get("align", "left")}"/>'
    ppr += f'<w:outlineLvl w:val="{level - 1}"/>'
    rpr = ""
    if h.get("font"):
        rpr += fonts(h["font"])
    if h.get("bold", True):
        rpr += "<w:b/><w:bCs/>"
    if h.get("italic"):
        rpr += "<w:i/><w:iCs/>"
    if h.get("color"):
        rpr += f'<w:color w:val="{h["color"]}"/>'
    rpr += size(h.get("size_pt") or ref["size_pt"])
    return para_style(f"Heading{level}", f"heading {level}", ppr, rpr, nxt="BodyText")


def build_styles(base_styles: str, ref: dict, text_width: int) -> str:
    body_size = ref["size_pt"]
    line = ref.get("line", 240)
    indent = ref.get("first_line_indent", 0)
    after = ref.get("paragraph_after", 0)
    align = ref.get("align", "both")
    cell = ref.get("table_text_size_pt", body_size)
    cell_line = ref.get("table_line", 240)
    mono = ref.get("mono_font", "Liberation Mono")

    defaults = ('<w:docDefaults><w:rPrDefault><w:rPr>' + fonts(ref["font"]) + size(body_size)
                + f'<w:lang w:val="{ref.get("lang", "en-US")}"/></w:rPr></w:rPrDefault>'
                '<w:pPrDefault><w:pPr>' + spacing(0, after, line)
                + f'<w:ind w:firstLine="{indent}"/></w:pPr></w:pPrDefault></w:docDefaults>')
    styles = re.sub(r"<w:docDefaults>.*?</w:docDefaults>", lambda _m: defaults, base_styles, count=1, flags=re.S)

    body_ppr = spacing(0, after, line) + f'<w:ind w:firstLine="{indent}"/><w:jc w:val="{align}"/>'
    cell_ppr = (spacing(0, 0, cell_line) + '<w:ind w:left="0" w:firstLine="0"/><w:jc w:val="left"/>')
    centered = '<w:ind w:left="0" w:firstLine="0"/><w:jc w:val="center"/>'
    title = ref.get("title", {})
    cap_size = ref.get("caption_size_pt", body_size)

    s = {
        "Normal": para_style("Normal", "Normal", "", "", based=None, default=True),
        "BodyText": para_style("BodyText", "Body Text", body_ppr),
        "FirstParagraph": para_style("FirstParagraph", "First Paragraph", "", based="BodyText", nxt="BodyText"),
        "Compact": para_style("Compact", "Compact", cell_ppr, size(cell)),
        "ListBody": para_style("ListBody", "List Body",
                               spacing(0, ref.get("list_item_after", 0), line)
                               + f'<w:ind w:firstLine="0"/><w:jc w:val="{align}"/>'),
        "Title": para_style("Title", "Title", '<w:keepNext/>' + spacing(title.get("before", 0), 240, 240) + centered,
                            "<w:b/><w:bCs/>" + size(title.get("size_pt", body_size + 6)), nxt="BodyText"),
        "Subtitle": para_style("Subtitle", "Subtitle", '<w:keepNext/>' + spacing(0, 240, 240) + centered,
                               size(title.get("subtitle_size_pt", body_size + 2)), nxt="BodyText"),
        "Author": para_style("Author", "Author", '<w:keepNext/>' + spacing(0, 120, 240) + centered, "",
                             nxt="BodyText"),
        "Date": para_style("Date", "Date", '<w:keepNext/>' + spacing(0, 240, 240) + centered, "", nxt="BodyText"),
        "AbstractTitle": para_style("AbstractTitle", "Abstract Title", '<w:keepNext/>' + spacing(240, 120, 240)
                                    + centered, "<w:b/><w:bCs/>", nxt="Abstract"),
        "Abstract": para_style("Abstract", "Abstract", body_ppr, "", nxt="BodyText"),
        "SourceCode": para_style("SourceCode", "Source Code",
                                 f'<w:shd w:val="clear" w:color="auto" w:fill="{ref.get("code_fill", "F2F2F2")}"/>'
                                 + spacing(60, 120, 240) + '<w:ind w:left="0" w:firstLine="0"/><w:jc w:val="left"/>',
                                 fonts(mono) + size(ref.get("code_size_pt", 8))),
        "VerbatimChar": char_style("VerbatimChar", "Verbatim Char",
                                   fonts(mono) + size(ref.get("inline_code_size_pt", body_size))),
        "Hyperlink": char_style("Hyperlink", "Hyperlink", f'<w:color w:val="{ref.get("link_color", "1F3864")}"/>'),
        "FootnoteReference": char_style("FootnoteReference", "Footnote Reference", '<w:vertAlign w:val="superscript"/>'),
        "Caption": para_style("Caption", "caption",
                              '<w:keepNext/>' + spacing(120, 120, ref.get("caption_line", 276)) + centered,
                              size(cap_size)),
        "TableCaption": para_style("TableCaption", "Table Caption", '<w:keepNext/><w:jc w:val="left"/>', "",
                                   based="Caption"),
        "ImageCaption": para_style("ImageCaption", "Image Caption", '<w:jc w:val="center"/>', "", based="Caption"),
        "CaptionedFigure": para_style("CaptionedFigure", "Captioned Figure", '<w:keepNext/>' + centered),
        "Figure": para_style("Figure", "Figure", centered),
        "BlockText": para_style("BlockText", "Block Text",
                                spacing(60, 120, ref.get("quote_line", 276)) + '<w:ind w:left="567" w:firstLine="0"/>'
                                '<w:pBdr><w:left w:val="single" w:sz="12" w:space="8" w:color="7F7F7F"/></w:pBdr>',
                                size(ref.get("quote_size_pt", body_size)), based="BodyText"),
        "TOCHeading": para_style("TOCHeading", "TOC Heading", spacing(0, 240, 240) + centered,
                                 "<w:b/><w:bCs/>" + size(ref.get("toc_heading_size_pt", body_size)), nxt="BodyText"),
        "DefinitionTerm": para_style("DefinitionTerm", "Definition Term", '<w:keepNext/><w:ind w:firstLine="0"/>',
                                     "<w:b/>"),
        "Definition": para_style("Definition", "Definition", '<w:ind w:left="720" w:firstLine="0"/>'),
        "FootnoteText": para_style("FootnoteText", "footnote text",
                                   spacing(0, 0, 240) + '<w:ind w:firstLine="0"/><w:jc w:val="left"/>',
                                   size(ref.get("footnote_size_pt", 10))),
        "FootnoteBlockText": para_style("FootnoteBlockText", "Footnote Block Text",
                                        '<w:ind w:left="360" w:firstLine="0"/>', "", based="FootnoteText"),
    }
    heads = ref.get("headings", [])
    for lvl in range(1, 10):
        h = heads[min(lvl, len(heads)) - 1] if heads else {}
        s[f"Heading{lvl}"] = heading_style(lvl, h, ref)
    toc_line = ref.get("toc_line", 276)
    for lvl in range(1, 10):
        s[f"TOC{lvl}"] = para_style(
            f"TOC{lvl}", f"toc {lvl}",
            f'<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="{text_width}"/></w:tabs>' + spacing(0, 0, toc_line)
            + f'<w:ind w:left="{(lvl - 1) * 440}" w:firstLine="0"/><w:jc w:val="left"/>', "")

    for sid, xml in s.items():
        pat = r'<w:style [^>]*w:styleId="%s"[^>]*>.*?</w:style>' % sid
        if re.search(pat, styles, re.S):
            styles = re.sub(pat, lambda _m, xml=xml: xml, styles, count=1, flags=re.S)
        else:
            styles = styles.replace("</w:styles>", xml + "</w:styles>")
    # heading/title character styles of pandoc's reference use the theme font and blue colour; neutralise them
    styles = re.sub(r'(<w:style w:type="character"[^>]*w:styleId="(?:Heading\dChar|TitleChar|SubtitleChar)"[^>]*>'
                    r'.*?<w:rPr>).*?(</w:rPr>)', r"\1\2", styles, flags=re.S)
    return styles


def footer(page_number: str) -> bytes:
    if page_number in ("left", "center", "right"):
        p = (f'<w:p><w:pPr><w:ind w:firstLine="0"/><w:jc w:val="{page_number}"/></w:pPr>'
             '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText>'
             '</w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
             '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    else:
        p = "<w:p/>"
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:ftr {W_NS}>{p}</w:ftr>').encode()


def sect_pr(page: dict, first_page_number: bool) -> str:
    refs = '<w:footerReference r:id="rIdFtrDefault" w:type="default"/>'
    if not first_page_number:
        refs += '<w:footerReference r:id="rIdFtrFirst" w:type="first"/>'
    return (f'<w:sectPr>{refs}<w:pgSz w:w="{page["width"]}" w:h="{page["height"]}" w:orient="portrait"/>'
            f'<w:pgMar w:top="{page["top"]}" w:right="{page["right"]}" w:bottom="{page["bottom"]}" '
            f'w:left="{page["left"]}" w:header="{page.get("header", 720)}" w:footer="{page.get("footer", 720)}" '
            f'w:gutter="0"/><w:pgNumType w:start="1"/>{"" if first_page_number else "<w:titlePg/>"}</w:sectPr>')


def pandoc_default_reference() -> bytes:
    try:
        return subprocess.run(["pandoc", "--print-default-data-file", "reference.docx"], check=True,
                              capture_output=True).stdout
    except FileNotFoundError:
        sys.exit("make_reference: pandoc is not installed (needed for its default reference.docx)")


def build(profile_dir: pathlib.Path, base: bytes):
    cfg = json.loads((profile_dir / "profile.json").read_text(encoding="utf-8"))
    ref = cfg["reference"]
    page = ref["page"]
    width = int(page["width"]) - int(page["left"]) - int(page["right"])
    import io
    with zipfile.ZipFile(io.BytesIO(base)) as z:
        files = {n: z.read(n) for n in z.namelist()}

    files["word/styles.xml"] = build_styles(files["word/styles.xml"].decode(), ref, width).encode()

    first_number = bool(ref.get("page_number_on_first_page", True))
    doc = files["word/document.xml"].decode()
    body_start = doc.index("<w:body>") + len("<w:body>")
    doc = doc[:body_start] + "<w:p/>" + sect_pr(page, first_number) + "</w:body></w:document>"
    if "xmlns:r=" not in doc[:3000]:
        doc = doc.replace("<w:document ",
                          '<w:document xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ', 1)
    files["word/document.xml"] = doc.encode()

    files["word/footer1.xml"] = footer(ref.get("page_number", "right"))
    files["word/footer2.xml"] = footer("none")
    rels = files["word/_rels/document.xml.rels"].decode()
    # pandoc's sample hyperlink relationship is not needed
    rels = re.sub(r'<Relationship [^>]*TargetMode="External"\s*/>', "", rels)
    ftype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"
    rels = rels.replace("</Relationships>",
                        f'<Relationship Id="rIdFtrDefault" Type="{ftype}" Target="footer1.xml"/>'
                        f'<Relationship Id="rIdFtrFirst" Type="{ftype}" Target="footer2.xml"/></Relationships>')
    files["word/_rels/document.xml.rels"] = rels.encode()
    ct = files["[Content_Types].xml"].decode()
    fct = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
    for n in ("footer1.xml", "footer2.xml"):
        if f"/word/{n}" not in ct:
            ct = ct.replace("</Types>", f'<Override PartName="/word/{n}" ContentType="{fct}"/></Types>')
    files["[Content_Types].xml"] = ct.encode()

    # empty document properties: no title, author or company travels with the template
    if "docProps/core.xml" in files:
        core = files["docProps/core.xml"].decode()
        core = re.sub(r"<dc:title>.*?</dc:title>", "<dc:title></dc:title>", core, flags=re.S)
        core = re.sub(r"<dc:creator>.*?</dc:creator>", "<dc:creator></dc:creator>", core, flags=re.S)
        files["docProps/core.xml"] = core.encode()
    if "docProps/app.xml" in files:
        app = files["docProps/app.xml"].decode()
        app = re.sub(r"<(Company|Manager)>.*?</\1>", "", app, flags=re.S)
        files["docProps/app.xml"] = app.encode()

    out = profile_dir / "reference.docx"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in files.items():
            z.writestr(n, data)
    print(f"written {out} (text width {width} twips)")


def main():
    names = sys.argv[1:] or sorted(p.name for p in PROFILES.iterdir() if (p / "profile.json").exists())
    base = pandoc_default_reference()
    for name in names:
        d = PROFILES / name
        if not (d / "profile.json").exists():
            sys.exit(f"make_reference: no profile.json in {d}")
        build(d, base)


if __name__ == "__main__":
    main()
