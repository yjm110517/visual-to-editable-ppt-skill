# Rendering and Structural QA Contract

Read this reference before rendering a generated PPTX, auditing fonts, verifying structure, or running a complete deterministic iteration.

## Font audit

- Audit text boxes and table-cell text at paragraph and Run level.
- Reconcile P3 text objects with `build_summary.typography.font_resolutions`.
- Require every tracked Run to contain the expected size and matching `a:latin`, `a:ea`, and `a:cs` typefaces.
- Treat a missing installed font, missing Run, unexpected Run, or font declaration mismatch as a violation.
- Never substitute a font silently. Let structural QA stop the iteration.

## Rendering

- Render exactly one slide.
- Use `layout.source.width_px` and `height_px` unless both dimensions are explicitly overridden with the same slide aspect ratio.
- In `auto` mode, try Microsoft PowerPoint first and LibreOffice second.
- Use an independent PowerPoint COM instance and open the presentation read-only with no document window.
- Use an isolated LibreOffice user profile and `impress_png_Export` with explicit pixel dimensions.
- Validate the PNG dimensions and re-encode it without metadata before committing it.
- Record every attempted renderer, the exact successful version, fallback use, and PPT/render hashes.

## Structural verification

- Treat actual OOXML as the source of truth; do not accept build-summary counts without reconciliation.
- Read object IDs from `p:cNvPr/@name`. Strip only a valid `#part` suffix when calculating the base element ID.
- Require actual object names to equal the names registered in `build_summary.element_map`.
- Require text, shape, line, and image elements to remain their expected native PowerPoint object types.
- Check image relationships and package media targets.
- Compute rotated axis-aligned bounds from OOXML transforms with a 0.5 pt rounding tolerance. Skip only elements explicitly marked `allow_overflow`.
- Calculate `editable_text_ratio` from `editability_required=true` text elements. Emit `null/not_applicable` when the denominator is zero.
- Validate text-bearing image exemptions against both layout and manifest.

Any invalid package, page count, hash, object ID, native type, text ratio, exemption, bound, media relationship, font audit, or render count is a hard structural failure. A structural report says only `pass` or `fail`; it never substitutes for visual review.

A structural pass means only `structurally reviewable`. It must be represented
as `deliverable: false` and `visual_review_status: pending` until the
independent Reviewer, deterministic evaluation, and final review assertion
have passed. Do not use a diagnostic pipeline invocation as delivery evidence.

## Single-iteration transaction

Run stages in this order:

```text
validate_spec preflight
→ crop_assets
→ sanitize_svg
→ validate_spec build-ready
→ build_slide
→ audit_fonts
→ render_ppt
→ verify_ppt
```

Execute the chain in a sibling staging work tree. Commit the updated assets, manifest, PPTX, build summary, font audit, render, render report, QA report, and log only after the result is internally consistent. On structural failure, commit the coherent diagnostic set and return exit code `8`. On an earlier failure, preserve only the pipeline log and leave iteration inputs unchanged.

Never create a delivery decision, `dist/` directory, or delivery ZIP during this stage.

Use `SOURCE_DATE_EPOCH` to freeze report time and `IVT_SKILL_REVISION` to freeze revision provenance in reproducibility tests.
