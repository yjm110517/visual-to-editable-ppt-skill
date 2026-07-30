# Visual Review Rubric

## Review dimensions

- `content_accuracy`: wording, numbers, punctuation, labels, emphasis, and user requirements.
- `layout_similarity`: page geometry, margins, spacing, alignment, hierarchy, and flow.
- `typography_similarity`: font appearance, size, weight, line breaks, line spacing, and paragraph rhythm.
- `visual_style_similarity`: colors, borders, shadows, transparency, corner radii, gradients, and overall style.
- `asset_quality`: asset identity, crop, clarity, edge quality, scale, and placement.

Do not score editability. Report a visible editability conflict as an issue; deterministic structural QA supplies the editability score.

## Score anchors

| Score | Meaning |
|---|---|
| 98–100 | Essentially identical; only imperceptible differences remain. |
| 95–97 | A small number of minor differences do not affect the whole. |
| 90–94 | Multiple minor differences exist, with no major issue. |
| 80–89 | At least one major difference exists. |
| 60–79 | Multiple major differences or severe local distortion exist. |
| 0–59 | A critical failure exists in the dimension. |

For a dimension, a critical issue caps its score at 59, a major issue at 89, one minor issue at 97, and two or more minor issues at 94. Suggestions do not force a deduction.

Category mapping:

- `content` and `user_requirement` → `content_accuracy`
- `layout` → `layout_similarity`
- `typography` → `typography_similarity`
- `style` → `visual_style_similarity`
- `asset` → `asset_quality`
- `editability` → deterministic structural editability

## Mandatory visual failure checks

Apply these checks before assigning scores:

| Failure | Minimum severity | Dimension |
|---|---|---|
| Missing, reversed, or disconnected semantic connector | `major`; `critical` when it changes the process meaning | `layout_similarity` |
| Closed loop flattened into unrelated straight segments | `major` | `layout_similarity` |
| Connector endpoints visibly float between nodes or an arrowhead is not clearly visible | `major` when repeated or meaning is ambiguous; otherwise `minor` | `layout_similarity` |
| Key central or side-panel proportion visibly changes the hierarchy | `major` | `layout_similarity` |
| Opaque rectangular crop boundary appears where the source has no tile | `major` | `asset_quality` |
| A crop contains a complete or partial neighboring number badge, label, body text, border, or unrelated component | at least `major` | `asset_quality` |
| Circular source artwork leaks rectangular corners | `major` | `asset_quality` |
| Local background crop creates a hard rectangular seam | `major` | `visual_style_similarity` and `asset_quality` |
| Multiple source depth cues are flattened at once, such as card borders, shadows, highlights, and layered background | `major` | `visual_style_similarity` |

Structural QA is independent evidence. A report with complete object IDs, native text, and no overflow can still require revision for any failure above.

The raw response must complete all seven `mandatory_visual_checks` fields:
connector topology, connector endpoints, key proportions, crop boundaries,
background seams, visual depth, and typography hierarchy. A failed check must
reference a non-suggestion issue. A check may be `not_applicable` only when the
source lacks that feature and the rationale states why. The `crop_boundaries`
check covers both pixel-edge quality and semantic crop
purity. A deterministic Alpha/edge report cannot prove that a complete
neighboring badge or label was not included. Any failed mandatory check
prevents a policy pass, regardless of the aggregate score.

## Issues

Use `critical`, `major`, `minor`, or `suggestion` independently from `recoverable`, `irrecoverable`, or `unknown`. A missing line of body text may be critical and recoverable; an unreadable source may be critical and irrecoverable.

Every non-suggestion issue must identify an element, an asset, `slide-root`, or a normalized region. Regions use `x`, `y`, `w`, and `h` in the range 0–1, with positive width and height and with `x + w <= 1` and `y + h <= 1`.

List an element in `approved_elements` only when it needs no non-suggestion change. Do not compute an overall score or policy decision.
