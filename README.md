# PDF to MD Transformer

A free, open-source (MIT) desktop tool that **converts PDF files into clean, organized Markdown** — entirely on your own machine.

- **Deterministic and mechanical.** Conversion is pure geometry and typography analysis (no cloud, no network calls, no randomness). The same PDF with the same tool version always produces byte-identical Markdown.
- **Metadata is stripped.** Only page *content* reaches the output. The PDF's Info dictionary, XMP metadata, author/producer strings, etc. are never copied.
- **Tables come through as Markdown tables**, placed inline exactly where they appear in the document flow — both fully ruled grids and the borderless *booktabs* style (horizontal rules only) used by virtually all scientific journals, reconstructed from column alignment.
- **Scanned papers are supported.** Pages with no text layer are rendered at 300 dpi, deskewed, and read with the [Tesseract](https://github.com/tesseract-ocr/tesseract) OCR engine (Apache-2.0); ruled lines are recovered by pixel analysis so tables in old scans are reconstructed too. OCR'd pages carry a provenance comment naming the engine version, page-segmentation mode, dpi and language, and every doubtful number is listed for checking (see below).
- **Figures are anchored, not dropped.** Embedded images and vector illustrations are detected and placed in reading order with their caption bound to them (`Figure 3: …` above or below); `--extract-images` writes each one as a PNG into `<name>_assets/` and links it.
- **Equations are reconstructed as LaTeX where the layout can be read with certainty** — fractions from the rule geometry, super/subscripts from baseline offsets, sums and integrals with their limits, radicals from the overline — and emitted as a `$$` block labelled as reconstructed. Where anything is uncertain (an undecodable glyph, an unexplained rule), nothing is claimed: the equation falls back to a labelled ```` ```equation ```` block holding the verbatim glyph run. Either way it stops flowing into the prose.
- **Sidebars / callout boxes** are rendered inline as blockquotes at their position in the reading order, whether they are shaded panels, boxes drawn with a border (a stroked rectangle or four separate lines), or unboxed pull quotes set inset in a different size or face.
- **Layout-aware:** two-column pages are re-flowed into natural reading order; bulleted and numbered lists are preserved; hyphenated line-wraps are repaired.
- **Headings** are ranked by font size into `#`–`######`. Section numbers in a heading (`3.1 Methods`) are preserved verbatim, so the document's own hierarchy survives even where the visual ranking is coarse.
- **Simple UI:** pick PDFs, pick an output folder, press *Convert*. A command-line interface (`pdf2md`) is included for scripting, and an MCP server (`pdf2md-mcp`) lets an AI agent drive the same engine.

## Install

Grab the installer for your platform from the [Releases](../../releases) page:

| Platform | File | OCR engine |
| --- | --- | --- |
| Windows x64 | `PDF-to-MD-Transformer-Setup-win-x64.exe` | bundled |
| macOS | `PDF-to-MD-Transformer-macOS.dmg` | `brew install tesseract` |
| Linux (Debian/Ubuntu) | `pdf-to-md-transformer_<version>_amd64.deb` | installed automatically (Recommends) |

On Linux: `sudo apt install ./pdf-to-md-transformer_<version>_amd64.deb`

Without Tesseract the app still fully converts any PDF that has a text layer; scanned pages are skipped with an explanatory comment in the output. A custom Tesseract location can be set with the `PDF2MD_TESSERACT` environment variable.

### Run from source (any platform with Python 3.9+)

```
pip install .
pdf2md-gui          # GUI
pdf2md file.pdf     # CLI -> file.md next to the PDF
```

## Use from an AI agent

The tool is fully drivable by an AI agent (Claude Code, Codex, …) as well as by
hand — same engine, same deterministic output.

**MCP server** (`pdf2md-mcp`, JSON-RPC over stdio, no extra dependencies):

```
claude mcp add pdf2md -- pdf2md-mcp
```

exposing `convert_pdf` (PDF -> Markdown text), `convert_file` (PDF -> `.md` on
disk) and `pdf_info` (page count, which pages need OCR, OCR availability).

**Scriptable CLI:** `--stdout` emits the Markdown, `--json` emits a
machine-readable per-file report (stdout carries only the payload; logs go to
stderr), and the exit code is non-zero if any file failed.

Output files are replaced atomically after a complete write. Conversion refuses
to overwrite a source PDF, including aliases, and batch conversion reports an
error when multiple inputs target the same output. Existing destination symlinks
are replaced rather than followed. Figure files are published individually;
the Markdown and its entire assets directory are not one atomic transaction.

A bundled Claude skill in `.claude/skills/pdf-to-markdown/` makes the agent
reach for the tool automatically. Full interface reference: [AGENTS.md](AGENTS.md).

## How it works

For each page with a text layer, the engine:

1. Detects ruled **tables** with lattice analysis ([pdfplumber](https://github.com/jsvine/pdfplumber), MIT).
2. Detects **borderless (booktabs) tables**: stacked horizontal rules bound a candidate region, column boundaries are found as vertical strips that no line of words crosses, and the grid is rebuilt from word positions. A candidate that doesn't convincingly form a table flows back into body text, so false positives can't destroy content.
3. Detects **sidebars**: filled panels, ruled boxes (stroked rectangles or four lines meeting at the corners), and — once lines exist — unboxed blocks that are inset from both sides, separated above and below, and set in a different size or face. The unboxed case is gated hard: a line sitting on any column margin is a column, not a callout, and an indented continuation in the body face stays a paragraph.
4. Rebuilds visual **lines** from positioned words (splitting side-by-side columns that share a baseline), finds the column gutter, and orders every element — paragraphs, tables, sidebars — into a single reading order.
5. Detects **display equations** — by mathematical glyphs and fonts, or by the geometry of a fraction rule — and rebuilds the expression from the positions of its glyphs, falling back to a verbatim block whenever the layout is not fully understood. A math-heavy line that is really a sentence stays prose.
6. Detects **figures** (embedded rasters and clusters of vector drawing outside tables and sidebars), binds the nearest matching caption, and drops in-figure labels from the prose. A candidate region full of text is discarded so body copy can't be swallowed.
7. Ranks **headings** by dominant font size, folds wrapped lines into paragraphs (repairing end-of-line hyphenation), and recognizes bullet/numbered lists.
8. Emits GitHub-flavoured Markdown. Nothing but page content is written.

Pages with **no text layer** (scanned papers) are rendered at 300 dpi, deskewed by projection-profile analysis, and read with Tesseract; ruled lines are recovered from pixel runs, and the exact same table-reconstruction and reading-order pipeline runs on the OCR word boxes.

Because OCR can be wrong in ways geometry cannot detect, numeric tokens are checked against Tesseract's confidence, with up to 80 second reads per page restricted to digits. Low-confidence numbers are retained and flagged. Disagreements in digits, signs, separators or percent markers are flagged too. A per-page comment lists up to 12 doubtful tokens and counts any additional ones:

```
<!-- page 3: no text layer; converted with OCR (tesseract 5.3.4, psm 3, 300 dpi, lang eng) -->
<!-- page 3: 2 numeric tokens to check against the source: '12.5' (confidence 61), '1,024' (reread as '1.024') -->
```

`--no-math-latex` disables reconstruction entirely if you would rather always see the raw glyphs. `--ocr-lang`, `--ocr-dpi` (400 helps small type), `--ocr-psm` and `--no-verify-numbers` tune the OCR pass.

OCR accepts language identifiers such as `eng`, `fra+eng` and `script/Latin`,
72–1200 dpi, and segmentation modes 1 or 3–13. Raster rendering is limited to
40 million pixels per page; reduce the dpi for large scanned pages. The main
OCR subprocess has a 120-second timeout and each numeric reread has a 10-second
timeout. MCP request lines are limited to 1,000,000 characters, and `max_chars`
must be a positive integer. These bounds do not sandbox the underlying PDF
parser or impose a total conversion memory/time limit.

### Limitations

- **OCR** output is reproducible only for a fixed Tesseract version, which the
  page comment records. Doubtful numbers are flagged, not corrected — and an
  *unflagged* number is likely, not certainly, right.
- **Reconstructed equations** are inferred from layout and should be checked
  against the source; the comment above each block says whether it was
  reconstructed or left verbatim. The glyph run inside an ` ```equation ` block
  is not a usable formula and should not be retyped as one.
- **Maths in fonts with no ToUnicode CMap**, other than Adobe `Symbol`, cannot
  be decoded, so those equations are always emitted verbatim. **Inline** maths
  inside a paragraph is not detected at all.
- **Figures** are anchored with their captions, but the images themselves are
  only written out with `--extract-images`. An equation set as a figure is an
  image, not text.
- **Unboxed callouts** are only found when they differ typographically from the
  body; one that is merely indented reads as an ordinary paragraph.
- **Headings** are ranked by visual prominence, so a heading set at body size
  (bold, unnumbered) is not distinguished from a paragraph. Section numbers
  survive verbatim in the heading text where the document uses them.

## Development

```
pip install -e .[dev]
python tests/make_fixture.py   # build a test PDF (reportlab); see tests/make_*.py
pytest
```

Every fixture PDF is generated by a `tests/make_*_fixture.py` script rather than
committed as a binary, so each one documents exactly the layout it exercises:
ruled and borderless tables, scanned pages, figures with captions above and
below, the three callout styles, and equations that must reconstruct as well as
equations that must be refused. Test modules build their own fixture on demand,
so a bare `pytest` works from a clean checkout.

OCR tests skip when Tesseract is not installed; set `PDF2MD_REQUIRE_OCR=1` to
force them to run (CI does).

Installers for all three platforms are built by [GitHub Actions](.github/workflows/build.yml) on native runners (PyInstaller + Inno Setup / hdiutil / dpkg-deb) whenever a `v*` tag is pushed.

## License

[MIT](LICENSE) © 2026 Michael Macauley. All dependencies are MIT-compatible (pdfplumber: MIT; pdfminer.six: MIT; Pillow: MIT-CMU). This project deliberately avoids AGPL-licensed PDF libraries.

## AI authorship disclosure

This repository is written and maintained by an **AI agent** working under the
handle **`paginaut`** — an instance of Anthropic's Claude running in Claude Code
— **acting as the authorized agent of Michael Macauley
([@mikemacauley](https://github.com/mikemacauley)) and on his behalf, with his
review and approval of the work at each step.** `paginaut` is not a person;
it is a name for the agent that does the work, adopted so that its contributions
are identifiable in the history rather than anonymous.

Commits, releases and documentation produced by the agent are labelled as such:
agent-authored commits carry `paginaut` as the author and a `Co-Authored-By`
trailer naming the underlying model. Design decisions and the scope of each
change are Michael's; the implementation, tests and documentation are the
agent's.

Where the agent could not verify something itself — output that depends on a
tool not installed on the build machine, or heuristics with no real-world corpus
to test against — that limitation is stated in the documentation rather than
papered over. See the limitations note under [How it works](#how-it-works) and
the guidance in [AGENTS.md](AGENTS.md).
