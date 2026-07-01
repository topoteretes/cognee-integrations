import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { sanitizeSessionKey } from "../src/scope";

type SanitizeCase = { input: string; expected: string; note: string };

// The shared case table lives at integrations/conformance/session_id_cases.json.
// The claude-code, codex and hermes-agent tests read the same file, so any
// implementation that drifts from the shared rule fails its test.
const casesPath = resolve(__dirname, "..", "..", "conformance", "session_id_cases.json");
const cases: SanitizeCase[] = JSON.parse(readFileSync(casesPath, "utf-8"));

describe("session-id sanitization conformance", () => {
  it.each(cases)("$note", ({ input, expected }) => {
    expect(sanitizeSessionKey(input)).toBe(expected);
  });
});
