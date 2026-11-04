// ---------------------------------------------------------------------------
// Recall text trimming (SDK-904)
//
// On cognee >= 1.6.0 a completion search with `only_context: true` returns the
// whole prompt the server's own LLM would have received: the conversation
// history, the question rendered through the retriever's template, the
// retrieved context and the session guidance block. The host agent already
// holds the conversation and needs no answering instructions, so injecting
// that string whole re-sends what is already in its context window. This
// module keeps only the retrieved context and shrinks it deterministically,
// without an LLM and without touching what the server retrieved.
//
// Two anchors, both ours to know:
//   * the question marker "The question is: `<prompt>`", rendered from the
//     very prompt the hook sent, so it matches exactly once however many
//     backticks the prompt or the context carry; the context slot opens at
//     the first backtick after it, in every one of the server's templates;
//   * the end: the final character when the text ends with a backtick,
//     otherwise the last "\n\n## Active session guidance".
//
// Anything that does not fit is returned whole and reported as unparsed: a
// template change can only cost tokens, never memory.
//
// Port of integrations/claude-code/scripts/_recall_text.py (the copy shared by
// the hook plugins). integrations/conformance/recall_text_cases.json pins both
// to the same outputs. Lengths, caps and budgets count code points, as Python
// does, and whitespace means Python's `str.isspace()`, so the two agree on
// every input — including CRLF text and astral characters.
// ---------------------------------------------------------------------------

/** Every cognee question template starts with this line. */
export const QUESTION_PREFIX = "The question is: `";
/** Title of the server-rendered guidance block. */
export const GUIDANCE_TITLE = "## Active session guidance";

export const PASSAGES_HEADING = "## Relevant passages";
export const ENTITIES_HEADING = "## Relevant entities";
export const FACTS_HEADING = "## Related facts";
/**
 * The hybrid retriever joins passages with this, no blank lines around it; a
 * markdown rule inside an answer has them, which `splitPassages` uses.
 */
export const PASSAGE_SEPARATOR = "\n---\n";

export const ENV_PASSAGE_CHARS = "COGNEE_RECALL_PASSAGE_CHARS";
export const ENV_CONTEXT_CHARS = "COGNEE_RECALL_CONTEXT_CHARS";
export const DEFAULT_PASSAGE_CHARS = 2000;
export const DEFAULT_CONTEXT_CHARS = 12000;

// Python's `str.isspace()` set, used for `\s` and for strip(): JS `\s` and
// trim() differ (they include U+FEFF and miss U+001C–U+001F and U+0085).
const WS = "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const WS_SET = new RegExp(`[${WS}]`, "u");
// Python's `^` under re.M matches only after "\n"; JS's /m also after "\r",
// U+2028 and U+2029. Python's `\b` is Unicode-aware; JS's is ASCII.
const BOL = "(?:^|(?<=\\n))";
const WORD_END = "(?![\\p{L}\\p{N}_])";

const SECTION_RE = new RegExp(
  `${BOL}(## Relevant passages|## Relevant entities|## Related facts)[ \\t]*\\n`,
  "u",
);
/**
 * The header a bridged session chunk opens with. `Session ID:` is the
 * transcript document; `# Session learning … (session <id>)` the distilled note.
 */
const SESSION_ID_RE = new RegExp(`${BOL}Session ID:[${WS}]*([^${WS}]+)`, "u");
const SESSION_LEARNING_RE = new RegExp(`${BOL}# Session learning${WORD_END}[^\\n]*?\\(session ([^)${WS}]+)\\)`, "u");
const DOC_HEADER_RE = new RegExp(`^(?:Session ID:|# Session learning${WORD_END})`, "u");
/**
 * Entity bullets the graph builder emits for every node: a bare type statement
 * (`X is a concept.`) and node-set membership (`X belongs to set …`). `[^\n]`
 * stands in for Python's `.`, which (unlike JS's) also matches "\r".
 */
const TYPE_BULLET_RE = /^- [^\n]+? is an? [a-z]+(?:[ _-][a-z]+)?\.$/;
const SET_BULLET_RE = new RegExp(`^- [^\\n]+ belongs to set [^${WS}]+\\.?$`, "u");

