#!/usr/bin/env node

/**
 * Deterministically render committed SVG evidence without browser or network I/O.
 *
 * Public sources:
 *   docs/assets/*.svg -> sibling *.png
 *
 * Demo sources:
 *   docs/evidence/demo/frames.json
 *   docs/evidence/demo/frame-*.svg
 *     -> docs/assets/unitsentinel-demo.gif
 *
 * Comparison sources:
 *   docs/assets/comparison-*.svg
 *   docs/assets/compare-*-terminal.svg
 *     -> sibling *.png
 *   docs/evidence/comparison-demo/frames.json
 *   docs/evidence/comparison-demo/frame-*.svg
 *     -> docs/assets/comparison-demo.gif
 *
 * Usage:
 *   node render.mjs                       Render every expected output.
 *   node render.mjs --check               Verify every committed output.
 *   node render.mjs --render-comparison   Render comparison outputs only.
 *   node render.mjs --check-comparison    Verify comparison outputs only.
 */

import { constants as FS_CONSTANTS } from "node:fs";
import {
  lstat,
  open,
  readdir,
  realpath,
  rename,
  unlink,
} from "node:fs/promises";
import { endianness } from "node:os";
import {
  basename,
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from "node:path";
import { TextDecoder } from "node:util";
import { fileURLToPath } from "node:url";

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = resolve(SCRIPT_DIRECTORY, "..", "..");
const ASSET_DIRECTORY = join(REPOSITORY_ROOT, "docs", "assets");
const DEMO_DIRECTORY = join(REPOSITORY_ROOT, "docs", "evidence", "demo");
const DEMO_MANIFEST = join(DEMO_DIRECTORY, "frames.json");
const DEMO_OUTPUT = join(ASSET_DIRECTORY, "unitsentinel-demo.gif");
const COMPARISON_DEMO_DIRECTORY = join(
  REPOSITORY_ROOT,
  "docs",
  "evidence",
  "comparison-demo",
);
const COMPARISON_DEMO_MANIFEST = join(
  COMPARISON_DEMO_DIRECTORY,
  "frames.json",
);
const COMPARISON_DEMO_OUTPUT = join(
  ASSET_DIRECTORY,
  "comparison-demo.gif",
);
const REPAIR_SOURCE = join(ASSET_DIRECTORY, "unit-repair-lineage.svg");
const REPAIR_OUTPUT = join(ASSET_DIRECTORY, "unit-repair-lineage.png");

const DEMO_SCHEMA = "unitsentinel.demo-frames/v1";
const COMPARISON_DEMO_SCHEMA = "unitsentinel.comparison-demo-frames/v1";
const FONT_ENVIRONMENT_VARIABLE = "UNITSENTINEL_FONT_PATH";
const PUBLIC_SVG_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*\.svg$/u;
const DEMO_FRAME_NAME = /^frame-[a-z0-9]+(?:-[a-z0-9]+)*\.svg$/u;
const COMPARISON_SOURCE_NAMES = Object.freeze([
  "comparison-workflow.svg",
  "comparison-lineage-drift.svg",
  "comparison-artifact-sizes.svg",
  "compare-compatible-terminal.svg",
  "compare-drift-terminal.svg",
  "compare-indeterminate-terminal.svg",
]);
const COMPARISON_FRAME_RECORDS = Object.freeze([
  Object.freeze({ delayMs: 3_000, path: "frame-compatible.svg" }),
  Object.freeze({ delayMs: 3_000, path: "frame-drift.svg" }),
  Object.freeze({ delayMs: 3_000, path: "frame-indeterminate.svg" }),
]);
const COMPARISON_FRAME_WIDTH = 1_440;
const COMPARISON_FRAME_HEIGHT = 1_100;

const MAX_PUBLIC_SVGS = 64;
const MAX_DEMO_FRAMES = 32;
const MAX_MANIFEST_BYTES = 65_536;
const MAX_SVG_BYTES = 2_097_152;
const MAX_FONT_BYTES = 20_971_520;
const MAX_DIMENSION = 4_096;
const MAX_IMAGE_PIXELS = 16_777_216;
const MAX_GIF_TOTAL_PIXELS = 20_000_000;
const MAX_PNG_BYTES = 67_108_864;
const MAX_GIF_BYTES = 134_217_728;
const MAX_TOTAL_OUTPUT_BYTES = 268_435_456;
const MAX_DELAY_MS = 60_000;
const IO_CHUNK_BYTES = 65_536;
const TEMP_ATTEMPTS = 16;

const ERROR_MESSAGES = Object.freeze({
  "arguments-not-supported": "command-line arguments are not supported",
  "asset-directory-invalid": "asset directory is unavailable or unsafe",
  "comparison-directory-invalid":
    "comparison demo directory is unavailable or unsafe",
  "demo-directory-invalid": "demo directory is unavailable or unsafe",
  "dependency-unavailable": "pinned renderer dependencies are unavailable",
  "font-invalid": "deterministic DejaVu font is unavailable or unsafe",
  "gif-encoding-failed": "demo GIF encoding failed",
  "gif-frame-dimensions": "demo frames have invalid dimensions",
  "gif-frame-limit": "demo frames exceed the bounded pixel budget",
  "internal-render-failure": "internal evidence rendering failure",
  "manifest-invalid": "demo frame manifest is invalid",
  "manifest-read-failed": "demo frame manifest could not be read safely",
  "no-public-svg": "no public SVG evidence sources were found",
  "output-durability-failed": "output durability could not be confirmed",
  "output-publish-failed": "rendered output could not be published atomically",
  "output-stale": "committed rendered evidence is missing or stale",
  "output-size-limit": "rendered outputs exceed the aggregate byte budget",
  "output-target-invalid": "rendered output target is unsafe",
  "output-write-failed": "rendered output could not be written completely",
  "platform-unsupported": "renderer platform requirements are not satisfied",
  "png-validation-failed": "rendered PNG validation failed",
  "public-svg-invalid": "public SVG evidence source is invalid",
  "public-svg-limit": "public SVG evidence exceeds the file-count limit",
  "repository-layout-invalid": "renderer repository layout is invalid",
  "svg-external-resource": "SVG source contains a forbidden external resource",
  "svg-invalid": "SVG evidence source is invalid",
  "svg-read-failed": "SVG source could not be read safely",
  "svg-render-failed": "SVG rendering failed",
  "svg-size-invalid": "SVG output dimensions exceed the renderer limits",
});

class RenderFailure extends Error {
  constructor(code) {
    super(ERROR_MESSAGES[code] ?? ERROR_MESSAGES["internal-render-failure"]);
    this.name = "RenderFailure";
    this.code = code;
  }
}

function fail(code) {
  throw new RenderFailure(code);
}

function isEintr(error) {
  return error !== null && typeof error === "object" && error.code === "EINTR";
}

function isMissing(error) {
  return error !== null && typeof error === "object" && error.code === "ENOENT";
}

function isInside(base, candidate, { allowEqual = false } = {}) {
  const difference = relative(base, candidate);
  if (difference === "") {
    return allowEqual;
  }
  return (
    !isAbsolute(difference) &&
    difference !== ".." &&
    !difference.startsWith(`..${sep}`)
  );
}

function requireInside(base, candidate, options) {
  if (!isInside(base, candidate, options)) {
    fail("repository-layout-invalid");
  }
}

async function retry(operation) {
  for (;;) {
    try {
      return await operation();
    } catch (error) {
      if (!isEintr(error)) {
        throw error;
      }
    }
  }
}

async function closeQuietly(handle) {
  if (handle === null) {
    return;
  }
  try {
    await handle.close();
  } catch {
    // Process teardown closes any descriptor whose explicit close was interrupted.
  }
}

function sameFileSnapshot(before, after) {
  return (
    before.dev === after.dev &&
    before.ino === after.ino &&
    before.mode === after.mode &&
    before.size === after.size &&
    before.mtimeNs === after.mtimeNs &&
    before.ctimeNs === after.ctimeNs
  );
}

async function readBoundedRegularFile(path, maxBytes, failureCode) {
  let handle = null;
  try {
    handle = await retry(() =>
      open(
        path,
        FS_CONSTANTS.O_RDONLY |
          FS_CONSTANTS.O_NOFOLLOW |
          FS_CONSTANTS.O_NONBLOCK,
      ),
    );
    const before = await retry(() => handle.stat({ bigint: true }));
    if (!before.isFile() || before.size > BigInt(maxBytes)) {
      fail(failureCode);
    }

    const chunks = [];
    let total = 0;
    while (total <= maxBytes) {
      const requested = Math.min(
        IO_CHUNK_BYTES,
        maxBytes + 1 - total,
      );
      const chunk = Buffer.allocUnsafe(requested);
      const { bytesRead } = await retry(() =>
        handle.read(chunk, 0, requested, null),
      );
      if (bytesRead === 0) {
        break;
      }
      chunks.push(Buffer.from(chunk.subarray(0, bytesRead)));
      total += bytesRead;
    }
    if (total > maxBytes) {
      fail(failureCode);
    }

    const after = await retry(() => handle.stat({ bigint: true }));
    if (
      !after.isFile() ||
      !sameFileSnapshot(before, after) ||
      after.size !== BigInt(total)
    ) {
      fail(failureCode);
    }
    return Buffer.concat(chunks, total);
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail(failureCode);
  } finally {
    await closeQuietly(handle);
  }
}

async function requireSafeDirectory(path, root, failureCode) {
  try {
    const metadata = await lstat(path);
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
      fail(failureCode);
    }
    const canonical = await realpath(path);
    if (!isInside(root, canonical, { allowEqual: false })) {
      fail(failureCode);
    }
    return canonical;
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail(failureCode);
  }
}

