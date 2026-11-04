/** The total cap on one prompt's injected recall (recall-budget.ts). */

import { TRUNCATION_MARK, capTotal } from "../../src/recall-budget";
import { pyLen } from "../../src/recall-text";

const total = (parts: string[]) => parts.reduce((n, p) => n + pyLen(p), 0);

describe("capTotal", () => {
  it("keeps everything that fits, in order", () => {
    expect(capTotal(["aa", "bbb"], 10)).toEqual(["aa", "bbb"]);
  });

  it("cuts the part that crosses the cap at a line break, marks it, and drops the rest", () => {
    // 8 for "guidance" leaves 22: "line one" (8) plus the 13-char mark fits, "line two" does not.
    const out = capTotal(["guidance", "line one\nline two\nline three", "later"], 30);
    expect(out[0]).toBe("guidance");
    expect(out[1]).toBe("line one" + TRUNCATION_MARK);
    expect(out[2]).toBe("");
    expect(total(out)).toBeLessThanOrEqual(30);
  });

  it("never exceeds the cap, whatever the shapes", () => {
    const parts = ["x".repeat(500), "y\n".repeat(400), "z".repeat(3000), "é🙂".repeat(900)];
    for (const cap of [1, 13, 100, 999, 2500, 6000]) expect(total(capTotal(parts, cap))).toBeLessThanOrEqual(cap);
  });

  it("counts and cuts astral characters as one, never splitting a pair", () => {
    const out = capTotal(["🙂".repeat(50)], 30);
    expect(out[0]).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
    expect(pyLen(out[0])).toBeLessThanOrEqual(30);
  });

  it("keeps an atomic part whole or drops it", () => {
    const code = "<code_graph>\nfact\n</code_graph>";
    expect(capTotal(["a".repeat(10), code], 20, (i) => i === 1)).toEqual(["a".repeat(10), ""]);
    expect(capTotal(["a".repeat(10), code], 100, (i) => i === 1)).toEqual(["a".repeat(10), code]);
  });

  it("is off at 0", () => {
    const parts = ["x".repeat(50000)];
    expect(capTotal(parts, 0)).toEqual(parts);
  });
});
