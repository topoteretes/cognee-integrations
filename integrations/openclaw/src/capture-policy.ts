// ---------------------------------------------------------------------------
// Automatic capture policy
//
// Port of the claude-code / codex / antigravity `_capture_policy.py`: which
// tool calls the after_tool_call hook may store, and what is scrubbed from a
// captured value before it leaves the machine. Explicit remember is
// independent of all of this. Shared behaviour is pinned by
// integrations/conformance/capture_policy_cases.json.
//
//   tools / denyTools — names, fnmatch globs, or `Tool(prefix:*)`. The allow
//     list defaults to "*"; the deny list runs after it. An entry that cannot
//     be parsed (`Bash(rg)`, `Bash()`) fails closed: nothing is captured and
//     `policy.error` says why, so a typo never disables capture in silence.
//   denyPaths — fnmatch globs added to the built-in secret-file list; any
//     path-like param (`path`, `file_path`, `filePath`, …) that matches drops
//     the whole call. A tool whose params carry a string `command` is also
//     dropped when any word of that command names such a path (see
//     commandReadsSecret: `cat ~/.aws/credentials`, `less "$HOME/.ssh/id_rsa"`).
//   redact — on by default. Private keys, connection strings, bearer/basic
//     tokens, credential assignments (incl. AWS / npm / .pypirc), vendor keys
//     (incl. npm_ / pypi- tokens), AWS access key ids, bcrypt hashes and
//     secret-named object keys, plus user regexes (redactPatterns).
//
// Differences from the Python module, all deliberate:
//   - Tool names match case-insensitively. OpenClaw names its tools `exec`,
//     `read`, `write`, while users copy `Bash(git:*)` / `Read` from the Claude
//     Code docs; a case-sensitive match would silently select nothing.
//   - `Tool(prefix:*)` applies to any tool whose params carry a string
//     `command` (OpenClaw's `exec`, `bash`, …), exactly as in Python — the
//     tool glob still has to match the tool name.
//   - Everything is parsed once, up front. Python only parses deny paths and
//     redact patterns when a value reaches them; here a malformed deny-path
//     list fails the whole policy closed, and a malformed redact pattern
//     throws on the first string redacted (Python raises there too).
//   - redactPatterns are JavaScript regexes. A leading Python-style inline
//     flag group (`(?i)`, `(?s)`, `(?m)`) is translated; other Python-only
//     syntax is reported through `policy.redactError`.
// ---------------------------------------------------------------------------

/** A capture tool pattern, list, or redact pattern is not understood. */
export class CapturePatternError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CapturePatternError";
  }
}

/** A list setting: already parsed (plugin config) or a raw env string. */
export type CaptureListInput = readonly string[] | string | undefined;

export interface CapturePolicyOptions {
  /** Allow list; empty or absent means "*". Raw strings split on `|`. */
  tools?: CaptureListInput;
  /** Deny list, applied after the allow list. Raw strings split on `|`. */
  denyTools?: CaptureListInput;
  /** Extra deny-path globs on top of the defaults. Raw strings split on `,`. */
  denyPaths?: CaptureListInput;
  /** Scrub secrets from captured values (default true). */
  redact?: boolean;
  /** Extra redaction regexes. Raw strings split on newlines (commas are regex syntax). */
  redactPatterns?: CaptureListInput;
  /** Setting names used in error messages; default to the COGNEE_CAPTURE_* env names. */
  labels?: Partial<Record<"tools" | "denyTools" | "denyPaths" | "redactPatterns", string>>;
}

export interface CapturePolicy {
  readonly redact: boolean;
  /**
   * Why tool capture is off, or "". Set when a tools / denyTools / denyPaths
   * entry cannot be parsed; allowTool() then refuses everything. Report it
   * somewhere the user sees it (the Python plugins repeat it on every prompt).
   */
  readonly error: string;
  /** Why redaction cannot run, or "". redactText() throws while it is set. */
  readonly redactError: string;
  /** Compiled redactPatterns, applied after the built-in rules. */
  readonly customPatterns: readonly RegExp[];
  /** Whether a tool call may be captured (false while `error` is set). */
  allowTool(toolName: string, params: unknown): boolean;
}

