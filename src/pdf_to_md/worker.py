"""One-shot offline worker entry point; launched only by a supervisor."""

import argparse
import json
import os
import time
from pathlib import Path
from .evidence import (
    EvidenceError,
    Limits,
    Profile,
    extract_package,
    inspect_document,
    write_bundle,
)
from .evidence.canonical import canonical, decode


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", choices=("inspect", "extract"), required=True)
    args = parser.parse_args(argv)
    # Fixed mount points are supervisor authority, never document-provided paths.
    try:
        options = decode(Path("/input/options.json").read_bytes())
        if set(options) != {"profile", "limits"}:
            raise ValueError("unknown options")
        fields = dict(options["profile"])
        for name in ("requested_pages", "required_features", "languages"):
            fields[name] = tuple(fields[name])
        profile = Profile(**fields)
        limits = Limits(**options["limits"])
        with open("/input/source.pdf", "rb") as source:
            if args.operation == "extract":
                package = extract_package(source, profile=profile, limits=limits)
                receipt = write_bundle(
                    package.document, Path("/work/bundle"), assets=package.assets
                )
            else:
                receipt = inspect_document(source, limits=limits)
        Path("/work/receipt.json").write_bytes(canonical(receipt))
        # Keep the container tmpfs mounted until the supervisor retrieves the
        # completed artifact set and removes the container.
        while True:
            time.sleep(1)
    except (EvidenceError, ValueError) as exc:
        Path("/work/error.json").write_bytes(
            canonical(
                dict(
                    code=getattr(exc, "code", "INVALID_OPTIONS"),
                    stage=getattr(exc, "stage", "worker"),
                )
            )
        )
        while True:
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
