"""Publish complete output files without following destination symlinks."""

import os
import tempfile
from contextlib import contextmanager


@contextmanager
def atomic_output(path):
    """Yield a binary file, then atomically replace the destination on success."""
    parent = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".pdf2md-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as output:
            yield output
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_distinct_paths(source, destination):
    if (os.path.realpath(source) == os.path.realpath(destination)
            or (os.path.exists(destination) and os.path.samefile(source, destination))):
        raise ValueError("output must not overwrite the source PDF")


def reserve_output(path, claimed, sources=()):
    """Reject two batch inputs targeting the same output file."""
    key = os.path.normcase(os.path.realpath(path))
    if key in claimed:
        raise ValueError(f"multiple inputs target the same output: {path}")
    for source in sources:
        if os.path.isfile(source):
            ensure_distinct_paths(source, path)
    claimed.add(key)
