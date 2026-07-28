import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rename, rm, stat, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import JSZip from "jszip";
import PptxGenJS from "pptxgenjs";

import { BuildError, enforceIterationBoundary, failure, logEvent, parseArgs, readJson, sha256Bytes, sha256File, success } from "./build_common.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const FIXED_DATE = new Date("1980-01-01T00:00:00.000Z");
const COMPONENT = "build_slide";

export function normalizeColor(value, explicitTransparency) {
  const cleaned = value.slice(1).toUpperCase();
  if (cleaned.length === 8) {
    const alpha = Number.parseInt(cleaned.slice(6), 16);
    return { color: cleaned.slice(0, 6), transparency: Math.round((1 - alpha / 255) * 100) };
  }
  return explicitTransparency === undefined ? { color: cleaned } : { color: cleaned, transparency: explicitTransparency };
}

function fillOptions(fill) {
  return fill ? normalizeColor(fill.color, fill.transparency) : { color: "FFFFFF", transparency: 100 };
}

function lineOptions(line) {
  if (!line) return { color: "FFFFFF", transparency: 100, width: 0 };
  return {
    ...normalizeColor(line.color, line.transparency),
    width: line.width_pt,
    dashType: line.dash ?? "solid",
    beginArrowType: line.begin_arrow ?? "none",
    endArrowType: line.end_arrow ?? "none",
  };
}

function shadowOptions(shadow) {
  if (!shadow) return undefined;
  return { type: "outer", color: shadow.color.slice(1), opacity: shadow.opacity, blur: shadow.blur_pt, angle: shadow.angle, offset: shadow.offset_pt, rotateWithShape: false };
}

function bulletOptions(bullet) {
  if (bullet === undefined) return undefined;
  if (bullet === true) return true;
  return {
    type: bullet.type,
    characterCode: bullet.character_code,
    numberType: bullet.style,
    numberStartAt: bullet.start_at,
    indent: bullet.indent_pt,
  };
}

function marginOptions(margin) {
  if (margin === undefined) return undefined;
  if (Array.isArray(margin)) return margin.map((value) => value * 72);
  return margin * 72;
}

function styleFor(layout, element) {
  return element.style_ref ? layout.styles[element.style_ref] : {};
}

function pick(value, fallback) {
  return value === undefined ? fallback : value;
}

function textRunOptions(run, element, style) {
  const fontFace = pick(run.font_face, pick(element.font_face, style.font_face));
  const fontSize = pick(run.font_size_pt, pick(element.font_size_pt, style.font_size_pt));
  const colorValue = pick(run.color, pick(element.color, style.color));
  const transparency = pick(run.transparency, pick(element.transparency, style.transparency));
  const color = normalizeColor(colorValue, transparency);
  return {
    fontFace,
    fontSize,
    ...color,
    bold: pick(run.bold, pick(element.bold, style.bold)),
    italic: pick(run.italic, pick(element.italic, style.italic)),
    underline: pick(run.underline, pick(element.underline, style.underline)),
    lang: pick(run.language, pick(element.language, style.language)),
    breakLine: run.break_line,
    bullet: bulletOptions(pick(run.bullet, pick(element.bullet, style.bullet))),
  };
}

export function stableBuildOrder(elements) {
  return elements.map((element, index) => ({ element, index })).sort((left, right) => left.element.z_index - right.element.z_index || left.index - right.index);
}

function basePosition(element, objectName) {
  return { x: element.x, y: element.y, w: element.w, h: element.h, rotate: element.rotation ?? 0, objectName };
}

function buildText(slide, layout, element, typography) {
  const style = styleFor(layout, element);
  const rawRuns = element.runs ?? [{ text: element.text }];
  const runs = rawRuns.map((run, index) => {
    const options = textRunOptions(run, element, style);
    const source = run.font_face ? "run" : element.font_face ? "element" : "style";
    typography.font_resolutions.push({ element_id: element.id, run_index: index, font_face: options.fontFace, font_size_pt: options.fontSize, source });
    if (source === "run") typography.explicit_run_font_count += 1;
    else typography.inherited_run_font_count += 1;
    typography.run_count += 1;
    return { text: run.text, options };
  });
  const fit = pick(element.fit, style.fit) ?? "none";
  if (fit !== "none") typography.non_default_fit_elements.push(element.id);
  const textOptions = {
    ...basePosition(element, `ivt:${element.id}`),
    align: pick(element.align, style.align) ?? "left",
    valign: pick(element.valign, style.valign) ?? "top",
    margin: marginOptions(pick(element.margin_in, style.margin_in) ?? 0),
    lineSpacing: pick(element.line_spacing_pt, style.line_spacing_pt),
    lineSpacingMultiple: pick(element.line_spacing_multiple, style.line_spacing_multiple),
    paraSpaceBeforePt: pick(element.para_space_before_pt, style.para_space_before_pt),
    paraSpaceAfterPt: pick(element.para_space_after_pt, style.para_space_after_pt),
    fit: fit === "resize-shape" ? "resize" : fit,
    breakLine: false,
    fill: { color: "FFFFFF", transparency: 100 },
    line: { color: "FFFFFF", transparency: 100, width: 0 },
  };
  slide.addText(runs, textOptions);
  typography.text_element_count += 1;
}