async function loadDependencies() {
  try {
    const [resvgModule, gifModule, pngModule] = await Promise.all([
      import("@resvg/resvg-js"),
      import("gifenc"),
      import("pngjs"),
    ]);
    const resvgPackage = resvgModule.default ?? resvgModule;
    const gifPackage = gifModule.default ?? gifModule;
    const pngPackage = pngModule.default ?? pngModule;
    const Resvg = resvgPackage.Resvg;
    const GIFEncoder = gifPackage.GIFEncoder;
    const applyPalette = gifPackage.applyPalette;
    const quantize = gifPackage.quantize;
    const PNG = pngPackage.PNG;
    if (
      typeof Resvg !== "function" ||
      typeof GIFEncoder !== "function" ||
      typeof applyPalette !== "function" ||
      typeof quantize !== "function" ||
      typeof PNG?.sync?.read !== "function"
    ) {
      fail("dependency-unavailable");
    }
    return { Resvg, GIFEncoder, applyPalette, quantize, PNG };
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail("dependency-unavailable");
  }
}

async function selectFont(repositoryRoot) {
  const explicit = process.env[FONT_ENVIRONMENT_VARIABLE];
  if (explicit !== undefined && explicit !== "") {
    if (!isAbsolute(explicit)) {
      fail("font-invalid");
    }
    return validateFont(explicit);
  }

  const candidates = [
    join(repositoryRoot, "tools", "evidence", "fonts", "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
  ];
  for (const candidate of candidates) {
    try {
      return await validateFont(candidate);
    } catch (error) {
      if (!(error instanceof RenderFailure) || error.code !== "font-invalid") {
        throw error;
      }
    }
  }
  fail("font-invalid");
}

async function validateFont(path) {
  try {
    const metadata = await lstat(path);
    if (
      !metadata.isFile() ||
      metadata.isSymbolicLink() ||
      metadata.size < 1_024 ||
      metadata.size > MAX_FONT_BYTES
    ) {
      fail("font-invalid");
    }
    return path;
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail("font-invalid");
  }
}

function decodeUtf8(payload, failureCode) {
  if (
    payload.length >= 3 &&
    payload[0] === 0xef &&
    payload[1] === 0xbb &&
    payload[2] === 0xbf
  ) {
    fail(failureCode);
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(payload);
  } catch {
    fail(failureCode);
  }
}

function isPlainRecord(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function hasExactKeys(value, expected) {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return (
    actual.length === wanted.length &&
    actual.every((key, index) => key === wanted[index])
  );
}

async function readFrameManifest({
  directory,
  expectedFrames = null,
  manifest,
  schema,
}) {
  const payload = await readBoundedRegularFile(
    manifest,
    MAX_MANIFEST_BYTES,
    "manifest-read-failed",
  );
  let parsed;
  try {
    parsed = JSON.parse(decodeUtf8(payload, "manifest-invalid"));
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail("manifest-invalid");
  }
  if (
    !isPlainRecord(parsed) ||
    !hasExactKeys(parsed, ["schema", "frames"]) ||
    parsed.schema !== schema ||
    !Array.isArray(parsed.frames) ||
    parsed.frames.length < 2 ||
    parsed.frames.length > MAX_DEMO_FRAMES ||
    (expectedFrames !== null &&
      parsed.frames.length !== expectedFrames.length)
  ) {
    fail("manifest-invalid");
  }

  const seen = new Set();
  return parsed.frames.map((frame, index) => {
    if (
      !isPlainRecord(frame) ||
      !hasExactKeys(frame, ["path", "delay_ms"]) ||
      typeof frame.path !== "string" ||
      !DEMO_FRAME_NAME.test(frame.path) ||
      basename(frame.path) !== frame.path ||
      (expectedFrames !== null &&
        (frame.path !== expectedFrames[index].path ||
          frame.delay_ms !== expectedFrames[index].delayMs)) ||
      !Number.isSafeInteger(frame.delay_ms) ||
      frame.delay_ms < 10 ||
      frame.delay_ms > MAX_DELAY_MS ||
      frame.delay_ms % 10 !== 0 ||
      seen.has(frame.path)
    ) {
      fail("manifest-invalid");
    }
    seen.add(frame.path);
    const path = resolve(directory, frame.path);
    if (!isInside(directory, path)) {
      fail("manifest-invalid");
    }
    return { path, delayMs: frame.delay_ms };
  });
}

async function readDemoManifest() {
  return readFrameManifest({
    directory: DEMO_DIRECTORY,
    manifest: DEMO_MANIFEST,
    schema: DEMO_SCHEMA,
  });
}

async function readComparisonDemoManifest() {
  return readFrameManifest({
    directory: COMPARISON_DEMO_DIRECTORY,
    expectedFrames: COMPARISON_FRAME_RECORDS,
    manifest: COMPARISON_DEMO_MANIFEST,
    schema: COMPARISON_DEMO_SCHEMA,
  });
}

async function listPublicSvgSources() {
  let entries;
  try {
    entries = await readdir(ASSET_DIRECTORY, { withFileTypes: true });
  } catch {
    fail("asset-directory-invalid");
  }
  const svgEntries = entries
    .filter((entry) => entry.name.endsWith(".svg"))
    .sort((left, right) =>
      left.name < right.name ? -1 : left.name > right.name ? 1 : 0,
    );
  if (svgEntries.length === 0) {
    fail("no-public-svg");
  }
  if (svgEntries.length > MAX_PUBLIC_SVGS) {
    fail("public-svg-limit");
  }
  return svgEntries.map((entry) => {
    if (
      !entry.isFile() ||
      entry.isSymbolicLink() ||
      !PUBLIC_SVG_NAME.test(entry.name)
    ) {
      fail("public-svg-invalid");
    }
    const source = resolve(ASSET_DIRECTORY, entry.name);
    if (!isInside(ASSET_DIRECTORY, source)) {
      fail("public-svg-invalid");
    }
    return source;
  });
}

function comparisonSvgSources() {
  return COMPARISON_SOURCE_NAMES.map((name) => {
    const source = resolve(ASSET_DIRECTORY, name);
    if (!isInside(ASSET_DIRECTORY, source)) {
      fail("repository-layout-invalid");
    }
    return source;
  });
}

function requireComparisonSources(publicSources) {
  const available = new Set(publicSources);
  for (const source of comparisonSvgSources()) {
    if (!available.has(source)) {
      fail("public-svg-invalid");
    }
  }
}

function validateSvgText(text) {
  const forbiddenMarkup = [
    /<!DOCTYPE/iu,
    /<!ENTITY/iu,
    /<\?xml-stylesheet/iu,
    /<\s*(?:[A-Za-z_][A-Za-z0-9_.-]*:)?(?:script|foreignObject|iframe|image|feImage)\b/iu,
    /@import\b/iu,
    /@font-face\b/iu,
    /\bxml:base\s*=/iu,
  ];
  if (
    text.includes("\u0000") ||
    forbiddenMarkup.some((pattern) => pattern.test(text))
  ) {
    fail("svg-external-resource");
  }

  const hrefPattern =
    /(?:^|\s)(?:href|xlink:href)\s*=\s*(["'])([\s\S]*?)\1/giu;
  for (const match of text.matchAll(hrefPattern)) {
    if (!/^#[A-Za-z_][A-Za-z0-9_.:-]*$/u.test(match[2])) {
      fail("svg-external-resource");
    }
  }

  const urlPattern = /url\s*\(\s*(["']?)([\s\S]*?)\1\s*\)/giu;
  for (const match of text.matchAll(urlPattern)) {
    if (!/^#[A-Za-z_][A-Za-z0-9_.:-]*$/u.test(match[2].trim())) {
      fail("svg-external-resource");
    }
  }
}

function validateDimensions(width, height) {
  if (
    !Number.isSafeInteger(width) ||
    !Number.isSafeInteger(height) ||
    width < 1 ||
    height < 1 ||
    width > MAX_DIMENSION ||
    height > MAX_DIMENSION ||
    width * height > MAX_IMAGE_PIXELS
  ) {
    fail("svg-size-invalid");
  }
}

function renderSvg(svgPayload, fontPath, dependencies) {
  validateSvgText(decodeUtf8(svgPayload, "svg-invalid"));
  const { Resvg, PNG } = dependencies;
  let renderer;
  try {
    renderer = new Resvg(svgPayload, {
      dpi: 96,
      fitTo: { mode: "original" },
      font: {
        defaultFontFamily: "DejaVu Sans",
        fontFiles: [fontPath],
        loadSystemFonts: false,
        sansSerifFamily: "DejaVu Sans",
        serifFamily: "DejaVu Sans",
      },
      imageRendering: 0,
      logLevel: "off",
      shapeRendering: 2,
      textRendering: 2,
    });
    validateDimensions(renderer.width, renderer.height);
    if (renderer.imagesToResolve().length !== 0) {
      fail("svg-external-resource");
    }
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail("svg-render-failed");
  }

  let pngBuffer;
  let decoded;
  try {
    const rendered = renderer.render();
    validateDimensions(rendered.width, rendered.height);
    if (
      rendered.width !== renderer.width ||
      rendered.height !== renderer.height
    ) {
      fail("png-validation-failed");
    }
    pngBuffer = Buffer.from(rendered.asPng());
    if (pngBuffer.length < 8 || pngBuffer.length > MAX_PNG_BYTES) {
      fail("png-validation-failed");
    }
    decoded = PNG.sync.read(pngBuffer, { checkCRC: true });
    validateDimensions(decoded.width, decoded.height);
    if (
      decoded.width !== rendered.width ||
      decoded.height !== rendered.height ||
      decoded.data.length !== decoded.width * decoded.height * 4
    ) {
      fail("png-validation-failed");
    }
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail("png-validation-failed");
  }
  return {
    png: pngBuffer,
    width: decoded.width,
    height: decoded.height,
    rgba: Uint8Array.from(decoded.data),
  };
}

function flattenOnWhite(rgba) {
  const flattened = new Uint8Array(rgba.length);
  for (let offset = 0; offset < rgba.length; offset += 4) {
    const alpha = rgba[offset + 3];
    const inverse = 255 - alpha;
    flattened[offset] = Math.floor(
      (rgba[offset] * alpha + 255 * inverse + 127) / 255,
    );
    flattened[offset + 1] = Math.floor(
      (rgba[offset + 1] * alpha + 255 * inverse + 127) / 255,
    );
    flattened[offset + 2] = Math.floor(
      (rgba[offset + 2] * alpha + 255 * inverse + 127) / 255,
    );
    flattened[offset + 3] = 255;
  }
  return flattened;
}

function encodeDemoGif(frames, dependencies) {
  if (endianness() !== "LE") {
    fail("platform-unsupported");
  }
  const { GIFEncoder, applyPalette, quantize } = dependencies;
  const width = frames[0].width;
  const height = frames[0].height;
  if (
    frames.some(
      (frame) => frame.width !== width || frame.height !== height,
    )
  ) {
    fail("gif-frame-dimensions");
  }
  const totalPixels = width * height * frames.length;
  if (totalPixels > MAX_GIF_TOTAL_PIXELS) {
    fail("gif-frame-limit");
  }

  try {
    const flattenedFrames = frames.map((frame) =>
      flattenOnWhite(frame.rgba),
    );
    const palettePixels = new Uint8Array(totalPixels * 4);
    let paletteOffset = 0;
    for (const frame of flattenedFrames) {
      palettePixels.set(frame, paletteOffset);
      paletteOffset += frame.length;
    }
    const palette = quantize(palettePixels, 256, { format: "rgb565" });
    if (!Array.isArray(palette) || palette.length < 1 || palette.length > 256) {
      fail("gif-encoding-failed");
    }

    const encoder = GIFEncoder();
    flattenedFrames.forEach((frame, index) => {
      const indices = applyPalette(frame, palette, "rgb565");
      encoder.writeFrame(indices, width, height, {
        colorDepth: 8,
        delay: frames[index].delayMs,
        dispose: 1,
        palette: index === 0 ? palette : undefined,
        repeat: 0,
      });
    });
    encoder.finish();
    const gif = Buffer.from(encoder.bytes());
    if (
      gif.length < 14 ||
      gif.length > MAX_GIF_BYTES ||
      gif.subarray(0, 6).toString("ascii") !== "GIF89a" ||
      gif[gif.length - 1] !== 0x3b
    ) {
      fail("gif-encoding-failed");
    }
    return gif;
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail("gif-encoding-failed");
  }
}

async function validateOutputTarget(
  path,
  repositoryRoot,
  { required = false } = {},
) {
  requireInside(repositoryRoot, path);
  if (dirname(path) !== ASSET_DIRECTORY) {
    fail("output-target-invalid");
  }
  try {
    const metadata = await lstat(path);
    if (!metadata.isFile() || metadata.isSymbolicLink()) {
      fail("output-target-invalid");
    }
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    if (isMissing(error) && required) {
      fail("output-stale");
    }
    if (!isMissing(error)) {
      fail("output-target-invalid");
    }
  }
}

async function writeAll(handle, payload) {
  let offset = 0;
  while (offset < payload.length) {
    const { bytesWritten } = await retry(() =>
      handle.write(payload, offset, payload.length - offset, offset),
    );
    if (bytesWritten < 1 || bytesWritten > payload.length - offset) {
      fail("output-write-failed");
    }
    offset += bytesWritten;
  }
}

async function syncDirectory(path) {
  let handle = null;
  try {
    handle = await retry(() =>
      open(
        path,
        FS_CONSTANTS.O_RDONLY |
          FS_CONSTANTS.O_DIRECTORY |
          FS_CONSTANTS.O_NOFOLLOW,
      ),
    );
    await retry(() => handle.sync());
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail("output-durability-failed");
  } finally {
    await closeQuietly(handle);
  }
}

async function atomicWrite(path, payload) {
  const parent = dirname(path);
  const targetName = basename(path);
  let temporary = null;
  let handle = null;
  let failureCode = "output-write-failed";
  try {
    for (let attempt = 0; attempt < TEMP_ATTEMPTS; attempt += 1) {
      const candidate = join(
        parent,
        `.${targetName}.${process.pid}.${attempt}.tmp`,
      );
      try {
        handle = await retry(() =>
          open(
            candidate,
            FS_CONSTANTS.O_WRONLY |
              FS_CONSTANTS.O_CREAT |
              FS_CONSTANTS.O_EXCL |
              FS_CONSTANTS.O_NOFOLLOW,
            0o600,
          ),
        );
        temporary = candidate;
        break;
      } catch (error) {
        if (!isMissing(error) && error?.code !== "EEXIST") {
          fail("output-write-failed");
        }
      }
    }
    if (handle === null || temporary === null) {
      fail("output-write-failed");
    }

    await writeAll(handle, payload);
    await retry(() => handle.chmod(0o644));
    await retry(() => handle.sync());
    await retry(() => handle.close());
    handle = null;

    failureCode = "output-publish-failed";
    await retry(() => rename(temporary, path));
    temporary = null;
    failureCode = "output-durability-failed";
    await syncDirectory(parent);
  } catch (error) {
    if (error instanceof RenderFailure) {
      throw error;
    }
    fail(failureCode);
  } finally {
    await closeQuietly(handle);
    if (temporary !== null) {
      try {
        await retry(() => unlink(temporary));
      } catch (error) {
        if (!isMissing(error) && !(error instanceof RenderFailure)) {
          // The primary failure remains more actionable; the private temp is 0600.
        }
      }
    }
  }
}

function appendBoundedOutput(outputs, output) {
  const retainedBytes = outputs.reduce(
    (total, current) => total + current.payload.length,
    0,
  );
  if (retainedBytes + output.payload.length > MAX_TOTAL_OUTPUT_BYTES) {
    fail("output-size-limit");
  }
  outputs.push(output);
}

async function renderPngOutputs(sources, dependencies, fontPath) {
  const outputs = [];
  for (const source of sources) {
    const payload = await readBoundedRegularFile(
      source,
      MAX_SVG_BYTES,
      "svg-read-failed",
    );
    const rendered = renderSvg(payload, fontPath, dependencies);
    const output = source.slice(0, -".svg".length) + ".png";
    appendBoundedOutput(outputs, { path: output, payload: rendered.png });
  }
  return outputs;
}

async function renderGifOutput(
  manifestFrames,
  output,
  dependencies,
  fontPath,
  expectedDimensions = null,
) {
  const frames = [];
  for (const frame of manifestFrames) {
    const payload = await readBoundedRegularFile(
      frame.path,
      MAX_SVG_BYTES,
      "svg-read-failed",
    );
    const rendered = renderSvg(payload, fontPath, dependencies);
    if (
      expectedDimensions !== null &&
      (rendered.width !== expectedDimensions.width ||
        rendered.height !== expectedDimensions.height)
    ) {
      fail("gif-frame-dimensions");
    }
    frames.push({
      delayMs: frame.delayMs,
      height: rendered.height,
      rgba: rendered.rgba,
      width: rendered.width,
    });
  }
  return {
    path: output,
    payload: encodeDemoGif(frames, dependencies),
  };
}

async function buildOutputs(context, dependencies, fontPath) {
  const publicSources = await listPublicSvgSources();
  requireComparisonSources(publicSources);
  const manifestFrames = await readDemoManifest();
  const comparisonManifestFrames = await readComparisonDemoManifest();
  const outputs = await renderPngOutputs(
    publicSources,
    dependencies,
    fontPath,
  );
  appendBoundedOutput(
    outputs,
    await renderGifOutput(
      manifestFrames,
      DEMO_OUTPUT,
      dependencies,
      fontPath,
    ),
  );
  appendBoundedOutput(
    outputs,
    await renderGifOutput(
      comparisonManifestFrames,
      COMPARISON_DEMO_OUTPUT,
      dependencies,
      fontPath,
      {
        height: COMPARISON_FRAME_HEIGHT,
        width: COMPARISON_FRAME_WIDTH,
      },
    ),
  );

  for (const output of outputs) {
    await validateOutputTarget(output.path, context.repositoryRoot);
  }
  return { gifCount: 2, outputs, publicCount: publicSources.length };
}

async function buildComparisonOutputs(context, dependencies, fontPath) {
  const publicSources = comparisonSvgSources();
  const manifestFrames = await readComparisonDemoManifest();
  const outputs = await renderPngOutputs(
    publicSources,
    dependencies,
    fontPath,
  );
  appendBoundedOutput(
    outputs,
    await renderGifOutput(
      manifestFrames,
      COMPARISON_DEMO_OUTPUT,
      dependencies,
      fontPath,
      {
        height: COMPARISON_FRAME_HEIGHT,
        width: COMPARISON_FRAME_WIDTH,
      },
    ),
  );
  for (const output of outputs) {
    await validateOutputTarget(output.path, context.repositoryRoot);
  }
  return {
    gifCount: 1,
    outputs,
    publicCount: publicSources.length,
  };
}

async function buildRepairOutput(context, dependencies, fontPath) {
  const payload = await readBoundedRegularFile(
    REPAIR_SOURCE,
    MAX_SVG_BYTES,
    "svg-read-failed",
  );
  const rendered = renderSvg(payload, fontPath, dependencies);
  await validateOutputTarget(REPAIR_OUTPUT, context.repositoryRoot);
  const outputs = [];
  appendBoundedOutput(outputs, {
    path: REPAIR_OUTPUT,
    payload: rendered.png,
  });
  return {
    gifCount: 0,
    outputs,
    publicCount: 1,
  };
}

async function verifyOutputs(outputs, repositoryRoot) {
  for (const output of outputs) {
    await validateOutputTarget(output.path, repositoryRoot, {
      required: true,
    });
    const maxBytes = output.path.endsWith(".gif")
      ? MAX_GIF_BYTES
      : MAX_PNG_BYTES;
    const committed = await readBoundedRegularFile(
      output.path,
      maxBytes,
      "output-stale",
    );
    if (!committed.equals(output.payload)) {
      fail("output-stale");
    }
  }
}

async function prepareContext(mode) {
  if (
    typeof FS_CONSTANTS.O_NOFOLLOW !== "number" ||
    typeof FS_CONSTANTS.O_DIRECTORY !== "number" ||
    typeof FS_CONSTANTS.O_NONBLOCK !== "number"
  ) {
    fail("platform-unsupported");
  }
  let repositoryRoot;
  try {
    repositoryRoot = await realpath(REPOSITORY_ROOT);
  } catch {
    fail("repository-layout-invalid");
  }
  if (repositoryRoot !== REPOSITORY_ROOT) {
    fail("repository-layout-invalid");
  }
  await requireSafeDirectory(
    ASSET_DIRECTORY,
    repositoryRoot,
    "asset-directory-invalid",
  );
  if (mode.endsWith("-all")) {
    await requireSafeDirectory(
      DEMO_DIRECTORY,
      repositoryRoot,
      "demo-directory-invalid",
    );
  }
  if (mode.endsWith("-all") || mode.endsWith("-comparison")) {
    await requireSafeDirectory(
      COMPARISON_DEMO_DIRECTORY,
      repositoryRoot,
      "comparison-directory-invalid",
    );
  }
  return { repositoryRoot };
}

function requestedMode() {
  if (process.argv.length === 2) {
    return "render-all";
  }
  if (process.argv.length === 3 && process.argv[2] === "--check") {
    return "check-all";
  }
  if (process.argv.length === 3 && process.argv[2] === "--repair") {
    return "render-repair";
  }
  if (process.argv.length === 3 && process.argv[2] === "--check-repair") {
    return "check-repair";
  }
  if (
    process.argv.length === 3 &&
    process.argv[2] === "--render-comparison"
  ) {
    return "render-comparison";
  }
  if (
    process.argv.length === 3 &&
    process.argv[2] === "--check-comparison"
  ) {
    return "check-comparison";
  }
  fail("arguments-not-supported");
}

function completionMessage(action, scope, publicCount, gifCount) {
  if (scope === "repair") {
    return `unitsentinel-evidence: ${action} 1 repair PNG\n`;
  }
  const qualifier = scope === "comparison" ? "comparison " : "";
  const gifLabel = gifCount === 1 ? "GIF" : "GIF files";
  return (
    `unitsentinel-evidence: ${action} ${publicCount} ${qualifier}` +
    `PNG files and ${gifCount} ${gifLabel}\n`
  );
}

async function main() {
  const mode = requestedMode();
  const context = await prepareContext(mode);
  const dependencies = await loadDependencies();
  const fontPath = await selectFont(context.repositoryRoot);
  const repairOnly = mode.endsWith("-repair");
  const comparisonOnly = mode.endsWith("-comparison");
  let build;
  let scope;
  if (repairOnly) {
    build = await buildRepairOutput(context, dependencies, fontPath);
    scope = "repair";
  } else if (comparisonOnly) {
    build = await buildComparisonOutputs(
      context,
      dependencies,
      fontPath,
    );
    scope = "comparison";
  } else {
    build = await buildOutputs(context, dependencies, fontPath);
    scope = "all";
  }
  const { gifCount, outputs, publicCount } = build;
  if (mode.startsWith("check-")) {
    await verifyOutputs(outputs, context.repositoryRoot);
    process.stdout.write(
      completionMessage("verified", scope, publicCount, gifCount),
    );
  } else {
    for (const output of outputs) {
      await atomicWrite(output.path, output.payload);
    }
    process.stdout.write(
      completionMessage("rendered", scope, publicCount, gifCount),
    );
  }
}

try {
  await main();
} catch (error) {
  const code =
    error instanceof RenderFailure
      ? error.code
      : "internal-render-failure";
  const message =
    ERROR_MESSAGES[code] ?? ERROR_MESSAGES["internal-render-failure"];
  process.stderr.write(`unitsentinel-evidence: error: ${message}\n`);
  process.exitCode = 1;
}
