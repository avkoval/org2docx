# org2docx

Export Org documents to styled DOCX and PDF from Emacs or the command line.

org2docx turns any Org file into a Word document with consistent, print-ready formatting and a matching PDF. It runs
pandoc with a profile-specific reference document and a Lua filter, post-processes the DOCX XML (tables, code,
lists, bookmarks), and then lets LibreOffice refresh the table of contents and export the PDF.

## What the output looks like

All profiles share the same table and code treatment:

- tables span the full text width, column widths follow the content (the longest unbreakable token sets the minimum
  width, the remaining width is shared by text length), single black borders on every cell, a light grey (`EFEFEF`)
  header row that repeats on every page, and rows that never split across pages;
- inline code in tables is set smaller than in body text; code blocks use a small monospaced font (8 pt) on a light
  grey background across the full text width;
- tight list items use the body text style, not the compact table-cell style;
- headings are numbered hierarchically (`1`, `1.2`, `1.2.3`); appendix headings can keep their letter (`Appendix A`)
  and number their children `A.1`, `A.1.1`;
- table and figure captions are numbered (`Table 3. …`, `Figure 2. …`) with words taken from the profile;
- PlantUML source blocks are rendered to PNG figures;
- an optional table of contents (a real Word TOC field with page numbers) follows the title block;
- heading bookmarks are moved into the heading paragraphs so internal links survive the LibreOffice round trip.

Bundled profiles:

| Profile    | Body text                                                         | Page                                 | Defaults                                    |
|------------|-------------------------------------------------------------------|--------------------------------------|---------------------------------------------|
| `formal`   | Times New Roman 14 pt, line spacing 1.5, first-line indent 1.25 cm, justified | A4, left margin 2 cm, top, bottom and right 1 cm, page number bottom right, none on the first page | TOC on, appendix lettering on, uncaptioned diagrams numbered |
| `internal` | Liberation Sans 11 pt, single spacing, 6 pt after paragraphs, left-aligned | A4, 2 cm margins, page number bottom right | TOC off, appendix lettering off |

Caption words, the TOC title and the appendix word are Ukrainian by default (`Таблиця`, `Рисунок`, `ЗМІСТ`,
`Додаток`); a document with `#+LANGUAGE: en` gets the English words (`Table`, `Figure`, `Contents`, `Appendix`)
through the profile's `by_language` section.

## Requirements

- pandoc 3.1 or newer (tested with 3.11);
- LibreOffice (`soffice`) for the PDF and the table of contents, with its Python bridge (`import uno`) available to
  the Python that runs `org2docx.py`; without the bridge org2docx falls back to `soffice --convert-to pdf`, which does
  not refresh the table of contents;
- Python 3.9+ (standard library only);
- optional: `plantuml` on `PATH` for diagrams, `pdfinfo` (poppler) for the page count in the summary;
- Emacs 27.1+ with Org 9.5+ for the Emacs command.

LibreOffice runs headless with a private, temporary user profile, so a LibreOffice window that is already open is not
affected.

## Installation

Clone the repository anywhere, for example `~/.emacs.d/org2docx`.

With `use-package` and a local checkout:

```elisp
(use-package org2docx
  :ensure nil
  :load-path "~/.emacs.d/org2docx"
  :after org
  :demand t)
```

With straight.el:

```elisp
(use-package org2docx
  :straight (org2docx :type git :host github :repo "USERNAME/org2docx"
                      :files ("*.el" "*.py" "*.lua" "profiles"))
  :after org
  :demand t)
```

Without a package manager:

```elisp
(add-to-list 'load-path "~/.emacs.d/org2docx")
(with-eval-after-load 'org (require 'org2docx))
```

`org2docx-directory` defaults to the directory `org2docx.el` was loaded from; set `org2docx-script` if
`org2docx.py` lives elsewhere.

## Usage from Emacs

- `M-x org2docx-export` exports the current buffer to `<basename>.docx` and `<basename>.pdf` next to the file.
  With `C-u` it asks for the profile. `my/org2docx-export` is an alias.
- `C-c C-e w` in the export dispatcher: `w` exports, `p` asks for the profile, `o` exports and opens the PDF.
  "Body only" is ignored; "Subtree" and "Visible only" are honoured.

The export runs asynchronously; the converter output goes to the `*org2docx*` buffer, which is shown when the export
fails. On success the paths are reported and, depending on `org2docx-open-pdf` (`ask`, `always`, `never`), the PDF is
opened with `org-open-file`.

Before conversion the buffer is expanded with Org's own exporter (a back-end derived from `org`): `#+INCLUDE`,
`#+MACRO`, `#+BIND` and `#+SETUPFILE` are resolved, `:noexport:` and `COMMENT` subtrees are removed, export blocks and
snippets of every type (for example raw `openxml`) are kept. The result goes to a temporary file that is deleted
afterwards.

Customization (`M-x customize-group RET org2docx`):

| Variable                   | Default           | Meaning                                                            |
|----------------------------|-------------------|--------------------------------------------------------------------|
| `org2docx-default-profile` | `nil` (`formal`)  | profile used when the document has no `#+DOCX_PROFILE`             |
| `org2docx-open-pdf`        | `ask`             | `ask`, `always` or `never`                                         |
| `org2docx-use-babel`       | `nil`             | evaluate Babel blocks during the expansion (off: no code runs)     |
| `org2docx-python`          | `"python"`        | interpreter for `org2docx.py`                                      |
| `org2docx-extra-args`      | `nil`             | extra arguments, e.g. `("--keep-intermediate")`                    |