function buildShape(pptx, slide, layout, element) {
  const style = styleFor(layout, element);
  slide.addShape(pptx.ShapeType[element.shape], {
    ...basePosition(element, `ivt:${element.id}`),
    fill: fillOptions(element.fill ?? style.fill),
    line: lineOptions(element.line ?? style.line),
    shadow: shadowOptions(element.shadow ?? style.shadow),
  });
}

function buildLine(pptx, slide, element) {
  const geometry = element.geometry ?? "straight";
  if (geometry === "curve") {
    const { start, control1, control2, end } = element.curve;
    const points = [start, control1, control2, end];
    if (points.some((point) => point.x > element.w || point.y > element.h)) {
      throw new BuildError("curve point exceeds the element bounding box", { category: "invalid_curve", target: element.id });
    }
    slide.addShape(pptx.ShapeType.custGeom, {
      ...basePosition(element, `ivt:${element.id}`),
      fill: { color: "FFFFFF", transparency: 100 },
      line: lineOptions(element.line),
      points: [
        { x: start.x, y: start.y, moveTo: true },
        { x: end.x, y: end.y, curve: { type: "cubic", x1: control1.x, y1: control1.y, x2: control2.x, y2: control2.y } },
      ],
    });
    return;
  }
  const shapeType = geometry === "arc" ? pptx.ShapeType.arc : pptx.ShapeType.line;
  slide.addShape(shapeType, { ...basePosition(element, `ivt:${element.id}`), fill: { color: "FFFFFF", transparency: 100 }, line: lineOptions(element.line) });
}

export async function verifyResolvedAsset(asset) {
  try {
    const fileStat = await stat(asset.path);
    if (fileStat.size !== asset.size_bytes || await sha256File(asset.path) !== asset.sha256) {
      throw new BuildError("asset changed after security validation", { exitCode: 9, category: "hash_conflict", target: asset.id });
    }
  } catch (error) {
    if (error instanceof BuildError) throw error;
    throw new BuildError("validated asset became unreadable", { exitCode: 9, category: "hash_conflict", target: asset.id });
  }
}

async function buildImage(slide, element, assets, usedAssets) {
  const asset = assets.get(element.asset_id);
  if (!asset) throw new BuildError("unknown asset id", { category: "unknown_asset", target: element.asset_id });
  await verifyResolvedAsset(asset);
  // PptxGenJS falls back to the absolute source path when altText is empty,
  // which makes otherwise identical builds depend on the staging directory.
  const options = { ...basePosition(element, `ivt:${element.id}`), path: asset.path, altText: element.alt_text ?? element.asset_id, rounding: element.rounding ?? false };
  if (element.fit !== "stretch") options.sizing = { type: element.fit, w: element.w, h: element.h };
  slide.addImage(options);
  usedAssets.set(asset.id, { asset_id: asset.id, type: asset.type, sha256: asset.sha256 });
}

function runProcess(executable, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (value) => { stdout += value; });
    child.stderr.on("data", (value) => { stderr += value; });
    child.on("error", (error) => reject(new BuildError(`Python runtime is unavailable: ${error.message}`, { exitCode: 5, category: "environment_error", target: executable })));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
}

