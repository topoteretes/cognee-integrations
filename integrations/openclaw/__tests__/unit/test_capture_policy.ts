import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  buildCapturePolicy,
  CapturePatternError,
  capturePolicyFromEnv,
  isCogneeSelfReference,
  parseCaptureList,
  redactText,
  redactValue,
} from "../../src/capture-policy";

type Env = Record<string, string>;
type ToolCase = { note: string; env: Env; tool: string; params: Record<string, unknown>; expected: boolean | "error" };
type RedactCase = { note: string; env: Env; input: unknown; expected?: unknown; error?: boolean };

// The shared case table lives at integrations/conformance/capture_policy_cases.json;
// its expected values come from running the Python `_capture_policy.py` that
// claude-code, codex and antigravity ship, and the shared Python suite checks
// those trees against the same file. Cases use Claude Code tool names (Bash,
// Read); OpenClaw matches tool names case-insensitively, so each case is also
// run with the lowercased name OpenClaw would report.
const casesPath = resolve(__dirname, "..", "..", "..", "conformance", "capture_policy_cases.json");
const table: { tool_cases: ToolCase[]; redact_cases: RedactCase[] } = JSON.parse(readFileSync(casesPath, "utf-8"));

const policyFor = (env: Env) => buildCapturePolicy(capturePolicyFromEnv(env));

describe("capture policy conformance: tool decisions", () => {
  it.each(table.tool_cases)("$note", ({ env, tool, params, expected }) => {
    const policy = policyFor(env);
    if (expected === "error") {
      expect(policy.error).not.toBe("");
      expect(policy.allowTool(tool, params)).toBe(false);
      return;
    }
    expect(policy.error).toBe("");
    expect(policy.allowTool(tool, params)).toBe(expected);
    expect(policy.allowTool(tool.toLowerCase(), params)).toBe(expected);
  });
});

describe("capture policy conformance: redaction", () => {
  it.each(table.redact_cases)("$note", ({ env, input, expected, error }) => {
    const policy = policyFor(env);
    if (error) {
      expect(() => redactValue(input, policy)).toThrow(CapturePatternError);
      return;
    }
    expect(redactValue(input, policy)).toEqual(expected);
  });
});

