# DEE-1 implementation status

This is an **incomplete implementation of DEE-1**, not an R1 release or approval
for customer rollout. Clausewerk has not been modified. The engineering spec
remains the target contract; the limitations below are not changes to it.

## Available now

The legacy baseline's missing scanned-fixture builder and scanned-grid OCR
failures are fixed. Grid preprocessing uses a separate recognition raster;
original pixels remain available for numeric rereads. Existing conversion
signatures and return codes remain unchanged.

The additive `pdf_to_md.evidence` SDK provides `Limits`, `Profile`,
`inspect_document`, `extract_document`, `validate_document`, `render_markdown`,
`write_bundle`, `validate_bundle`, `extract_package`, and `read_bundle`. The top-level package also exposes the
APIs named in §10 lazily. Caller streams remain open. New CLI commands are:

```sh
pdf2md inspect input.pdf --json
pdf2md extract input.pdf --profile evidence-conservative-1 --bundle output --assets regions --json
```

The bundle parent must already exist. Evidence extraction returns exit 3 for
partial/unreadable coverage or blocking issues, 0 for complete results, and 1
for execution failure. Existing files literally named `inspect` or `extract`
take precedence; use an explicit relative path for legacy filename clarity.

The core includes a published JSON Schema, generated TypeScript types,
independent Python/JavaScript canonical encoders, rational affine transforms,
literal observation ownership, mapped separators, OCR candidates and numeric
reread accounting, page coverage, restricted-page Markdown withholding,
immutable citation checks, and staged bundle publication with integrity checks.
`document.json` and `issues.json` are restricted artifacts. `reading.md` is an
ordinary projection. Local filesystem permissions do not substitute for the
host authorization required by the spec.

Schema generation is checked by tests:

```sh
.venv/bin/python src/pdf_to_md/evidence/schema/build.py
.venv/bin/python src/pdf_to_md/evidence/schema/generate_types.py
.venv/bin/python -m pytest tests/evidence -q
```

The schema adds `BLANK_PAGE_ASSESSED` as a versioned informational rule result.
The issue details currently use a closed `rule_id`/integer `measurements`
shape. Issue details and catalogs need further expansion before contract freeze.

## Structure handling

The evidence path now reuses the legacy geometry detectors while constructing
text only from retained observations. Digital ruled tables preserve rows, blank
cells, merged row/column spans and unknown header roles. Horizontal-rule and OCR
grids remain candidate structures with explicit issues. OCR cells with source
ink but no recognized word are unreadable, not blank. Cross-page table links
remain candidates; they do not silently join tables or remove repeated rows.

Document typography supplies digital heading levels, literal section labels,
list markers/depth, and repeated page furniture. OCR word-box height is not used
as a font-size measurement. Paragraph reflow records separators and retains
meaningful hyphens. Boxed/typographic callouts, nested table ownership, figures
with reciprocal caption links, and literal equation glyphs have separate blocks.
Reconstructed LaTeX stays explicitly inferred. Unclaimed images, curves, lines,
rectangles and annotations produce located gaps; this intentionally favors a
reviewable partial result over suppressing a small unfamiliar visual.

Independent validation checks observation conservation, complete grid coverage,
cell ownership, exact table concatenation and mappings, consistent grid edges,
resolved-cell containment, and caption/continuation relationships. The unreleased
1.0 schema now gives each cell an explicit `region_span_id`, referring to an
empty-observation geometry span also in `source_span_ids`. This refines §6's
cell source binding so list position cannot change which boundary is validated.
Generated TypeScript includes the field. Draft artifacts containing tables from
before this refinement must be re-extracted; no deployed contract migration is
claimed.

Private hash-checked page records are spooled under the disk budget and released
before the bounded document pass. PNG generation follows final paragraph merges.
`Profile(asset_mode="none")` is the dictionary-only extraction default.
`extract_package(..., profile=Profile(asset_mode="figures"))` returns an explicit
`EvidencePackage(document, assets)`. `figures` includes figure, equation and gap
crops; `regions` adds one designated region per block plus issue-bound regions,
not every provenance span. Pass both fields to `write_bundle(document, path,
assets=assets)`. `read_bundle(path)` reconstructs the package. Bundle validation
checks exact asset membership, PNG integrity, hashes and page-derived exposure.

Controls include merged/headerless/continuation grids, observation theft/lost
cells, mutated geometry, nested panels, crop origins and all quarter-turns,
OCR deskew localization, scanned blank versus unreadable cells, final-region
crop determinism, restricted crops, and spool tampering/cleanup. These generated
controls do not establish real-document accuracy or production qualification.

## Development worker

`pdf_to_md.supervisor.Supervisor` launches a disposable Linux Docker container.
It sets no network, read-only root/input, no capabilities, no new privileges,
a 16-task PID ceiling, 2-GiB memory/swap ceiling, bounded writable tmpfs, one
OpenMP thread, and a CPU quota whose maximum over the independent container
lifetime remains below 600 seconds. A container-side timeout remains active if
the host disappears. Outputs stream through bounded reads and are validated by
the host before return. Cleanup uncertainty retains private staging and refuses
success; `cleanup-pending.json` identifies the container for reconciliation.

Production construction requires a trusted deployment qualification record
binding image, qualification ID, expected runtime inventory and engine build.
The included Dockerfile and `development=True` are explicitly **unqualified**.
The development image has no Tesseract; OCR-unavailable results are intentional.
Do not use this image as an OCR-capable release image. The Dockerfile's build
inputs still need a release lock, native inventory and license/SBOM qualification.

```sh
docker build -f packaging/evidence/Dockerfile -t dee-worker:development .
DEE_TEST_IMAGE=$(docker image inspect dee-worker:development --format '{{.Id}}') \
  .venv/bin/python -m pytest tests/evidence/test_supervisor.py -q
```

The opt-in Docker controls exercise extraction, bounded inspection, deterministic
refusal, cancellation and staging cleanup. They do not qualify resource stress,
crash races, whole-tree cleanup under daemon failure, or a production workload.

## Outstanding work required before R1

- Finish native-frame capture, geometry/rotation/localization corpus, OCR
  preprocessing provenance, critical-token fixtures, undecodable glyph handling,
  issue-specific details and exact gap localization. Current gap regions can
  cover an affected page and must not be represented as exact damaged regions.
- Qualify visibility detection beyond nonrendering text and conservative filled
  panels. False positives and unsupported PDF constructs remain possible.
- Finish issue-specific dependency validation and qualification beyond the
  implemented ownership, table geometry/concatenation, and transform controls.
- Add durable job leases/generations, retry/idempotency, source epochs,
  nondeterministic recipe quarantine registry, staging GC, and operational
  receipts. The local publication lock is not a host job store.
- Implement Clausewerk's additive migrations, narrow service authority, exact
  database-verified selections, corrections/issue reviews, retention reach,
  source-scoped routes/search, settings and responsive source review UI. Follow
  its dependency testing and independent review policy before landing.
- Complete A01–A19 applicable R1 positive/negative and protection-removal
  controls, corpus/performance qualification, migration/rollback/restore proof,
  deployment/runbooks and release report. Enablement must remain off.

R2 HTTP/OpenAPI/MCP and R3 workflow/supplier adapters remain unimplemented.
No approval, retention or business-action capability was added to the engine.

See [traceability](evidence/traceability.json) for the bounded executable evidence.
The synthetic sample bundle is illustrative and binds the runtime that generated
it; it is not a cross-runtime golden extraction or customer material.