async function validateAndResolveAssets(args, python) {
  const command = [path.join(SCRIPT_DIR, "validate_assets.py"), "--asset-dir", args["asset-dir"], "--asset-manifest", args["asset-manifest"], "--layout", args.layout, "--emit-resolved-assets", "--run-id", args["run-id"], "--iteration", String(args.iteration)];
  if (args["svg-report"]) command.push("--svg-report", args["svg-report"]);
  if (args["schema-dir"]) command.push("--schema-dir", args["schema-dir"]);
  if (args["log-file"]) command.push("--log-file", args["log-file"]);
  const completed = await runProcess(python, command);
  let payload;
  try { payload = JSON.parse(completed.stdout); } catch { payload = null; }
  if (completed.code !== 0 || payload?.status !== "ok") {
    const detail = payload?.error;
    throw new BuildError(detail?.message ?? `asset validation failed: ${completed.stderr.trim()}`, { exitCode: detail?.exit_code ?? 5, category: detail?.category ?? "asset_validation", target: detail?.path ?? args["asset-manifest"] });
  }
  return payload.outputs;
}

async function normalizePptx(buffer) {
  const source = await JSZip.loadAsync(buffer);
  const normalized = new JSZip();
  for (const name of Object.keys(source.files).sort()) {
    const entry = source.files[name];
    if (entry.dir) {
      normalized.file(name, "", { dir: true, date: FIXED_DATE, unixPermissions: 0o755 });
      continue;
    }
    let content = await entry.async("nodebuffer");
    if (name === "docProps/core.xml") {
      let xml = content.toString("utf8");
      xml = xml.replace(/<dcterms:created[^>]*>.*?<\/dcterms:created>/s, '<dcterms:created xsi:type="dcterms:W3CDTF">1980-01-01T00:00:00Z</dcterms:created>');
      xml = xml.replace(/<dcterms:modified[^>]*>.*?<\/dcterms:modified>/s, '<dcterms:modified xsi:type="dcterms:W3CDTF">1980-01-01T00:00:00Z</dcterms:modified>');
      content = Buffer.from(xml, "utf8");
    }
    normalized.file(name, content, { date: FIXED_DATE, binary: true, unixPermissions: 0o644 });
  }
  const result = await normalized.generateAsync({ type: "nodebuffer", platform: "UNIX", compression: "DEFLATE", compressionOptions: { level: 9 }, streamFiles: false });
  const check = await JSZip.loadAsync(result);
  for (const required of ["[Content_Types].xml", "ppt/presentation.xml", "ppt/slides/slide1.xml", "docProps/core.xml"]) {
    if (!check.file(required)) throw new BuildError("PPTX package is missing a required part", { exitCode: 6, category: "pptx_integrity", target: required });
  }
  return result;
}

async function validateSummary(summaryPath, python, args) {
  const command = [path.join(SCRIPT_DIR, "validate_spec.py"), "--build-summary", summaryPath];
  if (args["schema-dir"]) command.push("--schema-dir", args["schema-dir"]);
  const completed = await runProcess(python, command);
  if (completed.code !== 0) throw new BuildError("generated build summary failed Schema validation", { exitCode: 6, category: "summary_invalid", target: summaryPath });
}

