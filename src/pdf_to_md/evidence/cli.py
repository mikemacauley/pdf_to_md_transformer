"""Versioned evidence commands, separate from the legacy argument parser."""

import argparse
import json
import sys
from . import (
    EvidenceError,
    Limits,
    Profile,
    extract_package,
    inspect_document,
    write_bundle,
)


def main(argv):
    parser = argparse.ArgumentParser(prog="pdf2md " + argv[0])
    parser.add_argument("input")
    parser.add_argument("--json", action="store_true")
    if argv[0] == "extract":
        parser.add_argument(
            "--profile",
            default="evidence-conservative-1",
            choices=["evidence-conservative-1"],
        )
        parser.add_argument("--bundle", required=True)
        parser.add_argument(
            "--assets", choices=["none", "figures", "regions"], default="none"
        )
        parser.add_argument("--ocr-lang", default="eng")
        parser.add_argument(
            "--mode", choices=["auto", "ocr_all", "digital_only"], default="auto"
        )
    args = parser.parse_args(argv[1:])
    try:
        with open(args.input, "rb") as source:
            if argv[0] == "inspect":
                result = inspect_document(source, limits=Limits())
                code = 0
            else:
                package = extract_package(
                    source,
                    profile=Profile(
                        profile_id=args.profile,
                        languages=tuple(args.ocr_lang.split("+")),
                        mode=args.mode,
                        asset_mode=args.assets,
                    ),
                    limits=Limits(),
                )
                document = package.document
                result = write_bundle(document, args.bundle, assets=package.assets)
                code = (
                    3
                    if document["quality"]["coverage"] != "complete"
                    or document["quality"]["issue_counts_by_severity"]["blocking"]
                    else 0
                )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return code
    except KeyboardInterrupt:
        return 130
    except (EvidenceError, OSError) as exc:
        error = dict(
            code=getattr(exc, "code", "STORAGE_UNAVAILABLE"),
            stage=getattr(exc, "stage", "io"),
            retryable=getattr(exc, "retryable", False),
            safe_message=getattr(exc, "safe_message", "Input or output unavailable"),
        )
        print(json.dumps({"error": error}), file=sys.stderr)
        return 1
