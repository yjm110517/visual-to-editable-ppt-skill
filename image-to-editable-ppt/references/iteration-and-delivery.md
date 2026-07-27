# Iteration and Delivery Contract

Use this contract after deterministic review evaluation. It defines how the
orchestrator advances state, applies a Planner patch, records warning
acceptance, and packages an accepted iteration.

## State ownership

`manage_run_state.py` is event driven. Callers submit events; they cannot set an
arbitrary target state. `run_state.json` is bound to the canonical
`work/<topic>/request.json` by SHA-256 and to the request's maximum iteration
count.

Every history record contains the previous state, target state, reason, UTC
time, and—when an artifact caused the transition—the artifact's work-root
relative path and SHA-256. Existing iterations are immutable.

Use these events in order:

```text
init → inputs_resolved → spec_validated → build_started
→ structural_result → review_started → review_ready
→ evaluation_result
```

Structural failure may use `replan_after_structure`. A policy revision is
advanced by `apply_review_patch.py`, which atomically creates the next
iteration in `spec_ready`. A final-iteration warning candidate must use
`await_acceptance` before a user response is recorded.

## Patch transaction

`apply_review_patch.py` accepts exactly seven operation types:

- `update_element`
- `update_style`
- `recrop_asset`
- `replace_asset`
- `reclassify_element`
- `add_element`
- `remove_element`

Operations execute in declared order. Each must make a material change. The
script validates the Review, Evaluation, layout, crops, and manifest hashes
before staging any result. Any stale hash, unknown reference, invalid final
contract, or injected failure leaves both the current iteration and run state
byte-for-byte unchanged and removes the staged next iteration.

The next iteration inherits only specifications, still-valid assets, and a
still-valid SVG security report. It never inherits a PPTX, preview, QA report,
Review, Evaluation, Patch, or log.

An approved element is protected directly and through its referenced style or
asset. An operation affecting any protected dependency requires a non-empty
`override_reason`.

## Warning acceptance

Warnings are deliverable only at the configured final iteration. The state
pauses at `awaiting_user_acceptance` and records the Evaluation hash and visible
warning list.

The response command accepts an explicit `accept` or `reject`; it never infers
intent from prose. Before hashing the message, it normalizes Unicode to NFC and
line endings to LF. It preserves all other whitespace. Only the SHA-256 is
stored. The message content is not copied into the work directory or log.

An accepted warning remains paused until `create_delivery_decision.py`
atomically commits a `pass_with_warnings` decision and advances to `packaging`.
A rejection advances to `failed` and can never be packaged.

## Delivery decision and package

`create_delivery_decision.py` derives, rather than accepts, the result:

- `review_pass` plus structural pass and policy pass → `pass`
- accepted final warning candidate → `pass_with_warnings`
- structural/policy failure or explicit rejection → `fail`

`package_output.py` accepts only a `packaging` state and a matching successful
decision. It revalidates the request, QA, Review, Evaluation, PPTX, preview,
layout, crops, asset manifest, assets, approval evidence, and their hash chain.

The committed delivery directory contains exactly seven files:

```text
<name>_editable.pptx
<name>_assets.zip
<name>_preview.png
<name>_qa_report.json
<name>_review_report.json
<name>_review_evaluation.json
<name>_delivery_decision.json
```

Source images, specifications, logs, raw Agent responses, `.agent-calls`, and
non-accepted iterations never enter delivery. Staging is committed atomically.
An existing byte-identical directory may complete an interrupted packaging
transition; any content conflict returns exit code 9 without overwriting it.

## Commands

```bash
python scripts/manage_run_state.py init \
  --work-root work/topic \
  --request work/topic/request.json \
  --output work/topic/run_state.json \
  --run-id task-001

python scripts/manage_run_state.py advance \
  --work-root work/topic \
  --state work/topic/run_state.json \
  --event evaluation_result \
  --artifact work/topic/iterations/01/review_evaluation.json \
  --run-id task-001

python scripts/apply_review_patch.py \
  --work-root work/topic \
  --run-state work/topic/run_state.json \
  --current-dir work/topic/iterations/01 \
  --patch work/topic/iterations/01/review_patch.json \
  --next-dir work/topic/iterations/02 \
  --run-id task-001

python scripts/manage_run_state.py warning-response \
  --work-root work/topic \
  --state work/topic/run_state.json \
  --decision accept \
  --actor-type user \
  --message-file acceptance.txt \
  --run-id task-001

python scripts/create_delivery_decision.py \
  --work-root work/topic \
  --run-state work/topic/run_state.json \
  --request work/topic/request.json \
  --iteration-dir work/topic/iterations/03 \
  --output work/topic/delivery_decision.json \
  --run-id task-001

python scripts/package_output.py \
  --work-root work/topic \
  --run-state work/topic/run_state.json \
  --delivery-decision work/topic/delivery_decision.json \
  --ppt work/topic/iterations/03/topic_editable.pptx \
  --dist-root dist \
  --output-name topic \
  --run-id task-001
```

All commands use the common JSON stdout, JSONL diagnostics, and exit-code
contract. State or hash conflicts return 9. A delivery gate refusal returns 10.
