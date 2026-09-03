/**
 * Pure helpers shared by the Cognee node. Kept free of n8n runtime imports
 * so they can be unit-tested in isolation.
 */

/**
 * Pull the agent's answer text out of the /v1/search response envelope.
 * AGENTIC_COMPLETION returns the answer wrapped in a list and/or a
 * { search_result: ... } object, so unwrap recursively (mirrors the SDK's
 * unwrap_answer in run_self_improve_skill.py).
 */
export function unwrapSearchAnswer(value: unknown): string {
  if (Array.isArray(value)) {
    return value.length ? unwrapSearchAnswer(value[0]) : '';
  }
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    for (const key of ['search_result', 'result', 'answer', 'text']) {
      if (key in obj) {
        return unwrapSearchAnswer(obj[key]);
      }
    }
    return JSON.stringify(obj);
  }
  return value == null ? '' : String(value);
}

/**
 * Normalise parsed JSON review fields to the canonical shape the workflow
 * expects: { score, missing_instruction, result_summary, dimensions }.
 * LLMs sometimes return equivalent fields under alternate names
 * (average_score, most_impactful_missing_instruction, summary, grades).
 */
export function normalizeReviewFields(obj: Record<string, unknown>): Record<string, unknown> {
  const score = obj.score ?? obj.average_score;
  const missing_instruction =
    obj.missing_instruction ?? obj.most_impactful_missing_instruction ?? '';
  const result_summary = obj.result_summary ?? obj.summary ?? '';
  let dimensions = obj.dimensions;
  if (!Array.isArray(dimensions) && obj.grades && typeof obj.grades === 'object') {
    dimensions = Object.entries(obj.grades as Record<string, number>).map(([name, s]) => ({
      name,
      score: s,
    }));
  }
  return { ...obj, score, missing_instruction, result_summary, dimensions: dimensions ?? [] };
}

/**
 * Tolerantly parse the strict-JSON review the prompt asks for. Falls back to
 * extracting the first {...} block if the model wrapped it in prose/fences,
 * and finally falls back to regex extraction of the self-review prose block
 * the model sometimes produces instead of JSON.
 */
export function parseReviewJson(text: string): Record<string, unknown> {
  // 1. Pure JSON response
  try {
    return normalizeReviewFields(JSON.parse(text) as Record<string, unknown>);
  } catch { /* fall through */ }

  // 2. JSON block embedded in prose or fenced code block
  const match = text.match(/\{[\s\S]*\}/);
  if (match) {
    try {
      return normalizeReviewFields(JSON.parse(match[0]) as Record<string, unknown>);
    } catch { /* fall through */ }
  }

  // 3. Prose self-review block: "Overall score: 0.94" + per-dimension bullet list
  const scoreMatch = text.match(/[Oo]verall\s+score[:\s]+([0-9]*\.?[0-9]+)/);
  if (scoreMatch) {
    const score = parseFloat(scoreMatch[1]);
    const dimensions: Array<{ name: string; score: number }> = [];
    const dimPattern = /-\s*([\w_]+):\s*([0-9]*\.?[0-9]+)/g;
    let m: RegExpExecArray | null;
    while ((m = dimPattern.exec(text)) !== null) {
      dimensions.push({ name: m[1], score: parseFloat(m[2]) });
    }
    const missingMatch = text.match(/[Mm]issing\s+instruction[:\s]+([^\n]+)/);
    const summaryMatch = text.match(/[Rr]esult\s+summary[:\s]+([^\n]+(?:\n[^\n]+)*?)(?=\n\n|\n[A-Z]|$)/);
    return {
      score,
      dimensions,
      missing_instruction: missingMatch ? missingMatch[1].trim() : '',
      result_summary: summaryMatch ? summaryMatch[1].trim() : '',
      review: text,
    };
  }

  return {};
}

