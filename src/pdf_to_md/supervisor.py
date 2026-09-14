"""Linux Docker boundary. Development runs are explicitly unqualified.

Qualification is trusted deployment configuration, never request JSON. Its image,
runtime and build digests must match before a worker result may be returned.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .evidence import EvidenceError, Limits, Profile, validate_bundle
from .evidence.canonical import canonical, decode, digest

ERRORS = {
    "INVALID_INPUT",
    "ENCRYPTED_INPUT",
    "UNSUPPORTED_FORMAT",
    "INVALID_OPTIONS",
    "UNSUPPORTED_SCHEMA",
    "LIMIT_EXCEEDED",
    "WORKER_TIMEOUT",
    "WORKER_CRASH",
    "INVALID_RESULT",
    "ASSET_RENDER_FAILED",
    "SOURCE_UNAVAILABLE",
    "STALE_ATTEMPT",
    "STORAGE_UNAVAILABLE",
}


class Supervisor:
    def __init__(
        self,
        image,
        *,
        staging_root,
        docker="docker",
        wall_seconds=900,
        qualification=None,
        development=False,
    ):
        if not re.fullmatch(r"(?:[a-zA-Z0-9._:/-]+@)?sha256:[0-9a-f]{64}", image):
            raise EvidenceError(
                "INVALID_OPTIONS", "supervisor", "A pinned image digest is required"
            )
        if type(wall_seconds) is not int or not 1 <= wall_seconds <= 900:
            raise EvidenceError(
                "INVALID_OPTIONS", "supervisor", "Invalid wall deadline"
            )
        if not development and (
            not isinstance(qualification, dict)
            or set(qualification)
            != {"image", "qualification_id", "runtime", "engine_build_sha256"}
            or qualification["image"] != image
        ):
            raise EvidenceError(
                "INVALID_OPTIONS",
                "supervisor",
                "Matching deployment qualification is required",
            )
        self.image = image
        self.root = Path(staging_root).resolve()
        self.docker = docker
        self.wall = wall_seconds
        self.qualification = qualification
        self.development = development
        if not self.root.is_dir():
            raise EvidenceError(
                "STORAGE_UNAVAILABLE", "supervisor", "Staging unavailable"
            )

    def _run(self, *args, timeout=10):
        return subprocess.run(
            [self.docker, *args], capture_output=True, timeout=timeout, check=True
        )

    def readiness(self):
        try:
            info = json.loads(self._run("info", "--format", "{{json .}}").stdout)
            if info.get("OSType") != "linux" or not all(
                info.get(k) for k in ("MemoryLimit", "PidsLimit", "CpuCfsQuota")
            ):
                raise ValueError("required controllers unavailable")
            image = json.loads(self._run("image", "inspect", self.image).stdout)[0]
            if (
                self.image not in image.get("RepoDigests", [])
                and self.image != image["Id"]
            ):
                raise ValueError("image not installed")
            return dict(image=self.image, os="linux", qualified=not self.development)
        except Exception as exc:
            raise EvidenceError(
                "INVALID_OPTIONS",
                "supervisor",
                "Required Linux container controls unavailable",
            ) from exc

    def _copy(self, name, path, destination, ceiling, deadline, cancelled):
        # Fixed cat argv avoids Docker cp's inability to copy tmpfs mounts. A
        # deadline watchdog kills the transport, while timeout inside the
        # container independently bounds the whole worker lifetime.
        process = subprocess.Popen(
            [self.docker, "exec", name, "cat", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stop = threading.Event()

        def watch():
            while not stop.wait(0.05):
                if time.monotonic() >= deadline or cancelled():
                    process.kill()
                    return

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        size = 0
        try:
            with destination.open("xb") as out:
                while True:
                    data = process.stdout.read(min(65536, ceiling - size + 1))
                    if not data:
                        break
                    size += len(data)
                    if size > ceiling:
                        raise EvidenceError(
                            "LIMIT_EXCEEDED",
                            "supervisor",
                            "Worker output exceeds limit",
                        )
                    out.write(data)
            status = process.wait(
                timeout=max(0.01, min(5, deadline - time.monotonic()))
            )
            if cancelled():
                raise EvidenceError(
                    "STALE_ATTEMPT", "supervisor", "Worker authority revoked"
                )
            if time.monotonic() >= deadline:
                raise EvidenceError(
                    "WORKER_TIMEOUT", "supervisor", "Worker deadline exceeded"
                )
            if status != 0:
                raise EvidenceError(
                    "WORKER_CRASH", "supervisor", "Worker output unavailable"
                )
            return size
        finally:
            stop.set()
            thread.join(timeout=1)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()

    def run(
        self,
        source,
        *,
        operation="extract",
        profile=Profile(),
        limits=Limits(),
        cancelled=lambda: False,
    ):
        if operation not in ("inspect", "extract"):
            raise EvidenceError(
                "INVALID_OPTIONS", "supervisor", "Unsupported operation"
            )
        self.readiness()
        name = "dee-" + uuid.uuid4().hex
        stage = Path(tempfile.mkdtemp(prefix=".dee-attempt-", dir=self.root))
        created = False
        clean = True
        try:
            inputs = stage / "input"
            inputs.mkdir(mode=0o755)
            size = 0
            with (inputs / "source.pdf").open("xb") as out:
                while True:
                    data = source.read(min(1024**2, limits.input_bytes - size + 1))
                    if not data:
                        break
                    size += len(data)
                    limits.check("input_bytes", size)
                    out.write(data)
            source_hash = digest((inputs / "source.pdf").read_bytes())
            fields = asdict(profile)
            for k in ("requested_pages", "required_features", "languages"):
                fields[k] = list(fields[k])
            (inputs / "options.json").write_bytes(
                canonical(dict(profile=fields, limits=asdict(limits)))
            )
            for p in inputs.iterdir():
                p.chmod(0o444)
            started = time.monotonic()
            deadline = started + self.wall

            def remaining():
                if cancelled():
                    raise EvidenceError(
                        "STALE_ATTEMPT", "supervisor", "Worker authority revoked"
                    )
                left = deadline - time.monotonic()
                if left <= 0:
                    raise EvidenceError(
                        "WORKER_TIMEOUT", "supervisor", "Worker deadline exceeded"
                    )
                return max(0.01, min(5, left))

            work_bytes = (
                min(limits.disk_bytes, 1024**3) - size - limits.artifact_bytes - 1024**2
            )
            if work_bytes <= 0:
                raise EvidenceError(
                    "LIMIT_EXCEEDED", "supervisor", "Insufficient staging budget"
                )
            # The CPU quota multiplied by the independent container lifetime
            # stays below 600 aggregate seconds, including one CFS-period burst.
            created = True  # A timed-out create may still have created a container.
            self._run(
                "create",
                "--pull=never",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "16",
                "--memory",
                "2g",
                "--memory-swap",
                "2g",
                "--cpus",
                "0.65",
                "--user",
                "65534:65534",
                "--log-driver",
                "none",
                "--tmpfs",
                f"/work:rw,noexec,nosuid,nodev,size={work_bytes},mode=1777",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=1048576,mode=1777",
                "--mount",
                "type=bind,src=" + str(inputs) + ",dst=/input,readonly",
                "--env",
                "TMPDIR=/work",
                "--env",
                "OMP_THREAD_LIMIT=1",
                "--env",
                "OMP_NUM_THREADS=1",
                "--env",
                "OPENBLAS_NUM_THREADS=1",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONHASHSEED=0",
                self.image,
                "timeout",
                "--signal=KILL",
                str(self.wall),
                "python",
                "-m",
                "pdf_to_md.worker",
                "--operation",
                operation,
                timeout=remaining(),
            )
            self._run("start", name, timeout=remaining())
            error = False
            while True:
                state = json.loads(
                    self._run(
                        "inspect",
                        "--format",
                        "{{json .State}}",
                        name,
                        timeout=remaining(),
                    ).stdout
                )
                if not state["Running"]:
                    raise EvidenceError(
                        "WORKER_CRASH", "supervisor", "Worker did not complete"
                    )
                found = False
                for filename in ("error.json", "receipt.json"):
                    try:
                        self._run(
                            "exec",
                            name,
                            "test",
                            "-f",
                            "/work/" + filename,
                            timeout=remaining(),
                        )
                    except subprocess.CalledProcessError:
                        continue
                    found = True
                    error = filename == "error.json"
                    break
                if found:
                    break
                time.sleep(min(0.1, remaining()))
            result = stage / "result"
            result.mkdir()
            if error:
                self._copy(
                    name,
                    "/work/error.json",
                    result / "error.json",
                    4096,
                    deadline,
                    cancelled,
                )
                raw = (result / "error.json").read_bytes()
                refusal = decode(raw)
                if (
                    canonical(refusal) != raw
                    or set(refusal) != {"code", "stage"}
                    or refusal["code"] not in ERRORS
                    or refusal["stage"]
                    not in (
                        "receive",
                        "inspect",
                        "options",
                        "extract",
                        "ocr",
                        "numeric_reread",
                        "validate",
                        "bundle",
                        "worker",
                    )
                ):
                    raise EvidenceError(
                        "INVALID_RESULT", "supervisor", "Invalid worker refusal"
                    )
                raise EvidenceError(
                    refusal["code"], refusal["stage"], "Worker refused processing"
                )
            total = 0
            targets = ["manifest.json"] if operation == "extract" else ["receipt.json"]
            for artifact in targets:
                path = (
                    "/work/bundle/" + artifact
                    if operation == "extract"
                    else "/work/receipt.json"
                )
                total += self._copy(
                    name,
                    path,
                    result / artifact,
                    min(
                        limits.artifact_bytes - total,
                        (
                            limits.document_bytes
                            if artifact == "document.json"
                            else limits.artifact_bytes
                        ),
                    ),
                    deadline,
                    cancelled,
                )
                if artifact == "manifest.json":
                    raw = (result / artifact).read_bytes()
                    manifest = decode(raw)
                    from jsonschema import Draft202012Validator
                    from .evidence.validate import schema

                    try:
                        Draft202012Validator(
                            dict(schema(), **{"$ref": "#/$defs/Manifest"})
                        ).validate(manifest)
                        names = [entry["name"] for entry in manifest["artifacts"]]
                        if (
                            canonical(manifest) != raw
                            or names != sorted(set(names))
                            or not {"document.json", "issues.json", "reading.md"}
                            <= set(names)
                        ):
                            raise ValueError("invalid inventory")
                        if len(names) > limits.spans + 3 or any(
                            n not in ("document.json", "issues.json", "reading.md")
                            and not re.fullmatch(r"crop_s[0-9]{8}\.png", n)
                            for n in names
                        ):
                            raise ValueError("invalid artifact name")
                    except Exception as exc:
                        raise EvidenceError(
                            "INVALID_RESULT", "supervisor", "Invalid worker manifest"
                        ) from exc
                    targets.extend(names)
            remaining()
            if operation == "extract":
                receipt = validate_bundle(
                    result, limits=limits, expected_source_hash=source_hash
                )
                document = decode((result / "document.json").read_bytes())
                if self.qualification:
                    for key in ("runtime", "engine_build_sha256"):
                        if document["recipe"][key] != self.qualification[key]:
                            raise EvidenceError(
                                "INVALID_RESULT",
                                "supervisor",
                                "Unqualified worker inventory",
                            )
                artifacts = {p.name: p.read_bytes() for p in result.iterdir()}
            else:
                from .evidence.inspection import validate_inspection

                raw = (result / "receipt.json").read_bytes()
                receipt = decode(raw)
                validate_inspection(
                    receipt, limits=limits, source_hash=source_hash, source_bytes=size
                )
                if canonical(receipt) != raw:
                    raise EvidenceError(
                        "INVALID_RESULT", "supervisor", "Noncanonical inspection"
                    )
                artifacts = {}
            remaining()
            return dict(
                receipt=receipt,
                artifacts=artifacts,
                execution=dict(
                    image=self.image,
                    qualified=not self.development,
                    wall_milliseconds=round((time.monotonic() - started) * 1000),
                ),
            )
        except subprocess.TimeoutExpired as exc:
            raise EvidenceError(
                "WORKER_TIMEOUT", "supervisor", "Worker control deadline exceeded"
            ) from exc
        finally:
            if created:
                try:
                    self._run("rm", "--force", name, timeout=5)
                except Exception:
                    clean = False
                    # Retain private staging for operator reconciliation; never
                    # silently reuse a lease whose container exit is uncertain.
                    (stage / "cleanup-pending.json").write_bytes(
                        canonical({"container": name})
                    )
            if clean:
                shutil.rmtree(stage)
            else:
                raise EvidenceError(
                    "STORAGE_UNAVAILABLE",
                    "cleanup",
                    "Worker cleanup requires reconciliation",
                )