export type OnlyContextLayers = { history: string; context: string; guidance: string; parsed: boolean };
export type ContextSections = {
  preamble: string;
  passages: string | null;
  entities: string | null;
  facts: string | null;
};
export type MinimizeStats = {
  passages_in: number;
  passages_dropped: number;
  passages_capped: number;
  entity_lines_removed: number;
  budget_trimmed: number;
};
/**
 * What `trimRecallText` reports, for logging. Keys match the Python hooks'
 * `recall_context_trimmed` / `recall_context_unparsed` events; the minimizer
 * counters are present only when the text parsed.
 */
export type TrimStats = {
  parsed: boolean;
  chars_in: number;
  history_chars: number;
  chars_out: number;
} & Partial<MinimizeStats>;
export type TrimResult = { context: string; guidance: string; stats: TrimStats };
/** Per-call overrides; `undefined` or `null` reads the environment knob. `0` disables. */
export type TrimOptions = { passageChars?: number | null; contextChars?: number | null };

// ── Python string semantics ───────────────────────────────────────────────────

function isSpace(ch: string): boolean {
  return WS_SET.test(ch);
}
function lstrip(s: string): string {
  let i = 0;
  while (i < s.length && isSpace(s[i])) i++;
  return s.slice(i);
}
function rstrip(s: string): string {
  let j = s.length;
  while (j > 0 && isSpace(s[j - 1])) j--;
  return s.slice(0, j);
}
function strip(s: string): string {
  return lstrip(rstrip(s));
}
function stripNewlines(s: string): string {
  let i = 0;
  let j = s.length;
  while (i < j && s[i] === "\n") i++;
  while (j > i && s[j - 1] === "\n") j--;
  return s.slice(i, j);
}
function isHigh(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff;
}
function isLow(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff;
}
/** Python `len()`: code points, a surrogate pair counting once. */
export function pyLen(s: string): number {
  let n = s.length;
  for (let i = 0; i + 1 < s.length; i++) {
    if (isHigh(s.charCodeAt(i)) && isLow(s.charCodeAt(i + 1))) {
      n--;
      i++;
    }
  }
  return n;
}
/** UTF-16 offset of code point `k` (clamped to the string's end). */
function unitOffset(s: string, k: number): number {
  let u = 0;
  for (let c = 0; c < k && u < s.length; c++) {
    u += isHigh(s.charCodeAt(u)) && u + 1 < s.length && isLow(s.charCodeAt(u + 1)) ? 2 : 1;
  }
  return u;
}
/** Python `s.rfind(sub, 0, end)` over UTF-16 offsets: the match must end by `end`. */
function rfindBefore(s: string, sub: string, end: number): number {
  return end < sub.length ? -1 : s.lastIndexOf(sub, end - sub.length);
}

// ── knobs ─────────────────────────────────────────────────────────────────────

/** Non-negative int from the environment; `0` disables, junk means default. */
function intEnv(name: string, fallback: number): number {
  const raw = strip(process.env[name] ?? "");
  if (!raw) return fallback;
  // Python's int(): optional sign, digits, single underscores between digits.
  if (!/^[+-]?\d+(?:_\d+)*$/.test(raw)) return fallback;
  const value = Number(raw.replace(/_/g, ""));
  return value >= 0 ? value : fallback;
}

export function passageCharsLimit(): number {
  return intEnv(ENV_PASSAGE_CHARS, DEFAULT_PASSAGE_CHARS);
}

export function contextCharsLimit(): number {
  return intEnv(ENV_CONTEXT_CHARS, DEFAULT_CONTEXT_CHARS);
}

// ── step 2: cut the template away ─────────────────────────────────────────────

/**
 * Split a 1.6.x `only_context` item into its layers. When the text cannot be
 * read as the server's prompt (an older server's bare context, a rewritten
 * question, an unknown template) `parsed` is false and the whole text sits in
 * `context` — fail-open.
 */