## Command line

```sh
python org2docx.py INPUT.org [-o OUTDIR] [--profile NAME] [--pdf | --no-pdf] [--toc | --no-toc]
                   [--name BASENAME] [--resource-path DIR] [--keep-intermediate]
python org2docx.py --list-profiles
```

Output goes to `OUTDIR` (default: the input file's directory) as `<name>.docx` and `<name>.pdf`. `--keep-intermediate`
keeps the raw pandoc DOCX and the rendered diagrams in `OUTDIR/<name>.org2docx-build/`.

Exit status: `0` success, `1` conversion failure, `2` usage error or missing dependency (pandoc, LibreOffice, an
unknown profile).

The command line reads the file with pandoc's Org reader directly, **without** the Emacs pre-expansion: plain
`#+INCLUDE: "file.org"` works, but macros, `:noexport:` tags, `COMMENT` subtrees, `#+BIND` and custom link types are
not processed. Use the Emacs command for such documents.

## Org keywords

Per-document settings in the file header; the command line overrides them.

| Keyword                       | Effect                                                       |
|-------------------------------|--------------------------------------------------------------|
| `#+DOCX_PROFILE: internal`    | profile for this document                                    |
| `#+DOCX_TOC: t` / `nil`       | table of contents on or off                                  |
| `#+DOCX_PDF: t` / `nil`       | PDF on or off                                                |
| `#+DOCX_CAPTION_TABLE: Tab.`  | caption word for tables                                      |
| `#+DOCX_CAPTION_FIGURE: Fig.` | caption word for figures                                     |
| `#+DOCX_TOC_TITLE: Contents`  | title above the table of contents                            |
| `#+LANGUAGE: en`              | selects the profile's `by_language.en` overrides             |
| `#+OPTIONS: num:nil` / `num:2`| no heading numbers / numbers on levels 1–2 only              |
| `#+OPTIONS: toc:nil` / `toc:2`| table of contents off / two levels                           |

Priority: command line, then `#+DOCX_*`, then `#+OPTIONS`, then the profile. Headings with `:UNNUMBERED: t` are not
numbered. A document that already contains a raw `openxml` TOC field (for example on a hand-made title page) gets no
second one; its field is still refreshed.

## Profiles

A profile is a directory `profiles/<name>/` with `profile.json` and the generated `reference.docx`.

`profile.json` has two parts:

- top-level converter settings: `caption_table`, `caption_figure`, `caption_separator`, `toc`, `toc_title`,
  `toc_levels`, `pdf`, `number_headings`, `appendix_lettering`, `appendix_word`, `appendix_page_break`,
  `number_uncaptioned_diagrams`, `code_block_size_pt`, a `table` section (`header_fill`, `border_color`,
  `border_size`, `inline_code_size_pt`, and the column-width estimates `cell_char_twips`, `cell_mono_twips`,
  `cell_pad_twips`), and `by_language` overrides keyed by the `#+LANGUAGE` code;
- `reference`: the typography used to build `reference.docx`: `font`, `mono_font`, `size_pt`, `line` (240 = single,
  360 = 1.5), `first_line_indent`, `paragraph_after`, `align`, `lang`, table, code, caption and footnote sizes,
  `title`, a `headings` list (size, bold, italic, spacing, alignment per level), `page` (size and margins in twips),
  `page_number` (`left`, `center`, `right`, `none`) and `page_number_on_first_page`.

To add a profile:

```sh
cp -r profiles/internal profiles/memo
$EDITOR profiles/memo/profile.json
python make_reference.py memo        # writes profiles/memo/reference.docx
```

`make_reference.py` starts from pandoc's default `reference.docx`, so every style pandoc emits exists; document
properties (title, author, company) are left empty. When changing fonts, adjust `table.cell_char_twips` (the average
character width in table cells, in twips) so that column widths stay realistic.

## Limitations

- Merged cells and column spans: Org tables have none, so none are produced; tables from raw `openxml` are left
  untouched.
- Babel results: by default no code runs during export; source blocks are exported as written and existing `#+RESULTS`
  are exported as they are, including results of blocks marked `:exports code`. Set `org2docx-use-babel` to let Org
  evaluate blocks and apply `:exports`.
- What pandoc's Org reader drops or simplifies: `#+ATTR_*` attributes other than image width, `#+LATEX_HEADER`,
  LaTeX fragments (rendered as text or OMML math depending on pandoc), table formulas (`#+TBLFM`, values only),
  column groups and alignment cookies, inline tasks, planning lines and clocks, citations without a bibliography.
- Very wide tables: when the longest tokens of all columns do not fit the text width, columns are scaled in
  proportion and long tokens wrap inside the cell.
- Uncaptioned images get no caption; only captioned figures (and, if enabled, PlantUML diagrams) are numbered.
- The table of contents and page numbers are refreshed by LibreOffice. Word shows the same field and may ask to
  update fields when the document is opened.
- Fonts that are not installed are substituted by LibreOffice in the PDF (for example Liberation Serif for Times New
  Roman, which is metrically compatible).

## License

MIT, see [LICENSE](LICENSE).
