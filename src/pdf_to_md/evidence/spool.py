"""Private bounded page spool; final schema validation follows the second pass."""

import tempfile
from pathlib import Path
from .canonical import canonical, decode, digest
from .model import EvidenceError
from .structure import FAMILIES


class PageSpool:
    def __init__(self, limits, input_bytes):
        self.limits = limits
        self.bytes = input_bytes
        self.records = []
        self.offsets = dict.fromkeys(FAMILIES, 0)

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="dee-pages-")
        return self

    def __exit__(self, *args):
        self.directory.cleanup()

    def put(self, d, metadata):
        record = dict(
            spool_version="1",
            pages=d["pages"],
            metadata=metadata,
            **{k: d[k] for k in FAMILIES},
        )
        raw = canonical(record)
        self.bytes += len(raw)
        self.limits.check("disk_bytes", self.bytes)
        path = Path(self.directory.name) / f"{len(self.records):04d}.json"
        path.write_bytes(raw)
        self.records.append((path, digest(raw), len(raw)))
        for family in FAMILIES:
            self.offsets[family] += len(d[family])
            d[family] = {}
        d["pages"] = []

    def restore(self, d):
        metadata = {}
        for path, expected, size in self.records:
            raw = path.read_bytes()
            if len(raw) != size or digest(raw) != expected:
                raise EvidenceError(
                    "INVALID_RESULT", "extract", "Page spool integrity failure"
                )
            record = decode(raw)
            if (
                set(record) != {"spool_version", "pages", "metadata", *FAMILIES}
                or record["spool_version"] != "1"
            ):
                raise EvidenceError(
                    "INVALID_RESULT", "extract", "Unknown page spool format"
                )
            d["pages"].extend(record["pages"])
            for family in FAMILIES:
                if set(d[family]) & set(record[family]):
                    raise EvidenceError(
                        "INVALID_RESULT", "extract", "Duplicate spooled identity"
                    )
                d[family].update(record[family])
            metadata.update(record["metadata"])
        return metadata