export function splitOnlyContext(text: string | null | undefined, prompt: string): OnlyContextLayers {
  const whole = text ? String(text) : "";
  const unparsed = { history: "", context: whole, guidance: "", parsed: false };
  const marker = `${QUESTION_PREFIX}${prompt}\``;
  const questionAt = whole.indexOf(marker);
  if (questionAt < 0) return unparsed;
  let start = whole.indexOf("`", questionAt + marker.length);
  if (start < 0) return unparsed;
  start += 1;
  let end: number;
  let guidance: string;
  if (whole.endsWith("`")) {
    end = whole.length - 1;
    guidance = "";
  } else {
    end = whole.lastIndexOf("`\n\n" + GUIDANCE_TITLE);
    if (end < start) return unparsed;
    guidance = strip(whole.slice(end + 1));
  }
  return {
    history: rstrip(whole.slice(0, questionAt)),
    // Python's text[start:end] is "" when end < start; JS slice agrees.
    context: whole.slice(start, end),
    guidance,
    parsed: true,
  };
}

// ── step 3: shrink the context ────────────────────────────────────────────────

/**
 * The context's sections; a key is null when absent. `preamble` is whatever
 * precedes the first heading (the hybrid retriever's optional global-context
 * section, or an unsectioned context from another retriever), as-is. A
 * repeated heading keeps its last block, as in Python.
 */
export function splitSections(context: string | null | undefined): ContextSections {
  const parts = (context || "").split(SECTION_RE);
  const sections: ContextSections = { preamble: parts[0], passages: null, entities: null, facts: null };
  const names: Record<string, "passages" | "entities" | "facts"> = {
    [PASSAGES_HEADING]: "passages",
    [ENTITIES_HEADING]: "entities",
    [FACTS_HEADING]: "facts",
  };
  for (let i = 1; i < parts.length - 1; i += 2) sections[names[parts[i]]] = parts[i + 1];
  return sections;
}

/**
 * Passages of a `## Relevant passages` block. The server joins passages with
 * exactly "\n---\n"; a markdown rule inside an answer is the same three
 * characters between blank lines, so after the split that piece starts with a
 * newline and is glued back onto the one before it — unless it opens with a
 * document header.
 */
export function splitPassages(block: string | null | undefined): string[] {
  const pieces = stripNewlines(block || "").split(PASSAGE_SEPARATOR);
  const passages: string[] = [];
  for (const piece of pieces) {
    const continuation = piece.startsWith("\n") && !DOC_HEADER_RE.test(lstrip(piece));
    if (passages.length && continuation) {
      passages[passages.length - 1] = passages[passages.length - 1] + PASSAGE_SEPARATOR + piece;
    } else {
      passages.push(piece);
    }
  }
  return passages.filter((p) => strip(p) !== "");
}

/** The session a bridged chunk came from, or "" for any other document. */
export function passageSessionId(passage: string): string {
  const stripped = lstrip(passage);
  const head = stripped.slice(0, unitOffset(stripped, 400));
  const match = SESSION_ID_RE.exec(head) ?? SESSION_LEARNING_RE.exec(head);
  return match ? match[1] : "";
}

/**
 * `passage` cut to about `limit` chars at a paragraph break, marked with how
 * much was cut. Returns the very same string when nothing is cut.
 */
export function capPassage(passage: string, limit: number): string {
  const total = pyLen(passage);
  if (limit <= 0 || total <= limit) return passage;
  const half = Math.floor(limit / 2);
  const limitAt = unitOffset(passage, limit);
  // Back from a UTF-16 offset to a code-point index (a "\n" never splits a pair).
  const toCp = (u: number): number => (u < 0 ? u : pyLen(passage.slice(0, u)));
  let cut = toCp(rfindBefore(passage, "\n\n", limitAt));
  if (cut < half) cut = toCp(rfindBefore(passage, "\n", limitAt));
  if (cut < half) cut = limit;
  const head = rstrip(passage.slice(0, unitOffset(passage, cut)));
  return `${head}\n…[+${total - pyLen(head)} chars]`;
}

/**
 * Drop type and node-set bullets, then headings left without bullets.
 * `text` is "" when nothing with content survives.
 */
