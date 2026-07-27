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

## Issues

Use `critical`, `major`, `minor`, or `suggestion` independently from `recoverable`, `irrecoverable`, or `unknown`. A missing line of body text may be critical and recoverable; an unreadable source may be critical and irrecoverable.

Every non-suggestion issue must identify an element, an asset, `slide-root`, or a normalized region. Regions use `x`, `y`, `w`, and `h` in the range 0–1, with positive width and height and with `x + w <= 1` and `y + h <= 1`.

List an element in `approved_elements` only when it needs no non-suggestion change. Do not compute an overall score or policy decision.