const FALSE_VALUES = new Set(["0", "false", "no", "off"]);

const DEFAULT_LABELS = {
  tools: "COGNEE_CAPTURE_TOOLS",
  denyTools: "COGNEE_CAPTURE_DENY_TOOLS",
  denyPaths: "COGNEE_CAPTURE_DENY_PATHS",
  redactPatterns: "COGNEE_CAPTURE_REDACT_PATTERNS",
};

// `filePath` lowercases into `filepath`, so OpenClaw's camelCase params are covered.
const PATH_KEYS = new Set(["file_path", "filepath", "path", "paths", "notebook_path", "filename"]);

export const DEFAULT_DENY_PATHS: readonly string[] = [
  ".env",
  ".env.*",
  "*.pem",
  "*.key",
  "id_rsa*",
  "id_ed25519*",
  ".npmrc",
  ".netrc",
  "*/.aws/credentials",
  "*/.ssh/*",
  "*.p12",
  "*.pfx",
  "secrets.*",
  "credentials.*",
];

// ---------------------------------------------------------------------------
// Python regex semantics
//
// The rules below are Python `re` patterns on str, where \w, \b, \s and \d are
// Unicode-aware. JavaScript's are ASCII-only (\s aside, which differs in a few
// code points), so the rules spell the Python classes out to redact the same
// spans on non-ASCII input.
// ---------------------------------------------------------------------------

/** Python str.isspace() code points, as a character-class body. */
const SPACE = "\\t\\n\\v\\f\\r \\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const S = `[${SPACE}]`;
/** Python \w: letters, numbers, underscore. */
const WORD = "\\p{L}\\p{N}_";
const W = `[${WORD}]`;
/** Python \b over that \w. */
const B = `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`;

const SPACE_RE = new RegExp(`^${S}$`, "u");
const LEADING_SPACE_RE = new RegExp(`^${S}+`, "u");
const TRAILING_SPACE_RE = new RegExp(`${S}+$`, "u");

function isSpace(ch: string): boolean {
  return SPACE_RE.test(ch);
}

/** Python str.strip() / lstrip() with no argument. */
function pyStrip(value: string): string {
  return value.replace(LEADING_SPACE_RE, "").replace(TRAILING_SPACE_RE, "");
}

function pyLstrip(value: string): string {
  return value.replace(LEADING_SPACE_RE, "");
}

// The last word of a secret-bearing name. `secret` may carry a `_key` /
// `_access_key` suffix (`SECRET_KEY`, `aws_secret_access_key`), and the AWS /
// npm names a plain suffix test misses are spelled out: `access_key_id`,
// camelCase `SessionToken` / `SecretAccessKey` / `_authToken`.
const SECRET_WORD =
  "secret(?:[_-]?(?:access[_-]?)?key)?|token|password|passwd|api[_-]?key" +
  "|access[_-]?key(?:[_-]?id)?|session[_-]?token|auth[_-]?token";

const SECRET_KEY = new RegExp(`^(?:authorization|x-api-key|_auth|(?:.*[_-])?(?:${SECRET_WORD}))\\n?$`, "iu");

// Order matters: each rule runs on the previous rule's output, as in Python.
const RULES: ReadonlyArray<readonly [string, RegExp]> = [
  [
    "private-key",
    // Python's `$` also matches before a final newline; (?=\n?$) keeps it.
    new RegExp("-----BEGIN [A-Z ]*PRIVATE KEY-----[\\s\\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|(?=\\n?$))", "gu"),
  ],
  [
    "connection",
    new RegExp(`${B}(?:postgres(?:ql)?|mysql|mongodb(?:\\+srv)?|redis|amqps?)://[^${SPACE}"'<>]+`, "giu"),
  ],
  ["authorization", new RegExp(`${B}(?:bearer|basic)${S}+[A-Za-z0-9._~+/=-]+`, "giu")],
  [
    "credential",
    new RegExp(
      `["']?${B}(?:(?:[${WORD}-]*[_-])?(?:${SECRET_WORD}|x-api-key)|_auth)` +
        `["']?${S}*[:=]${S}*(?:"[^"]*"|'[^']*'|[^${SPACE},;]+)`,
      "giu",
    ),
  ],
  [
    "vendor-key",
    new RegExp(
      `${B}(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|xox[baprs]-|whsec_)[A-Za-z0-9_-]{16,}` +
        `|${B}npm_[A-Za-z0-9]{36}${B}|${B}pypi-[A-Za-z0-9_-]{50,}`,
      "gu",
    ),
  ],
  [
    "aws-key-id",
    new RegExp(`${B}(?:A3T[A-Z0-9]|AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16}${B}`, "gu"),
  ],
  ["bcrypt", new RegExp("\\$2[aby]\\$\\p{Nd}{2}\\$[./A-Za-z0-9]{53}", "gu")],
];

