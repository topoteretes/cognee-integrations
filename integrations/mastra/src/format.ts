/**
 * Text formatting across the cognee boundary: `messageToText` turns a
 * Mastra message into plain text for `rememberEntry` (word-boundary
 * truncated at `config.write.maxChars`, default 8000); `formatContextBlock`
 * turns `RecallHit[]` into the system-message block the input processor
 * injects, or `null` on an empty result set. Pure functions only — no
 * network, filesystem, or clock — so both are unit-testable with plain fixtures.
 */

import type { RecallHit } from "./types.js";

// ---- Word-boundary truncation ----

/** Appended to a truncated string so callers/tests/LLMs can tell truncation happened. */
export const TRUNCATION_MARKER = "…";

/**
 * Truncate `text` to at most `maxChars`, cutting at the last whitespace at
 * or before that point rather than mid-word; falls back to a hard cut only
 * when no boundary exists in range. Truncated results get `TRUNCATION_MARKER`
 * appended; `maxChars <= 0` returns `""`; text already `<= maxChars` is returned unchanged.
 */
export function truncateAtWordBoundary(text: string, maxChars: number): string {
  if (maxChars <= 0) return "";
  if (text.length <= maxChars) return text;

  const slice = text.slice(0, maxChars);
  const boundary = Math.max(slice.lastIndexOf(" "), slice.lastIndexOf("\n"), slice.lastIndexOf("\t"));
  // boundary === 0 would produce an empty cut — treat that like -1 (no boundary) and hard-cut
  // instead.
  const cut = boundary > 0 ? slice.slice(0, boundary) : slice;

  return cut.trimEnd() + TRUNCATION_MARKER;
}

// ---- Message -> text ----

/**
 * Minimal shape this module needs from a Mastra message — not
 * `MastraDBMessage`, so this stays a pure, dependency-free formatter.
 * `content` accepts anything `extractMessageText` understands: a string, an AI-SDK parts array, or
 * a bare `{text}` object.
 */
export interface FormattableMessage {
  role?: string;
  content: unknown;
}

/**
 * Best-effort plain-text extraction from `content`, whichever shape it arrives in; non-text parts
 * (tool calls, images) are silently skipped rather than stringified.
 */
export function extractMessageText(content: unknown): string {
  if (typeof content === "string") return content;

  if (Array.isArray(content)) {
    return content
      .map((part) => extractPartText(part))
      .filter((s): s is string => Boolean(s))
      .join("\n");
  }

  return extractPartText(content) ?? "";
}

function extractPartText(part: unknown): string | undefined {
  if (typeof part === "string") return part;
  if (part && typeof part === "object") {
    const text = (part as Record<string, unknown>).text;
    if (typeof text === "string") return text;
  }
  return undefined;
}

/**
 * Render one message as cognee-ingestible text: `"<role>: <text>"` when a
 * role is present, content truncated on a word boundary to `maxChars`
 * (default 8000) BEFORE the role prefix is added — truncating the
 * already-prefixed string risks landing the boundary search inside the
 * prefix itself (e.g. `"user: "`), returning almost nothing of the actual
 * content. Returns `""` (never `null`) for a message with no extractable text — unlike
 * `formatContextBlock`, an empty message is still a valid unit to join into a turn's write.
 */
export function messageToText(message: FormattableMessage, maxChars = 8000): string {
  const text = extractMessageText(message.content).trim();
  if (!text) return "";
  const truncated = truncateAtWordBoundary(text, maxChars);
  return message.role ? `${message.role}: ${truncated}` : truncated;
}

// ---- Recall hits -> injected context block ----

export interface FormatContextOptions {
  /** First line of the block. Default: `"Relevant memory from cognee:"`. */
  heading?: string;
  /** Per-hit truncation, word-boundary (default 2000). */
  maxCharsPerHit?: number;
}

const DEFAULT_HEADING = "Relevant memory from cognee:";
const DEFAULT_MAX_CHARS_PER_HIT = 2000;

/**
 * Render recall hits as the system-message block `CogneeInputProcessor`
 * injects. Returns `null` (not an empty string or heading-only block) when
 * `hits` is empty or every hit's `text` is blank, so the processor can skip injection on a "no
 * results" recall.
 */
export function formatContextBlock(hits: readonly RecallHit[], options: FormatContextOptions = {}): string | null {
  const usable = hits.filter((hit) => typeof hit.text === "string" && hit.text.trim().length > 0);
  if (usable.length === 0) return null;

  const heading = options.heading ?? DEFAULT_HEADING;
  const maxCharsPerHit = options.maxCharsPerHit ?? DEFAULT_MAX_CHARS_PER_HIT;

  const lines = usable.map((hit, index) => {
    const text = truncateAtWordBoundary(hit.text.trim(), maxCharsPerHit);
    const source = hit.source ?? hit.datasetName ?? hit.datasetId ?? undefined;
    return source ? `${index + 1}. ${text} (source: ${source})` : `${index + 1}. ${text}`;
  });

  return [heading, ...lines].join("\n");
}
