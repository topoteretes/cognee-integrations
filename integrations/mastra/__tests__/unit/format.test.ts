import {
  TRUNCATION_MARKER,
  extractMessageText,
  formatContextBlock,
  messageToText,
  truncateAtWordBoundary,
} from "../../src/format.js";
import type { RecallHit } from "../../src/types.js";

describe("truncateAtWordBoundary", () => {
  it("returns short text unchanged, with no truncation marker", () => {
    expect(truncateAtWordBoundary("hello world", 100)).toBe("hello world");
  });

  it("returns the exact-length text unchanged", () => {
    const text = "12345";
    expect(truncateAtWordBoundary(text, 5)).toBe(text);
  });

  it("cuts at the last word boundary at or before maxChars", () => {
    const text = "the quick brown fox jumps over the lazy dog";
    // "the quick brown fox " is 20 chars; cut at 22 should land inside "fox jumps"
    const result = truncateAtWordBoundary(text, 22);
    expect(result.endsWith(TRUNCATION_MARKER)).toBe(true);
    const withoutMarker = result.slice(0, -TRUNCATION_MARKER.length);
    expect(withoutMarker.length).toBeLessThanOrEqual(22);
    // must not have cut mid-word: what's left, minus the marker, is a
    // prefix of the original text ending exactly at a space (or the string).
    expect(text.startsWith(withoutMarker)).toBe(true);
    expect(withoutMarker.endsWith(" ")).toBe(false); // trimmed
    const cutPoint = withoutMarker.length;
    expect(cutPoint === text.length || text[cutPoint] === " ").toBe(true);
  });

  it("falls back to a hard cut when no boundary exists in range", () => {
    const text = "supercalifragilisticexpialidocious";
    const result = truncateAtWordBoundary(text, 10);
    expect(result).toBe(text.slice(0, 10) + TRUNCATION_MARKER);
  });

  it("treats newlines and tabs as boundaries too", () => {
    const text = "line one\nline two is much longer than the cutoff";
    const result = truncateAtWordBoundary(text, 12);
    expect(result.startsWith("line one")).toBe(true);
    expect(result).not.toMatch(/\n.*\S/); // did not cut mid-second-line without a boundary
  });

  it("maxChars <= 0 returns empty string", () => {
    expect(truncateAtWordBoundary("anything", 0)).toBe("");
    expect(truncateAtWordBoundary("anything", -5)).toBe("");
  });

  it("empty input returns empty output", () => {
    expect(truncateAtWordBoundary("", 10)).toBe("");
  });
});

describe("extractMessageText", () => {
  it("passes through a plain string", () => {
    expect(extractMessageText("hello")).toBe("hello");
  });

  it("joins an AI-SDK-style parts array", () => {
    const content = [{ type: "text", text: "hello" }, { type: "text", text: "world" }];
    expect(extractMessageText(content)).toBe("hello\nworld");
  });

  it("skips non-text parts (tool calls, etc.) without throwing", () => {
    const content = [{ type: "text", text: "hi" }, { type: "tool-call", toolName: "x" }, "raw"];
    expect(extractMessageText(content)).toBe("hi\nraw");
  });

  it("reads a bare {text} object", () => {
    expect(extractMessageText({ text: "hello" })).toBe("hello");
  });

  it("returns '' for content with no extractable text", () => {
    expect(extractMessageText({ type: "tool-call" })).toBe("");
    expect(extractMessageText([{ type: "tool-call" }])).toBe("");
    expect(extractMessageText(null)).toBe("");
    expect(extractMessageText(undefined)).toBe("");
  });
});

describe("messageToText", () => {
  it("prefixes with the role when present", () => {
    expect(messageToText({ role: "user", content: "hi there" })).toBe("user: hi there");
  });

  it("omits the role prefix when absent", () => {
    expect(messageToText({ content: "hi there" })).toBe("hi there");
  });

  it("returns '' for a message with no extractable text", () => {
    expect(messageToText({ role: "assistant", content: [{ type: "tool-call" }] })).toBe("");
  });

  it("truncates the content (not the role prefix) on a word boundary at maxChars", () => {
    const longText = Array.from({ length: 50 }, (_, i) => `word${i}`).join(" ");
    const result = messageToText({ role: "user", content: longText }, 30);
    expect(result.startsWith("user: ")).toBe(true);
    const contentPart = result.slice("user: ".length);
    expect(contentPart.length).toBeLessThanOrEqual(30 + TRUNCATION_MARKER.length);
    expect(contentPart.endsWith(TRUNCATION_MARKER)).toBe(true);
    expect(longText.startsWith(contentPart.slice(0, -TRUNCATION_MARKER.length))).toBe(true);
  });

  it("defaults maxChars to 8000, applied to content only", () => {
    const longText = "x".repeat(9000);
    const result = messageToText({ role: "user", content: longText });
    // maxChars bounds the extracted content (hard-cut: no boundary in an
    // unbroken run of "x"); the role prefix is added on top, uncounted.
    expect(result).toBe(`user: ${"x".repeat(8000)}${TRUNCATION_MARKER}`);
  });
});

describe("formatContextBlock", () => {
  const hit = (overrides: Partial<RecallHit> = {}): RecallHit => ({ text: "some fact", ...overrides });

  it("returns null for an empty hit list", () => {
    expect(formatContextBlock([])).toBeNull();
  });

  it("returns null when every hit has blank/missing text", () => {
    expect(formatContextBlock([hit({ text: "" }), hit({ text: "   " })])).toBeNull();
  });

  it("renders a heading plus one numbered line per hit", () => {
    const block = formatContextBlock([hit({ text: "fact one" }), hit({ text: "fact two" })]);
    expect(block).not.toBeNull();
    const lines = block!.split("\n");
    expect(lines[0]).toBe("Relevant memory from cognee:");
    expect(lines[1]).toBe("1. fact one");
    expect(lines[2]).toBe("2. fact two");
  });

  it("appends a source annotation when available", () => {
    const block = formatContextBlock([hit({ text: "fact", source: "graph" })]);
    expect(block).toBe("Relevant memory from cognee:\n1. fact (source: graph)");
  });

  it("falls back to datasetName then datasetId for the source annotation", () => {
    const byName = formatContextBlock([hit({ text: "fact", datasetName: "myapp" })]);
    expect(byName).toContain("(source: myapp)");
    const byId = formatContextBlock([hit({ text: "fact", datasetId: "ds-1" })]);
    expect(byId).toContain("(source: ds-1)");
  });

  it("omits blank hits from the middle of the list without renumbering gaps oddly", () => {
    const block = formatContextBlock([hit({ text: "keep one" }), hit({ text: "" }), hit({ text: "keep two" })]);
    const lines = block!.split("\n");
    expect(lines).toEqual(["Relevant memory from cognee:", "1. keep one", "2. keep two"]);
  });

  it("honours a custom heading", () => {
    const block = formatContextBlock([hit()], { heading: "Custom:" });
    expect(block!.startsWith("Custom:\n")).toBe(true);
  });

  it("truncates a long hit's text on a word boundary at maxCharsPerHit", () => {
    const longText = Array.from({ length: 50 }, (_, i) => `word${i}`).join(" ");
    const block = formatContextBlock([hit({ text: longText })], { maxCharsPerHit: 20 });
    const line = block!.split("\n")[1];
    expect(line.includes(TRUNCATION_MARKER)).toBe(true);
  });
});
