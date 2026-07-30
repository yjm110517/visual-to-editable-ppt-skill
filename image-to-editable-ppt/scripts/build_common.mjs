import { createHash } from "node:crypto";
import { appendFile, mkdir, readFile, realpath, stat } from "node:fs/promises";
import path from "node:path";

export class BuildError extends Error {
  constructor(message, { exitCode = 4, category = "contract_error", target = "$" } = {}) {
    super(message);
    this.name = "BuildError";
    this.exitCode = exitCode;
    this.category = category;
    this.target = target;
  }
}

export function parseArgs(argv) {
  const values = {};
  const allowed = new Set(["iteration-dir", "layout", "asset-manifest", "asset-processing-report", "asset-dir", "svg-report", "output", "build-summary", "python", "run-id", "iteration", "log-file", "schema-dir"]);
  for (let index = 0; index < argv.length; index += 2) {
    const option = argv[index];
    const value = argv[index + 1];
    if (!option?.startsWith("--") || value === undefined || value.startsWith("--")) {
      throw new BuildError(`invalid CLI argument near ${option ?? "<end>"}`, { exitCode: 2, category: "cli_error", target: option ?? "$" });
    }
    const name = option.slice(2);
    if (!allowed.has(name) || Object.hasOwn(values, name)) {
      throw new BuildError(`unknown or duplicate option: ${option}`, { exitCode: 2, category: "cli_error", target: option });
    }
    values[name] = value;
  }
  const required = ["iteration-dir", "layout", "asset-manifest", "asset-processing-report", "asset-dir", "output", "build-summary", "run-id", "iteration"];
  const missing = required.filter((name) => !values[name]);
  if (missing.length) {
    throw new BuildError(`missing required options: ${missing.map((name) => `--${name}`).join(", ")}`, { exitCode: 2, category: "cli_error" });
  }
  values.iteration = Number(values.iteration);
  if (!Number.isInteger(values.iteration) || values.iteration < 1) {
    throw new BuildError("--iteration must be a positive integer", { exitCode: 2, category: "cli_error", target: "--iteration" });
  }
  return values;
}

export async function sha256File(filePath) {
  const digest = createHash("sha256");
  digest.update(await readFile(filePath));
  return digest.digest("hex");
}

export function sha256Bytes(content) {
  return createHash("sha256").update(content).digest("hex");
}

export async function readJson(filePath) {
  try {
    return JSON.parse(await readFile(filePath, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") {
      throw new BuildError("input file does not exist", { exitCode: 3, category: "missing_input", target: filePath });
    }
    throw new BuildError(`invalid JSON input: ${error.message}`, { category: "invalid_json", target: filePath });
  }
}

async function canonicalExistingPath(candidate) {
  try {
    return await realpath(candidate);
  } catch (error) {
    if (error.code === "ENOENT") {
      throw new BuildError("input path does not exist", { exitCode: 3, category: "missing_input", target: candidate });
    }
    throw error;
  }
}

export async function enforceIterationBoundary(iterationDir, inputs, outputs, appendOutputs = []) {
  const root = await canonicalExistingPath(iterationDir);
  const within = (candidate) => {
    const relative = path.relative(root, candidate);
    return relative !== "" && !relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative);
  };
  for (const candidate of inputs.filter(Boolean)) {
    const resolved = await canonicalExistingPath(candidate);
    if (!within(resolved)) {
      throw new BuildError("input escapes iteration directory", { category: "path_escape", target: candidate });
    }
  }
  for (const candidate of outputs.filter(Boolean)) {
    const resolved = path.resolve(candidate);
    const parent = await canonicalExistingPath(path.dirname(resolved));
    if (!within(resolved) || !within(parent) && parent !== root) {
      throw new BuildError("output escapes iteration directory", { category: "path_escape", target: candidate });
    }
    try {
      await stat(resolved);
      throw new BuildError("output already exists", { category: "output_collision", target: candidate });
    } catch (error) {
      if (error instanceof BuildError) throw error;
      if (error.code !== "ENOENT") throw error;
    }
  }
  for (const candidate of appendOutputs.filter(Boolean)) {
    const resolved = path.resolve(candidate);
    const parent = await canonicalExistingPath(path.dirname(resolved));
    if (!within(resolved) || (!within(parent) && parent !== root)) {
      throw new BuildError("append output escapes iteration directory", { category: "path_escape", target: candidate });
    }
  }
  return root;
}

export async function logEvent(logPath, { level, component, event, message, runId, iteration, data = {} }) {
  if (!logPath) return;
  await mkdir(path.dirname(logPath), { recursive: true });
  const record = { timestamp: new Date().toISOString(), level, component, event, message, run_id: runId, iteration, data };
  await appendFile(logPath, `${JSON.stringify(record)}\n`, "utf8");
}

export function success(component, outputs, runId, iteration) {
  return { status: "ok", component, run_id: runId, iteration, outputs, error: null };
}

export function failure(component, error, runId, iteration) {
  const expected = error instanceof BuildError;
  return {
    status: "error",
    component,
    run_id: runId,
    iteration,
    outputs: {},
    error: {
      exit_code: expected ? error.exitCode : 70,
      category: expected ? error.category : "internal_error",
      message: error.message,
      path: expected ? error.target : "$",
    },
  };
}
