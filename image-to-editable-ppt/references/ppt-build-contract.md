# PPT Build Contract

Read this reference before invoking or changing the PptxGenJS builder.

## Inputs and outputs

Build one slide from a validated `layout.json` and build-ready asset manifest. Keep the layout, manifest, asset directory, SVG report, PPTX, build summary, and log inside the same iteration directory.

```bash
node scripts/build_slide.mjs \
  --iteration-dir work/topic/iterations/01 \
  --layout work/topic/iterations/01/layout.json \
  --asset-manifest work/topic/iterations/01/asset_manifest.json \
  --asset-dir work/topic/iterations/01/assets \
  --svg-report work/topic/iterations/01/svg_security_report.json \
  --output work/topic/iterations/01/topic_editable.pptx \
  --build-summary work/topic/iterations/01/build_summary.json \
  --python .venv/Scripts/python.exe \
  --run-id task-001 \
  --iteration 1 \
  --log-file work/topic/iterations/01/pipeline.log
```

The SVG report is required only when the manifest contains SVG. Python selection is `--python`, then `IVT_PYTHON`, then `python` on PATH.

## Build rules

- Support only `text`, `shape`, `line`, and `image` elements in v1.3 P3.
- Build by ascending `z_index`, preserving layout array order for ties.
- Write `ivt:<element_id>` to the PowerPoint selection-pane object name. Use `ivt:<element_id>#<part>` only for a real one-to-many mapping.
- Resolve text properties in the order Run, element, referenced style. Every Run must resolve a font face, font size, and color.
- Keep requested font names even when the local system may not contain the font. P4 performs installation and fallback auditing.
- Resolve images only through the validated asset index returned by `validate_assets.py --emit-resolved-assets`. Recheck asset hashes immediately before embedding.
- Set `rounding: true` only when the source image is visibly clipped to a circular or elliptical mask. Use it to prevent rectangular crop corners from leaking outside a circular composition; do not apply it to arbitrary icons.
- Treat `from_id` and `to_id` as QA metadata, not dynamic connector attachment. Use both fields only for a real semantic relationship; decorative stems, dividers, underlines, and leader accents omit both. Never provide only one field.
- Use `relationship_groups` to declare ordered semantic structures such as a
  `closed_cycle`. Preflight validation must prove the connector IDs form the
  stated node-to-node sequence, have visible destination arrowheads, stay close
  to node boundaries, and avoid the inner text-safe region.
- Use `geometry: straight` for ordinary line segments, `geometry: arc` for native editable quarter-arcs, and `geometry: curve` with explicit start, end, and cubic control points when the connector must meet specific node boundaries. Curve coordinates are relative to the element bounding box and must remain inside it. Preserve the source flow direction with arrowheads at the destination; do not flatten a curved cycle into disconnected straight segments.
- Reject groups, tables, charts, freeform paths, gradients, and complex effects rather than silently rasterizing them.
- Preflight slide-bound checks allow only 0.5 pt of deterministic serialization/rounding tolerance at the right and bottom edges. This tolerance is not permission to design outside the slide; larger overflow still requires `allow_overflow: true` and remains subject to structural QA.

## Transaction and reproducibility

Never overwrite an existing PPTX or build summary. Build both in a temporary iteration-local directory, validate the PPTX package and summary, then commit both. Remove the committed PPTX if summary commit fails.

Normalize the PPTX core timestamps and ZIP entry timestamps, ordering, permissions, and compression. Identical inputs, run ID, iteration, and output name must produce identical PPTX and build-summary bytes.
