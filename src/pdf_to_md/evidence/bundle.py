"""Immutable local bundle publication, with bounded no-follow artifact reads."""

import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from .canonical import canonical, decode, digest
from .model import EvidenceError, Limits
from .render import render_markdown
from .assets import expected_assets, validate_png, EvidencePackage
from .validate import validate_document

NAMES = {
    "document.json": "application/json",
    "issues.json": "application/json",
    "reading.md": "text/markdown",
}


def _read_at(directory, name, ceiling):
    if not re.fullmatch(r"[a-z][a-z0-9_-]*\.(?:json|md|png)", name):
        raise EvidenceError("INVALID_RESULT", "bundle", "Unsafe artifact name")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > ceiling:
            raise EvidenceError(
                "INVALID_RESULT", "bundle", "Invalid artifact size or type"
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(ceiling + 1)
        if len(data) > ceiling:
            raise EvidenceError("LIMIT_EXCEEDED", "bundle", "Artifact exceeds limit")
        return data
    finally:
        os.close(fd)


def _load_bundle(directory, *, limits=Limits(), expected_source_hash=None):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        mb = _read_at(fd, "manifest.json", 1024**2)
        manifest = decode(mb)
        if canonical(manifest) != mb:
            raise ValueError("noncanonical manifest")
        from jsonschema import Draft202012Validator
        from .validate import schema

        contract = dict(schema(), **{"$ref": "#/$defs/Manifest"})
        Draft202012Validator(contract).validate(manifest)
        entries = manifest["artifacts"]
        names = [e["name"] for e in entries]
        if (
            names != sorted(set(names))
            or not set(NAMES) <= set(names)
            or any(
                name not in NAMES and not re.fullmatch(r"crop_s[0-9]{8}\.png", name)
                for name in names
            )
            or set(os.listdir(fd)) != set(names) | {"manifest.json"}
        ):
            raise ValueError("artifact inventory mismatch")
        files = {}
        total = len(mb)
        for entry in entries:
            data = _read_at(
                fd,
                entry["name"],
                (
                    limits.document_bytes
                    if entry["name"] in ("document.json", "issues.json")
                    else limits.artifact_bytes
                ),
            )
            total += len(data)
            limits.check("artifact_bytes", total)
            if (
                len(data) != entry["bytes"]
                or digest(data) != entry["sha256"]
                or entry["mime_type"] != NAMES.get(entry["name"], "image/png")
            ):
                raise ValueError("artifact mismatch")
            expected_exposure = (
                "restricted"
                if entry["name"] in ("document.json", "issues.json")
                else "ordinary"
            )
            if entry["name"] in NAMES and entry["exposure"] != expected_exposure:
                raise ValueError("incorrect artifact exposure")
            files[entry["name"]] = data
        document = decode(files["document.json"])
        validate_document(document)
        required = expected_assets(document)
        if set(names) - set(NAMES) != set(required):
            raise ValueError("asset references do not match manifest")
        for entry in entries:
            if entry["name"] in required:
                if entry["exposure"] != required[entry["name"]]["exposure"]:
                    raise ValueError("asset exposure mismatch")
                validate_png(files[entry["name"]], limits)
        if canonical(document) != files["document.json"]:
            raise ValueError("noncanonical document")
        if (
            manifest["document_hash"] != digest(files["document.json"])
            or manifest["extraction_id"] != document["extraction_id"]
        ):
            raise ValueError("manifest identity mismatch")
        if (
            expected_source_hash is not None
            and expected_source_hash != document["source"]["sha256"]
        ):
            raise ValueError("source mismatch")
        if (
            files["issues.json"] != canonical(document["issues"])
            or files["reading.md"] != render_markdown(document).encode()
        ):
            raise ValueError("projection mismatch")
        receipt = dict(
            extraction_id=document["extraction_id"],
            document_hash=manifest["document_hash"],
            manifest_hash=digest(mb),
            quality=document["quality"],
            bytes=total,
        )
        return receipt, files
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError(
            "INVALID_RESULT", "bundle", "Bundle integrity validation failed"
        ) from exc
    finally:
        os.close(fd)


def validate_bundle(directory, *, limits=Limits(), expected_source_hash=None):
    return _load_bundle(
        directory, limits=limits, expected_source_hash=expected_source_hash
    )[0]


def read_bundle(directory, *, limits=Limits(), expected_source_hash=None):
    _, files = _load_bundle(
        directory, limits=limits, expected_source_hash=expected_source_hash
    )
    return EvidencePackage(
        decode(files["document.json"]),
        {name: data for name, data in files.items() if name.endswith(".png")},
    )


def write_bundle(document, destination, *, assets=None):
    validate_document(document)
    destination = Path(destination).absolute()
    parent = destination.parent
    if not parent.is_dir() or parent.resolve() != parent:
        raise EvidenceError(
            "INVALID_OPTIONS",
            "bundle",
            "Bundle parent must be an existing directory without symlinks",
        )
    limits = Limits(**document["recipe"]["count_limits"])
    files = {
        "document.json": canonical(document),
        "issues.json": canonical(document["issues"]),
        "reading.md": render_markdown(document).encode(),
    }
    assets = {} if assets is None else assets
    required = expected_assets(document)
    if set(assets) != set(required):
        raise EvidenceError(
            "ASSET_RENDER_FAILED",
            "bundle",
            "Required source assets are missing or unexpected",
        )
    for name, data in assets.items():
        if type(data) is not bytes:
            raise EvidenceError("INVALID_RESULT", "bundle", "Artifact bytes required")
        validate_png(data, limits)
    files.update(assets)
    manifest = dict(
        schema_version="1.0.0",
        extraction_id=document["extraction_id"],
        document_hash=digest(files["document.json"]),
        artifacts=[
            dict(
                name=name,
                mime_type=NAMES.get(name, "image/png"),
                bytes=len(data),
                sha256=digest(data),
                exposure=(
                    "restricted"
                    if name in ("document.json", "issues.json")
                    else required.get(name, {}).get("exposure", "ordinary")
                ),
            )
            for name, data in sorted(files.items())
        ],
    )
    files["manifest.json"] = canonical(manifest)
    limits.check("artifact_bytes", sum(map(len, files.values())))
    stage = Path(tempfile.mkdtemp(prefix=".dee-", dir=parent))
    try:
        for name, data in files.items():
            fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        receipt = validate_bundle(
            stage, limits=limits, expected_source_hash=document["source"]["sha256"]
        )
        # Serialize cooperating publishers without ever replacing an existing
        # directory. The lock holds no source content; OS releases it on crash.
        import fcntl

        lockfd = os.open(
            parent / ".dee-publication.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX)
            if os.path.lexists(destination):
                previous = validate_bundle(destination, limits=limits)
                if previous["extraction_id"] != receipt["extraction_id"]:
                    raise EvidenceError(
                        "INVALID_OPTIONS",
                        "bundle",
                        "Destination belongs to another extraction",
                    )
                if previous["manifest_hash"] != receipt["manifest_hash"]:
                    quarantine = parent / (
                        ".dee-quarantine-" + stage.name.removeprefix(".dee-")
                    )
                    os.rename(stage, quarantine)
                    raise EvidenceError(
                        "NONDETERMINISTIC_RESULT",
                        "bundle",
                        "Conflicting deterministic result quarantined",
                    )
            else:
                stagefd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(stagefd)
                finally:
                    os.close(stagefd)
                os.rename(stage, destination)
                parentfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parentfd)
                finally:
                    os.close(parentfd)
        finally:
            os.close(lockfd)
        return dict(receipt, output=str(destination))
    finally:
        if stage.exists():
            shutil.rmtree(stage)
