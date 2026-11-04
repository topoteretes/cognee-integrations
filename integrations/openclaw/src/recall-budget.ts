import { pyLen } from "./recall-text.js";

// ---------------------------------------------------------------------------
// Total cap on one prompt's injected recall.
//
// trimRecallText bounds each dataset's item on its own (recallContextChars per
// item, as in the claude-code/codex hooks), so with several scopes, trailing
// facts and entities, an unparsed item or the guidance block, the injection as
// a whole could still run past it. capTotal is the final pass: parts are kept
// in priority order until the cap is reached; the part that crosses it is cut
// at the last line break that fits and marked, and every later part is
// dropped. Lengths count code points, like the trimmer.
// ---------------------------------------------------------------------------

export const TRUNCATION_MARK = "\n…[truncated]";

/**
 * `parts` in priority order -> the same parts, each kept whole, cut, or "" so
 * that their total length is at most `cap`. `cap` 0 (or less) disables it.
 * An `atomic` part (one carrying its own markup, e.g. a `<code_graph>` block)
 * is kept whole or dropped, never cut.
 */
export function capTotal(parts: readonly string[], cap: number, atomic: (index: number) => boolean = () => false): string[] {
  if (!(cap > 0)) return [...parts];
  let left = cap;
  return parts.map((part, index) => {
    if (!part) return part;
    const n = pyLen(part);
    if (n <= left) {
      left -= n;
      return part;
    }
    if (atomic(index)) return "";
    const room = left - pyLen(TRUNCATION_MARK);
    left = 0;
    if (room <= 0) return "";
    // Code points, not UTF-16 units: never split a surrogate pair.
    const head = Array.from(part).slice(0, room).join("");
    const cut = head.lastIndexOf("\n");
    const kept = (cut > 0 ? head.slice(0, cut) : head).trimEnd();
    return kept ? kept + TRUNCATION_MARK : "";
  });
}
