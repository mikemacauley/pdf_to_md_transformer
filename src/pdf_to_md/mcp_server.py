"""Model Context Protocol (MCP) server -- lets an AI agent drive the converter.

Speaks JSON-RPC 2.0 over stdio, one JSON object per line, with no third-party
dependencies (the same offline/deterministic guarantee as the rest of the
tool).  Register it with an agent, e.g. Claude Code:

    claude mcp add pdf2md -- pdf2md-mcp

or in an ``mcpServers`` config block:

    {"mcpServers": {"pdf2md": {"command": "pdf2md-mcp"}}}

Tools exposed:
    convert_pdf   -- PDF -> Markdown returned as text
    convert_file  -- PDF -> .md file on disk, returns the path
    pdf_info      -- page count, which pages need OCR, OCR availability
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

from . import __version__
from .engine import convert_file, convert_pdf_to_markdown
from .ocr import OcrOptions

PROTOCOL_VERSION = "2025-06-18"

# Markdown longer than this is truncated in the tool result (with a note) so a
# single huge document cannot blow up an agent's context window.
DEFAULT_MAX_CHARS = 200_000
MAX_REQUEST_CHARS = 1_000_000

TOOLS = [
    {
        "name": "convert_pdf",
        "description": (
            "Convert a PDF file to clean Markdown and return the text. "
            "Offline and deterministic: same file + same version -> same "
            "output. Tables (ruled and borderless) and sidebars are placed "
            "inline; PDF metadata is never included. Scanned pages are OCR'd "
            "when Tesseract is available; every OCR'd page is marked with "
            "its provenance and any doubtful numbers are listed in an HTML "
            "comment for checking."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Absolute or relative path to the PDF."},
                "ocr_lang": {
                    "type": "string",
                    "description": ("Tesseract language for scanned pages "
                                    "(default 'eng', e.g. 'deu', 'fra+eng')."),
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Truncate the returned Markdown to this many "
                        f"characters (default {DEFAULT_MAX_CHARS}). Use "
                        "convert_file for large documents."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "convert_file",
        "description": (
            "Convert a PDF to Markdown and write it to disk. Use this instead "
            "of convert_pdf for large documents. Set extract_images to also "
            "write each figure as a PNG beside the Markdown. Returns the "
            "output path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the PDF."},
                "out_path": {"type": "string",
                             "description": "Exact .md path to write."},
                "ocr_lang": {
                    "type": "string",
                    "description": ("Tesseract language for scanned pages "
                                    "(default 'eng', e.g. 'deu', 'fra+eng')."),
                },
                "extract_images": {
                    "type": "boolean",
                    "description": ("Also write each figure as a PNG into a "
                                    "sibling <name>_assets/ directory and "
                                    "link it from the Markdown."),
                },
                "outdir": {
                    "type": "string",
                    "description": ("Directory to write <name>.md into "
                                    "(ignored if out_path is given; default: "
                                    "next to the PDF)."),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "pdf_info",
        "description": (
            "Inspect a PDF without converting it: page count, which pages "
            "have no text layer (and so need OCR), and whether an OCR engine "
            "is available. Content only -- document metadata is not read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the PDF."},
            },
            "required": ["path"],
        },
    },
]


# --------------------------------------------------------------------------
# tool implementations
# --------------------------------------------------------------------------

def _require_pdf(args: Dict[str, Any]) -> str:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("'path' is required")
    if not os.path.isfile(path):
        raise ValueError(f"not found: {path}")
    return path


def _ocr_options(args: Dict[str, Any]) -> OcrOptions:
    return OcrOptions(lang=args.get("ocr_lang", "eng"))


def _tool_convert_pdf(args: Dict[str, Any]) -> str:
    path = _require_pdf(args)
    limit = args.get("max_chars", DEFAULT_MAX_CHARS)
    if type(limit) is not int or limit <= 0:
        raise ValueError("'max_chars' must be a positive integer")
    md = convert_pdf_to_markdown(path, ocr_options=_ocr_options(args))
    if isinstance(limit, int) and limit > 0 and len(md) > limit:
        md = (md[:limit]
              + f"\n\n<!-- truncated at {limit} of {len(md)} characters; "
                f"use convert_file to write the whole document to disk -->\n")
    return md or "<!-- no extractable content -->"


def _tool_convert_file(args: Dict[str, Any]) -> str:
    path = _require_pdf(args)
    for key in ("out_path", "outdir"):
        if key in args and (not isinstance(args[key], str) or not args[key]):
            raise ValueError(f"'{key}' must be a non-empty string")
    if "extract_images" in args and type(args["extract_images"]) is not bool:
        raise ValueError("'extract_images' must be a boolean")
    options = _ocr_options(args)
    out_path = args.get("out_path")
    if not out_path:
        base = os.path.splitext(os.path.basename(path))[0] + ".md"
        outdir = args.get("outdir") or os.path.dirname(os.path.abspath(path))
        out_path = os.path.join(outdir, base)
    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    convert_file(path, out_path,
                 extract_images=bool(args.get('extract_images')),
                 ocr_options=options)
    return json.dumps({"output": out_path,
                       "bytes": os.path.getsize(out_path)}, indent=2)


def _tool_pdf_info(args: Dict[str, Any]) -> str:
    import pdfplumber

    from .ocr import ocr_available, page_needs_ocr

    path = _require_pdf(args)
    scanned = []
    with pdfplumber.open(path) as pdf:
        npages = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            if page_needs_ocr(page):
                scanned.append(i + 1)
            page.close()
    return json.dumps({
        "path": path,
        "pages": npages,
        "pages_needing_ocr": scanned,
        "ocr_available": ocr_available(),
    }, indent=2)


HANDLERS = {
    "convert_pdf": _tool_convert_pdf,
    "convert_file": _tool_convert_file,
    "pdf_info": _tool_pdf_info,
}


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

def handle_request(msg: Dict[str, Any]) -> Any:
    """Return a JSON-RPC response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request"}}
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params", {})

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}

    if (msg.get("jsonrpc") != "2.0" or not isinstance(method, str)
            or (mid is not None and type(mid) not in (str, int))):
        mid = None
        return err(-32600, "invalid request")
    if "id" not in msg:  # notification -- nothing to answer
        return None
    if not isinstance(params, dict):
        return err(-32602, "params must be an object")

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "pdf-to-md-transformer",
                           "version": __version__},
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return err(-32602, "tool name must be a string")
        handler = HANDLERS.get(name)
        if handler is None:
            return err(-32602, f"unknown tool: {name}")
        try:
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            text = handler(arguments)
        except Exception as exc:
            return ok({"content": [{"type": "text",
                                    "text": f"{type(exc).__name__}: {exc}"}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": text}],
                   "isError": False})
    return err(-32601, f"method not found: {method}")


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    while True:
        line = stdin.readline(MAX_REQUEST_CHARS + 1)
        if not line:
            break
        if len(line) > MAX_REQUEST_CHARS:
            while line and not line.endswith("\n"):
                line = stdin.readline(MAX_REQUEST_CHARS + 1)
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "request too large"}}) + "\n")
            stdout.flush()
            continue
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle_request(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv and argv[0] == "--version":
        print(__version__)
        return 0
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