// `Tool(prefix:*)` borrows Claude Code's permission spelling, so the pattern a
// user already writes to allow a command also selects whether it is remembered.
const COMMAND_PATTERN = new RegExp(`^([^()${SPACE}]+)\\(([^()]*):\\*\\)\\n?$`, "u");

// ---------------------------------------------------------------------------
// fnmatch
// ---------------------------------------------------------------------------

const REGEX_SYNTAX = "^$\\.*+?()[]{}|/";

function escapeClassChar(ch: string): string {
  return "\\]-[^".includes(ch) ? "\\" + ch : ch;
}

/**
 * Python's fnmatch.translate: `*` and `?` cross `/`, `[!…]` negates, an
 * unterminated `[` is literal, and reversed ranges are dropped.
 */
function fnmatchToRegex(pat: string): RegExp {
  const chars = Array.from(pat);
  const n = chars.length;
  let out = "";
  let i = 0;
  while (i < n) {
    const c = chars[i++];
    if (c === "*") {
      out += ".*";
      while (i < n && chars[i] === "*") i++;
    } else if (c === "?") {
      out += ".";
    } else if (c === "[") {
      let j = i;
      if (j < n && chars[j] === "!") j++;
      if (j < n && chars[j] === "]") j++;
      while (j < n && chars[j] !== "]") j++;
      if (j >= n) {
        out += "\\[";
        continue;
      }
      // Python escapes the body and joins range chunks with bare "-"; here each
      // chunk is a list of literal characters joined the same way.
      let chunks: string[][];
      if (!chars.slice(i, j).includes("-")) {
        chunks = [chars.slice(i, j)];
      } else {
        chunks = [];
        let start = i;
        let k = chars[i] === "!" ? i + 2 : i + 1;
        for (;;) {
          k = chars.indexOf("-", k);
          if (k < 0 || k >= j) break;
          chunks.push(chars.slice(start, k));
          start = k + 1;
          k += 3;
        }
        const last = chars.slice(start, j);
        if (last.length) chunks.push(last);
        else chunks[chunks.length - 1].push("-");
        for (let m = chunks.length - 1; m > 0; m--) {
          const prev = chunks[m - 1];
          if (prev[prev.length - 1] > chunks[m][0]) {
            chunks[m - 1] = [...prev.slice(0, -1), ...chunks[m].slice(1)];
            chunks.splice(m, 1);
          }
        }
      }
      i = j + 1;
      const flat = chunks.flat();
      if (chunks.length === 1 && flat.length === 0) {
        out += "(?!)"; // empty range: never match
      } else if (chunks.length === 1 && flat.length === 1 && flat[0] === "!") {
        out += "."; // negated empty range: any character
      } else {
        const negate = flat[0] === "!";
        if (negate) chunks[0] = chunks[0].slice(1);
        out += `[${negate ? "^" : ""}${chunks.map((chunk) => chunk.map(escapeClassChar).join("")).join("-")}]`;
      }
    } else {
      out += REGEX_SYNTAX.includes(c) ? "\\" + c : c;
    }
  }
  return new RegExp(`^(?:${out})$`, "su");
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

/**
 * Parse a list setting the way the Python plugins read COGNEE_CAPTURE_*: a
 * JSON array of strings, or `separator`-separated entries (trimmed, empties
 * dropped). Arrays pass through unchanged, like a JSON array would. Throws
 * CapturePatternError for a JSON value that is not an array of strings.
 */
export function parseCaptureList(raw: CaptureListInput, separator: string, label: string): string[] {
  if (raw === undefined) return [];
  if (typeof raw !== "string") {
    if (!raw.every((item) => typeof item === "string")) {
      throw new CapturePatternError(`${label} must be an array of strings`);
    }
    return [...raw];
  }
  const text = pyStrip(raw);
  if (!text) return [];
  if (text.startsWith("[")) {
    let values: unknown;
    try {
      values = JSON.parse(text);
    } catch (e) {
      throw new CapturePatternError(`${label} is not a valid JSON array: ${String(e)}`);
    }
    if (!Array.isArray(values) || !values.every((item) => typeof item === "string")) {
      throw new CapturePatternError(`${label} is not a valid JSON array: ${label} must be an array of strings`);
    }
    return values as string[];
  }
  return text.split(separator).map(pyStrip).filter(Boolean);
}

type ToolPattern = { tool: RegExp; prefix: string | null };

function parseToolPattern(pattern: string): ToolPattern {
  if (!pattern.includes("(") && !pattern.includes(")")) {
    return { tool: fnmatchToRegex(pattern.toLowerCase()), prefix: null };
  }
  const match = COMMAND_PATTERN.exec(pattern);
  const prefix = match ? pyStrip(match[2]) : "";
  if (!match || !prefix) {
    // Matching nothing is how `Bash(rg:*)` used to switch capture off for
    // every tool without a word of warning; a typo has to be loud.
    throw new CapturePatternError(
      `capture tool pattern ${JSON.stringify(pattern)} is not understood; ` +
        "use a tool name, an fnmatch glob, or Tool(command-prefix:*)",
    );
  }
  return { tool: fnmatchToRegex(match[1].toLowerCase()), prefix };
}

/** Parse every entry up front so a bad one fails whether or not it is reached. */
function parseToolPatterns(raw: CaptureListInput, label: string, fallback: string[]): ToolPattern[] {
  let entries = parseCaptureList(raw, "|", label);
  if (!entries.length) entries = fallback;
  try {
    return entries.map(parseToolPattern);
  } catch (e) {
    throw new CapturePatternError(`${label}: ${(e as Error).message}`);
  }
}

/** A user regex, with a leading Python inline-flag group moved into JS flags. */
function compileRedactPattern(pattern: string): RegExp {
  let source = pattern;
  let flags = "g";
  const inline = /^\(\?([ims]+)\)/.exec(source);
  if (inline) {
    source = source.slice(inline[0].length);
    flags += [...new Set(inline[1])].join("");
  }
  try {
    return new RegExp(source, flags + "u");
  } catch {
    // Unicode mode rejects identity escapes Python accepts (`\-`, `\_`).
    return new RegExp(source, flags);
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/** PurePosixPath(value).name: the last component, ignoring empty and "." parts. */
function baseName(path: string): string {
  const parts = path.split("/").filter((part) => part && part !== ".");
  return parts.length ? parts[parts.length - 1] : "";
}

// ---------------------------------------------------------------------------
// Shell words
// ---------------------------------------------------------------------------

/** shlex's whitespace in POSIX mode. */
const SHLEX_SPACE = " \t\r\n";

/**
 * Python's shlex.split(command) (posix, no comments): quotes are removed, a
 * backslash escapes the next character outside quotes and only `"` / `\\`
 * inside double quotes. Throws on an unclosed quote or a trailing backslash,
 * where shlex raises ValueError.
 */
function shlexSplit(command: string): string[] {
  const chars = Array.from(command);
  const n = chars.length;
  const words: string[] = [];
  let token = "";
  let inWord = false;
  let i = 0;
  while (i < n) {
    const c = chars[i++];
    if (SHLEX_SPACE.includes(c)) {
      if (inWord) words.push(token);
      token = "";
      inWord = false;
      continue;
    }
    inWord = true;
    if (c === "\\") {
      if (i >= n) throw new Error("No escaped character");
      token += chars[i++];
    } else if (c === "'") {
      const end = chars.indexOf("'", i);
      if (end < 0) throw new Error("No closing quotation");
      token += chars.slice(i, end).join("");
      i = end + 1;
    } else if (c === '"') {
      for (;;) {
        if (i >= n) throw new Error("No closing quotation");
        const d = chars[i++];
        if (d === '"') break;
        if (d === "\\") {
          if (i >= n) throw new Error("No closing quotation");
          const e = chars[i++];
          if (e !== '"' && e !== "\\") token += "\\";
          token += e;
        } else {
          token += d;
        }
      }
    } else {
      token += c;
    }
  }
  if (inWord) words.push(token);
  return words;
}

// Shell control characters that end a word for the secret-file check:
// `cat .env|grep`, `cat<.env`, `$(cat .env)`, `a;cat .env`.
const SHELL_SPLIT = /[;|&()<>`]/;
const HOME_VAR = /^\$(?:HOME|\{HOME\})(?=\/|$)/;
const PY_SPACE_RUN = new RegExp(`${S}+`, "u");

/** Split a shell command into words, POSIX-quoted; whitespace if it will not parse. */
function shellWords(command: string): string[] {
  let words: string[];
  try {
    words = shlexSplit(command);
  } catch {
    words = command.split(PY_SPACE_RUN).filter(Boolean); // Python str.split()
  }
  return words.flatMap((word) =>
    word
      .split(SHELL_SPLIT)
      .filter(Boolean)
      .map((piece) => piece.replace(HOME_VAR, "~")),
  );
}

// ---------------------------------------------------------------------------
// Policy
// ---------------------------------------------------------------------------

/**
 * Build a capture policy from already-parsed settings (plugin config arrays)
 * or raw env strings. Never throws: an unparseable tools / denyTools /
 * denyPaths entry sets `error` and fails tool capture closed; an unparseable
 * redact pattern sets `redactError` and makes redaction throw.
 */
export function buildCapturePolicy(opts: CapturePolicyOptions = {}): CapturePolicy {
  const labels = { ...DEFAULT_LABELS, ...opts.labels };
  const redact = opts.redact ?? true;

  let allowed: ToolPattern[] = [];
  let denied: ToolPattern[] = [];
  let denyPaths: RegExp[] = [];
  let error = "";
  try {
    allowed = parseToolPatterns(opts.tools, labels.tools, ["*"]);
    denied = parseToolPatterns(opts.denyTools, labels.denyTools, []);
    denyPaths = [...DEFAULT_DENY_PATHS, ...parseCaptureList(opts.denyPaths, ",", labels.denyPaths)].map(
      (pattern) => fnmatchToRegex(pattern.toLowerCase()),
    );
  } catch (e) {
    error = (e as Error).message;
  }

  let custom: RegExp[] = [];
  let redactError = "";
  try {
    const patterns = parseCaptureList(opts.redactPatterns, "\n", labels.redactPatterns);
    custom = patterns.map((pattern) => {
      try {
        return compileRedactPattern(pattern);
      } catch (e) {
        throw new CapturePatternError(
          `${labels.redactPatterns}: invalid pattern ${JSON.stringify(pattern)}: ${(e as Error).message}`,
        );
      }
    });
  } catch (e) {
    redactError = (e as Error).message;
  }

  const sensitivePath = (value: unknown): boolean => {
    if (Array.isArray(value)) return value.some(sensitivePath);
    if (typeof value !== "string") return false;
    const normalized = value.replace(/\\/g, "/").toLowerCase();
    const name = baseName(normalized);
    const rooted = "/" + normalized.replace(/^\/+/, "");
    return denyPaths.some((re) => re.test(name) || re.test(rooted));
  };

  const hasSensitivePath = (value: unknown): boolean => {
    if (isPlainObject(value)) {
      return Object.entries(value).some(
        ([key, item]) => (PATH_KEYS.has(key.toLowerCase()) && sensitivePath(item)) || hasSensitivePath(item),
      );
    }
    if (Array.isArray(value)) return value.some(hasSensitivePath);
    return false;
  };

  // Whether a shell command names a deny-listed path as one of its words. A
  // word-level check, not a shell interpreter: `cat ~/.aws/credentials`,
  // `less "$HOME/.ssh/id_rsa"` and `source .env.local` are caught, but a path
  // built at run time (`cat $AWS_DIR/credentials`, `cat ~/.aws/cred*`) is not, and neither
  // is a file a program reads without naming it.
  const commandReadsSecret = (params: unknown): boolean => {
    const command = isPlainObject(params) ? params.command : undefined;
    return typeof command === "string" && shellWords(command).some(sensitivePath);
  };

  const matches = ({ tool, prefix }: ToolPattern, name: string, params: unknown): boolean => {
    if (!tool.test(name)) return false;
    if (prefix === null) return true;
    const raw = isPlainObject(params) ? params.command : undefined;
    if (typeof raw !== "string") return false;
    // The prefix has to end on a word boundary: `Bash(git:*)` keeps `git status`
    // and drops `gitx weird`. Compound commands (`cd x && git status`) are
    // matched on their first word only; this is a prefix test, not a shell parser.
    const command = pyLstrip(raw);
    if (!command.startsWith(prefix)) return false;
    return command.length === prefix.length || isSpace(String.fromCodePoint(command.codePointAt(prefix.length)!));
  };

  return {
    redact,
    error,
    redactError,
    customPatterns: custom,
    allowTool(toolName: string, params: unknown): boolean {
      if (error) return false;
      const name = String(toolName ?? "").toLowerCase();
      if (!allowed.some((p) => matches(p, name, params))) return false;
      if (denied.some((p) => matches(p, name, params))) return false;
      return !(hasSensitivePath(params) || commandReadsSecret(params));
    },
  };
}

/** The capture settings from COGNEE_CAPTURE_* env vars, for env fallbacks. */
export function capturePolicyFromEnv(env: NodeJS.ProcessEnv = process.env): CapturePolicyOptions {
  return {
    tools: env.COGNEE_CAPTURE_TOOLS,
    denyTools: env.COGNEE_CAPTURE_DENY_TOOLS,
    denyPaths: env.COGNEE_CAPTURE_DENY_PATHS,
    redact: !FALSE_VALUES.has(pyStrip(env.COGNEE_CAPTURE_REDACT ?? "true").toLowerCase()),
    redactPatterns: env.COGNEE_CAPTURE_REDACT_PATTERNS,
  };
}

/**
 * Scrub secrets from a string. Redact before truncating, so a clipped private
 * key cannot escape matching. Throws CapturePatternError while the policy's
 * redact patterns are invalid (fail closed: do not capture the text).
 */
export function redactText(text: string, policy: CapturePolicy): string {
  if (!policy.redact) return text;
  if (policy.redactError) throw new CapturePatternError(policy.redactError);
  let value = text;
  for (const [kind, re] of RULES) value = value.replace(re, `[redacted:${kind}]`);
  for (const re of policy.customPatterns) {
    value = value.replace(re, "[redacted:custom]");
  }
  return value;
}

/**
 * Deep-redact a captured value (method_params, a whole entry): strings are
 * scrubbed, a secret-named key (`api_key`, `authorization`, `*_token`, …) has
 * its value masked whatever its type, and anything else is returned as is.
 */
export function redactValue<T>(value: T, policy: CapturePolicy): T {
  if (!policy.redact) return value;
  if (typeof value === "string") return redactText(value, policy) as T;
  if (Array.isArray(value)) return value.map((item) => redactValue(item, policy)) as T;
  if (isPlainObject(value)) {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = SECRET_KEY.test(key) ? "[redacted:credential]" : redactValue(item, policy);
    }
    return out as T;
  }
  return value;
}

/**
 * Self-reference guard shared with claude/codex: a shell command that mentions
 * cognee is likely the plugin or CLI talking to itself, and capturing it would
 * recurse. Kept separate from allowTool() because it is not user policy; the
 * caller applies it to any tool whose params carry a string `command`.
 */
export function isCogneeSelfReference(params: unknown): boolean {
  return isPlainObject(params) && typeof params.command === "string" && params.command.includes("cognee");
}
