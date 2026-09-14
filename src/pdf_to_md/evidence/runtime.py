"""Local inventory. A deployment supervisor must qualify this inventory separately."""

import importlib.metadata
import os
import platform
import subprocess
from pathlib import Path
from .canonical import canonical, digest
from ..ocr import find_tesseract


def inventory():
    dependencies = []
    for name in (
        "pdfplumber",
        "pdfminer.six",
        "pypdfium2",
        "Pillow",
        "numpy",
        "pytesseract",
        "jsonschema",
    ):
        distribution = importlib.metadata.distribution(name)
        records = []
        for entry in sorted(distribution.files or [], key=str):
            path = Path(distribution.locate_file(entry))
            if path.is_file() and path.suffix not in (".pyc", ".pyo"):
                records.append([str(entry), digest(path.read_bytes())])
        dependencies.append(
            dict(
                name=name,
                version=distribution.version,
                sha256=digest(canonical(records)),
            )
        )
    cmd = find_tesseract()
    trained = []
    if cmd:
        result = subprocess.run(
            [cmd, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        import re

        match = re.search(r'"([^"\n]+)"', result.stdout + result.stderr)
        folder = Path(
            os.environ.get("TESSDATA_PREFIX")
            or (match.group(1) if match else "/nonexistent")
        )
        for path in sorted(folder.glob("*.traineddata")):
            trained.append(dict(language=path.stem, sha256=digest(path.read_bytes())))
    return dict(
        qualification_id="local-unqualified",
        python_version=platform.python_version(),
        os=platform.system(),
        architecture=platform.machine(),
        dependencies=sorted(dependencies, key=lambda d: d["name"]),
        ocr_binary_sha256=digest(Path(cmd).read_bytes()) if cmd else None,
        traineddata=trained,
    )


def engine_digest():
    root = Path(__file__).parents[1]
    return digest(
        canonical(
            [
                [str(p.relative_to(root)), digest(p.read_bytes())]
                for p in sorted(root.rglob("*"))
                if p.suffix in (".py", ".json")
            ]
        )
    )
