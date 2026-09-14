"""Real Docker controls are opt-in and bound to an immutable image digest."""

import io
import os
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas
from pdf_to_md.evidence import EvidenceError
from pdf_to_md.supervisor import Supervisor


def test_production_requires_qualification(tmp_path):
    with pytest.raises(EvidenceError):
        Supervisor("sha256:" + "1" * 64, staging_root=tmp_path)
    with pytest.raises(EvidenceError):
        Supervisor("image:mutable", staging_root=tmp_path, development=True)


@pytest.fixture
def worker(tmp_path):
    image = os.environ.get("DEE_TEST_IMAGE")
    if not image:
        pytest.skip("Set DEE_TEST_IMAGE to a built immutable development image")
    return Supervisor(image, staging_root=tmp_path, wall_seconds=30, development=True)


def source():
    b = io.BytesIO()
    c = canvas.Canvas(b, invariant=1)
    c.drawString(72, 720, "Synthetic worker fixture")
    c.save()
    b.seek(0)
    return b


def test_real_worker_extract_and_inspect(worker):
    receipt = worker.run(source())
    assert receipt["receipt"]["quality"]["coverage"] == "complete"
    assert receipt["execution"]["qualified"] is False
    assert set(receipt["artifacts"]) == {
        "document.json",
        "manifest.json",
        "reading.md",
        "issues.json",
    }
    inspection = worker.run(source(), operation="inspect")
    assert inspection["receipt"]["page_count"] == 1
    assert not list(worker.root.iterdir())


def test_real_worker_preserves_deterministic_refusal(worker):
    with pytest.raises(EvidenceError) as error:
        worker.run(io.BytesIO(b"not PDF"))
    assert error.value.code == "UNSUPPORTED_FORMAT"
    assert not list(worker.root.iterdir())


def test_real_worker_cancelled_authority(worker):
    with pytest.raises(EvidenceError) as error:
        worker.run(source(), cancelled=lambda: True)
    assert error.value.code == "STALE_ATTEMPT"
    assert not list(worker.root.iterdir())


@pytest.mark.parametrize("cause", ["cancel", "deadline"])
def test_copy_preserves_terminal_cause(tmp_path, cause):
    import sys
    import time

    transport = tmp_path / "slow-transport"
    transport.write_text(
        "#!"
        + sys.executable
        + '\nimport os,time\nfor i in range(1000):\n os.write(1,b"x"*65536)\n time.sleep(.01)\n'
    )
    transport.chmod(0o700)
    supervisor = Supervisor(
        "sha256:" + "1" * 64,
        staging_root=tmp_path,
        docker=str(transport),
        development=True,
    )
    started = time.monotonic()
    deadline = started + (0.15 if cause == "deadline" else 5)
    cancelled = lambda: cause == "cancel" and time.monotonic() - started > 0.15
    with pytest.raises(EvidenceError) as error:
        supervisor._copy(
            "unused",
            "/fixed-path",
            tmp_path / "output",
            100_000_000,
            deadline,
            cancelled,
        )
    assert error.value.code == (
        "STALE_ATTEMPT" if cause == "cancel" else "WORKER_TIMEOUT"
    )
    assert time.monotonic() - started < 3


def test_real_worker_transports_region_assets(worker):
    from pdf_to_md.evidence import Profile

    receipt = worker.run(source(), profile=Profile(asset_mode="regions"))
    assert any(name.endswith(".png") for name in receipt["artifacts"])
    assert not list(worker.root.iterdir())