export async function buildPresentation(args) {
  const inputs = [args.layout, args["asset-manifest"], args["asset-dir"], args["svg-report"]];
  const outputs = [args.output, args["build-summary"]];
  const iterationDir = await enforceIterationBoundary(args["iteration-dir"], inputs, outputs, [args["log-file"]]);
  args.__logAuthorized = true;
  await logEvent(args["log-file"], { level: "info", component: COMPONENT, event: "started", message: "PPT build started", runId: args["run-id"], iteration: args.iteration });
  const layout = await readJson(args.layout);
  if (layout.metadata?.iteration !== args.iteration) throw new BuildError("CLI iteration does not match layout metadata", { category: "iteration_mismatch", target: "--iteration" });
  const python = args.python ?? process.env.IVT_PYTHON ?? "python";
  const resolved = await validateAndResolveAssets(args, python);
  if (await sha256File(args.layout) !== resolved.layout_sha256 || await sha256File(args["asset-manifest"]) !== resolved.manifest_sha256) {
    throw new BuildError("layout or asset manifest changed after validation", { exitCode: 9, category: "hash_conflict" });
  }

  const pptx = new PptxGenJS();
  pptx.defineLayout({ name: "IVT_CUSTOM", width: layout.slide.width_in, height: layout.slide.height_in });
  pptx.layout = "IVT_CUSTOM";
  pptx.author = "Image to Editable PPT Skill";
  pptx.company = "";
  pptx.subject = "Deterministic editable PowerPoint build";
  pptx.title = layout.metadata.topic;
  pptx.revision = "1";
  const slide = pptx.addSlide();
  slide.background = { color: layout.slide.background.slice(1).toUpperCase() };

  const assets = new Map(resolved.resolved_assets.map((asset) => [asset.id, asset]));
  const usedAssets = new Map();
  const typography = { text_element_count: 0, run_count: 0, explicit_run_font_count: 0, inherited_run_font_count: 0, unresolved_font_count: 0, non_default_fit_elements: [], font_resolutions: [] };
  const ordered = stableBuildOrder(layout.elements);
  const elementMap = [];
  const connections = [];
  for (const { element } of ordered) {
    if (element.type === "text") buildText(slide, layout, element, typography);
    else if (element.type === "shape") buildShape(pptx, slide, layout, element);
    else if (element.type === "line") {
      buildLine(pptx, slide, element);
      connections.push({ element_id: element.id, from_id: element.from_id ?? null, to_id: element.to_id ?? null });
    } else if (element.type === "image") await buildImage(slide, element, assets, usedAssets);
    else throw new BuildError("unsupported element type", { category: "unsupported_element", target: element.type });
    elementMap.push({ element_id: element.id, type: element.type, object_names: [`ivt:${element.id}`], object_count: 1 });
  }

  const builtIds = new Set(elementMap.map((item) => item.element_id));
  const expectedIds = layout.elements.map((item) => item.id);
  const missing = expectedIds.filter((id) => !builtIds.has(id));
  const unexpected = [...builtIds].filter((id) => !expectedIds.includes(id));
  if (missing.length || unexpected.length || builtIds.size !== expectedIds.length) throw new BuildError("element reconciliation failed", { exitCode: 6, category: "element_reconciliation" });

  await mkdir(path.dirname(args.output), { recursive: true });
  const temporary = await mkdtemp(path.join(iterationDir, ".build-slide-"));
  const stagedPptx = path.join(temporary, path.basename(args.output));
  const stagedSummary = path.join(temporary, path.basename(args["build-summary"]));
  let outputCommitted = false;
  try {
    const raw = await pptx.write({ outputType: "nodebuffer", compression: true });
    const pptxBytes = await normalizePptx(Buffer.from(raw));
    await writeFile(stagedPptx, pptxBytes);
    const summary = {
      schema_version: "1.3",
      run_id: args["run-id"],
      iteration: args.iteration,
      hashes: { layout_sha256: resolved.layout_sha256, asset_manifest_sha256: resolved.manifest_sha256, output_pptx_sha256: sha256Bytes(pptxBytes) },
      output_pptx: path.relative(iterationDir, args.output).split(path.sep).join("/"),
      expected_element_count: expectedIds.length,
      built_element_count: builtIds.size,
      missing_element_ids: missing,
      unexpected_element_ids: unexpected,
      build_order: ordered.map(({ element }) => element.id),
      element_map: elementMap.sort((left, right) => expectedIds.indexOf(left.element_id) - expectedIds.indexOf(right.element_id)),
      assets: [...usedAssets.values()].sort((left, right) => left.asset_id.localeCompare(right.asset_id)),
      typography,
      connections,
      warnings: [],
    };
    await writeFile(stagedSummary, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
    await validateSummary(stagedSummary, python, args);
    await rename(stagedPptx, args.output);
    outputCommitted = true;
    await rename(stagedSummary, args["build-summary"]);
    return summary;
  } catch (error) {
    if (outputCommitted) await unlink(args.output).catch(() => {});
    await unlink(args["build-summary"]).catch(() => {});
    throw error;
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
    const summary = await buildPresentation(args);
    const outputs = { pptx: path.resolve(args.output), build_summary: path.resolve(args["build-summary"]), pptx_sha256: summary.hashes.output_pptx_sha256, element_count: summary.built_element_count, build_order: summary.build_order };
    await logEvent(args["log-file"], { level: "info", component: COMPONENT, event: "completed", message: "PPT build completed", runId: args["run-id"], iteration: args.iteration, data: { exit_code: 0, element_count: summary.built_element_count } });
    console.log(JSON.stringify(success(COMPONENT, outputs, args["run-id"], args.iteration)));
    return 0;
  } catch (error) {
    const runId = args?.["run-id"] ?? "unknown";
    const iteration = args?.iteration ?? null;
    if (args?.__logAuthorized) await logEvent(args["log-file"], { level: "error", component: COMPONENT, event: "failed", message: error.message, runId, iteration, data: { exit_code: error instanceof BuildError ? error.exitCode : 70 } }).catch(() => {});
    const payload = failure(COMPONENT, error, runId, iteration);
    console.log(JSON.stringify(payload));
    return payload.error.exit_code;
  }
}

if (path.resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) process.exitCode = await main();