describe("capture policy on OpenClaw tool calls", () => {
  it("matches Claude-style patterns against OpenClaw's lowercase tool names", () => {
    const policy = buildCapturePolicy({ tools: ["Bash(git:*)", "Read"] });
    expect(policy.allowTool("bash", { command: "git status" })).toBe(true);
    expect(policy.allowTool("bash", { command: "rg foo" })).toBe(false);
    expect(policy.allowTool("read", { path: "src/a.ts" })).toBe(true);
    expect(policy.allowTool("write", { path: "src/a.ts" })).toBe(false);
  });

  it("applies a prefix rule to any tool carrying a string command", () => {
    const policy = buildCapturePolicy({ denyTools: ["*(rg:*)"] });
    expect(policy.allowTool("exec", { command: "rg needle" })).toBe(false);
    expect(policy.allowTool("exec", { command: "git log" })).toBe(true);
    // A prefix rule needs a command; a tool without one is not selected by it.
    expect(policy.allowTool("read", { path: "a.ts" })).toBe(true);
  });

  it("checks OpenClaw path params against the deny paths", () => {
    const policy = buildCapturePolicy({});
    expect(policy.allowTool("read", { path: "/repo/.env" })).toBe(false);
    expect(policy.allowTool("write", { filePath: "/home/u/.ssh/id_rsa" })).toBe(false);
    expect(policy.allowTool("edit", { file_path: "certs/server.pem" })).toBe(false);
    expect(policy.allowTool("read", { path: "/repo/src/index.ts" })).toBe(true);
  });

  it("drops an OpenClaw shell call that reads a secret file", () => {
    const policy = buildCapturePolicy({ denyPaths: ["*.private"] });
    expect(policy.allowTool("exec", { command: "cat ~/.aws/credentials" })).toBe(false);
    expect(policy.allowTool("bash", { command: 'less "$HOME/.ssh/id_ed25519"' })).toBe(false);
    expect(policy.allowTool("exec", { command: "grep KEY .env.local | head" })).toBe(false);
    expect(policy.allowTool("exec", { command: "cat notes.private" })).toBe(false);
    expect(policy.allowTool("exec", { command: "cat README.md && git status" })).toBe(true);
  });

  it("redacts AWS, npm and PyPI credentials in captured output", () => {
    const policy = buildCapturePolicy({});
    const out = redactText(
      "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\naws_secret_access_key = wJalrXUtnFEMI/K7MDENG\n" +
        "//registry.npmjs.org/:_authToken=npm_abcdefghijklmnopqrstuvwxyz0123456789\n" +
        "key ASIAIOSFODNN7EXAMPLE pypi-" +
        "A".repeat(60),
      policy,
    );
    expect(out).not.toMatch(/AKIA|ASIA|wJalr|npm_|pypi-/);
  });

  it("adds configured deny paths to the defaults", () => {
    const policy = buildCapturePolicy({ denyPaths: ["*.private"] });
    expect(policy.allowTool("read", { path: "a.private" })).toBe(false);
    expect(policy.allowTool("read", { path: ".env.local" })).toBe(false);
  });

  it("fails closed and reports an invalid pattern", () => {
    const policy = buildCapturePolicy({ tools: ["Read", "Bash(rg)"], labels: { tools: "captureTools" } });
    expect(policy.error).toMatch(/^captureTools: capture tool pattern "Bash\(rg\)" is not understood/);
    expect(policy.allowTool("read", { path: "a.ts" })).toBe(false);
    // Redaction is independent of the tool lists, as in Python.
    expect(redactText("password=x", policy)).toBe("[redacted:credential]");
  });

  it("treats an empty allow list as everything", () => {
    expect(buildCapturePolicy({ tools: [] }).allowTool("exec", { command: "ls" })).toBe(true);
  });

  it("deep-redacts nested method params", () => {
    const policy = buildCapturePolicy({});
    const params = {
      command: "curl -H 'Authorization: Bearer abc.def' https://x",
      env: { GITHUB_TOKEN: "ghp_012345678901234567890123456789", HOME: "/home/u" },
      args: ["--url", "postgres://u:p@db/app", { password: "hunter2" }],
      timeout: 30,
    };
    expect(redactValue(params, policy)).toEqual({
      command: "curl -H 'Authorization: [redacted:authorization]' https://x",
      env: { GITHUB_TOKEN: "[redacted:credential]", HOME: "/home/u" },
      args: ["--url", "[redacted:connection]", { password: "[redacted:credential]" }],
      timeout: 30,
    });
    expect(params.env.GITHUB_TOKEN).toMatch(/^ghp_/); // input is not mutated
  });

  it("leaves values alone when redaction is off", () => {
    const policy = buildCapturePolicy({ redact: false, redactPatterns: ["["] });
    expect(redactValue({ api_key: "v", t: "password=x" }, policy)).toEqual({ api_key: "v", t: "password=x" });
  });

  it("reports an invalid redact pattern and refuses to redact strings", () => {
    const policy = buildCapturePolicy({ redactPatterns: ["("] });
    expect(policy.redactError).toMatch(/COGNEE_CAPTURE_REDACT_PATTERNS: invalid pattern "\("/);
    expect(policy.error).toBe("");
    expect(() => redactText("anything", policy)).toThrow(CapturePatternError);
  });

  it("accepts identity escapes Python allows in custom patterns", () => {
    const policy = buildCapturePolicy({ redactPatterns: ["acct\\-\\d+"] });
    expect(redactText("id acct-42", policy)).toBe("id [redacted:custom]");
  });

  it("flags cognee self-reference commands", () => {
    expect(isCogneeSelfReference({ command: "openclaw cognee status" })).toBe(true);
    expect(isCogneeSelfReference({ command: "git status" })).toBe(false);
    expect(isCogneeSelfReference({ path: "cognee.md" })).toBe(false);
  });
});

describe("parseCaptureList", () => {
  it("splits raw strings and passes arrays through", () => {
    expect(parseCaptureList(" a | b ||", "|", "x")).toEqual(["a", "b"]);
    expect(parseCaptureList('["a|b", " c "]', "|", "x")).toEqual(["a|b", " c "]);
    expect(parseCaptureList(["Bash(git:*)"], "|", "x")).toEqual(["Bash(git:*)"]);
    expect(parseCaptureList(undefined, "|", "x")).toEqual([]);
  });

  it("rejects malformed JSON", () => {
    expect(() => parseCaptureList("[a", "|", "X")).toThrow(/X is not a valid JSON array/);
    expect(() => parseCaptureList("[1]", "|", "X")).toThrow(CapturePatternError);
  });
});
