---
name: image-to-editable-ppt
description: Convert user-provided screenshots, slide images, or visual designs into editable PowerPoint presentations using native text and shapes plus isolated image assets. Use when Codex needs to recreate, reproduce, or convert a reference image into an editable PPT or PPTX, including iterative visual review and delivery artifacts.
---

# Image to Editable PPT

## Core requirements

- Rebuild readable text as native PowerPoint text.
- Rebuild cards, borders, lines, arrows, labels, and simple diagrams as native shapes.
- Use isolated PNG, JPEG, or sanitized SVG assets for complex visuals that do not need native editing, including polished source icons whose identity depends on gradients, highlights, shadows, depth, texture, or irregular detail.
- Never replace a distinctive source icon with a Unicode glyph, letter, emoji, or generic polygon merely to maximize native editability.
- Never use the complete source image as the final slide background or rasterize a text-bearing card.
- Preserve semantic connector topology. A closed loop, curved cycle, merge, or branch must remain visibly connected and directional; do not flatten it into disconnected straight segments.
- Place connector endpoints close to their source and destination boundaries, and verify that every arrowhead remains clearly visible at final render size. A semantically correct but visibly floating arrow is not acceptable.
- Inspect every placed crop at render size. Reject visible rectangular crop edges, incompatible tile backgrounds, clipped effects, and decorative background seams that are not present in the source.
- Decompose numbered steps into a native badge shape, native number text, isolated complex icon asset, and native card/text/connector objects. Never allow complete or partial neighboring numbers, labels, body text, or borders inside an icon crop.
- Require a persistent `boundary_policy` for every cropped asset and require `asset_processing_report.json` before building. Treat that report as pixel-edge evidence only; semantic crop contamination still requires independent visual review.
- Preserve stable element and asset IDs across iterations.
- Save each iteration separately and never overwrite an earlier iteration.

## Resolve typography

Apply explicit user typography first. Otherwise select the interaction mode:

- `ask`: ask once for title font, title size, body font, and body size.
- `match-source`: infer the hierarchy from the reference image.
- `default`: use Microsoft YaHei 32 pt for titles and Microsoft YaHei 18 pt for body text.

Treat requests such as "directly proceed," "match the image," or "do not ask" as `match-source`. Record the resolved values in `request.json` and do not ask again during later iterations.

## Route responsibilities

Keep the roles independent:

- Let the Skill Orchestrator manage user interaction, role calls, state transitions, iteration limits, input isolation, and delivery decisions.
- Let the Layout Planner analyze the source and produce layout, crop, and asset specifications or a review patch.
- Let the Visual Reviewer compare the source, render, and structural QA data without modifying the slide or computing the final policy decision.
- Let deterministic scripts validate contracts, process assets, build and render the presentation, verify structure, evaluate review policy, apply patches, and package accepted output.

Do not let the Planner approve its own output or let the Reviewer modify specifications.
A role configuration file does not execute an Agent. The Orchestrator must explicitly open the Reviewer checkpoint, execute the prepared package in a fresh context, finalize the response, evaluate it, and pass the final review gate.

## Execute the workflow

1. Confirm that a readable source image and conversion request exist.
2. Resolve typography and freeze the normalized request.
3. Generate and validate `layout.json`, `crops.json`, and `asset_manifest.json`.
4. Run deterministic asset processing, PPT construction, font audit, rendering, and structural verification in production mode with `run_state.json`.
5. Stop before visual review when a hard structural QA gate fails.
6. Run the Visual Reviewer with only the approved source, render, structural QA, request summary, and provenance inputs. Require an explicit side-by-side check of connector topology, key proportions, crop edges, background seams, and visual depth even when structural QA passes.
7. Calculate scores, issue counts, editability, and policy status with deterministic evaluation code.
8. Apply an approved review patch transactionally to a new iteration when revision is required.
9. Stop after at most three iterations unless the request explicitly changes the limit.
10. Package only the iteration named by a validated delivery decision.

Never describe a structural QA pass as final completion. `run_pipeline.py` returns `deliverable: false` and `visual_review_status: pending`; it proves only that the iteration is structurally reviewable. Before a normal delivery, run `assert_review_gate.py` and require `visual_review_gate: pass`.

## Enforce review and delivery gates

- Treat `critical + recoverable` as revision and `critical + irrecoverable` as failure.
- Require no critical or major issues, all hard QA gates, and the configured score threshold for a normal pass.
- Require every mandatory visual check to pass or be explicitly `not_applicable` with a reason. A failed mandatory check forces revision or failure even when aggregate scores would otherwise pass.
- Reject a review whose Planner and Reviewer share the same context ID.
- Pause in `awaiting_user_acceptance` for a warning candidate and store the approval message hash before producing `pass_with_warnings`.
- Deliver the editable PPT, asset archive, preview, QA report, review report, review evaluation, and delivery decision from the same accepted iteration.
- Record input, specification, asset, tool, renderer, model, prompt, rubric, parameter, and call provenance where required by the current contract.

## Follow the execution contract

Read [references/agent-orchestration.md](references/agent-orchestration.md) before invoking or implementing a deterministic command. Follow its path, output, logging, and exit-code rules.

Read [references/element-classification.md](references/element-classification.md) before choosing native PowerPoint, raster, or SVG representation, and before applying a text-editability exemption.

Read [references/ppt-build-contract.md](references/ppt-build-contract.md) before invoking or modifying the deterministic PptxGenJS builder.

Read [references/rendering-and-qa.md](references/rendering-and-qa.md) before auditing fonts, rendering a PPTX, verifying structural editability, or invoking the single-iteration pipeline.

Read [references/visual-review-rubric.md](references/visual-review-rubric.md) before preparing a Reviewer call or evaluating a visual review. Use `agents/planner.yaml` and `agents/visual_reviewer.yaml` as separate fresh-context role contracts; never continue a Planner conversation as the Reviewer.

Read [references/iteration-and-delivery.md](references/iteration-and-delivery.md) before advancing run state, applying a review patch, recording warning acceptance, creating a delivery decision, or packaging accepted output.