export function pruneEntities(block: string | null | undefined): { text: string; removed: number } {
  const lines = stripNewlines(block || "").split("\n");
  const kept: string[] = [];
  let removed = 0;
  for (const line of lines) {
    if (TYPE_BULLET_RE.test(line) || SET_BULLET_RE.test(line)) {
      removed++;
      continue;
    }
    kept.push(line);
  }
  const pruned: string[] = [];
  kept.forEach((line, i) => {
    if (line.startsWith("### ")) {
      const next = i + 1 < kept.length ? kept[i + 1] : "";
      if (!next.startsWith("- ")) {
        removed++;
        return;
      }
    }
    pruned.push(line);
  });
  if (!pruned.some((line) => line.startsWith("- "))) return { text: "", removed };
  return { text: stripNewlines(pruned.join("\n")), removed };
}

/**
 * Shrink a parsed context. In order: drop the passages that are this session's
 * own bridged turns, cap each remaining passage, prune the entity bullets that
 * carry nothing, keep the facts, and as a last resort drop passages from the
 * end until the whole thing fits `contextChars`. Entities and facts are never
 * trimmed by the budget. An unsectioned context passes through (stripped).
 */
export function minimizeContext(
  context: string | null | undefined,
  sessionId = "",
  options: TrimOptions = {},
): { text: string; stats: MinimizeStats } {
  const passageChars = options.passageChars ?? passageCharsLimit();
  const contextChars = options.contextChars ?? contextCharsLimit();
  const stats: MinimizeStats = {
    passages_in: 0,
    passages_dropped: 0,
    passages_capped: 0,
    entity_lines_removed: 0,
    budget_trimmed: 0,
  };
  const sections = splitSections(context);
  if (sections.passages === null && sections.entities === null && sections.facts === null) {
    return { text: strip(context || ""), stats };
  }

  const out: string[] = [];
  const preamble = strip(sections.preamble || "");
  if (preamble) out.push(preamble);

  const passages: string[] = [];
  if (sections.passages !== null) {
    const raw = splitPassages(sections.passages);
    stats.passages_in = raw.length;
    for (const passage of raw) {
      if (sessionId && passageSessionId(passage) === sessionId) {
        stats.passages_dropped++;
        continue;
      }
      const capped = capPassage(passage, passageChars);
      if (capped !== passage) stats.passages_capped++;
      passages.push(capped);
    }
  }

  let entities = "";
  if (sections.entities !== null) {
    const pruned = pruneEntities(sections.entities);
    entities = pruned.text;
    stats.entity_lines_removed = pruned.removed;
  }

  const facts = sections.facts !== null ? stripNewlines(sections.facts) : "";

  const render = (kept: string[]): string => {
    const blocks = [...out];
    if (kept.length) blocks.push(PASSAGES_HEADING + "\n" + kept.join(PASSAGE_SEPARATOR));
    if (entities) blocks.push(ENTITIES_HEADING + "\n" + entities);
    if (strip(facts)) blocks.push(FACTS_HEADING + "\n" + facts);
    return strip(blocks.join("\n\n"));
  };

  let text = render(passages);
  while (contextChars > 0 && pyLen(text) > contextChars && passages.length) {
    passages.pop();
    stats.budget_trimmed++;
    text = render(passages);
  }
  return { text, stats };
}

/**
 * Steps 2 and 3 together. `context` is what to inject under the memory
 * heading; `guidance` the server's trailing guidance block when one came with
 * the item. Unparsed text is returned whole with `stats.parsed === false`.
 */
export function trimRecallText(
  text: string | null | undefined,
  prompt: string,
  sessionId = "",
  options: TrimOptions = {},
): TrimResult {
  const whole = text ? String(text) : "";
  const layers = splitOnlyContext(whole, prompt);
  const head = { parsed: layers.parsed, chars_in: pyLen(whole), history_chars: pyLen(layers.history) };
  if (!layers.parsed) {
    return { context: whole, guidance: "", stats: { ...head, chars_out: pyLen(whole) } };
  }
  const { text: context, stats } = minimizeContext(layers.context, sessionId, options);
  return { context, guidance: layers.guidance, stats: { ...head, ...stats, chars_out: pyLen(context) } };
}
