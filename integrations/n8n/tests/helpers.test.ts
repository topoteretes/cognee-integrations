import { describe, expect, it } from 'vitest';

import {
  normalizeReviewFields,
  parseReviewJson,
  unwrapSearchAnswer,
} from '../nodes/Cognee/helpers';

describe('unwrapSearchAnswer', () => {
  it('returns plain strings unchanged', () => {
    expect(unwrapSearchAnswer('hello')).toBe('hello');
  });

  it('unwraps the first element of a list', () => {
    expect(unwrapSearchAnswer(['first', 'second'])).toBe('first');
  });

  it('returns an empty string for an empty list or null', () => {
    expect(unwrapSearchAnswer([])).toBe('');
    expect(unwrapSearchAnswer(null)).toBe('');
    expect(unwrapSearchAnswer(undefined)).toBe('');
  });

  it('unwraps nested search_result envelopes recursively', () => {
    expect(unwrapSearchAnswer([{ search_result: { answer: 'deep' } }])).toBe('deep');
  });

  it('serialises objects without a known answer key', () => {
    expect(unwrapSearchAnswer({ foo: 1 })).toBe('{"foo":1}');
  });
});

describe('normalizeReviewFields', () => {
  it('keeps canonical fields as they are', () => {
    const out = normalizeReviewFields({
      score: 0.8,
      missing_instruction: 'x',
      result_summary: 'y',
      dimensions: [{ name: 'a', score: 1 }],
    });
    expect(out.score).toBe(0.8);
    expect(out.missing_instruction).toBe('x');
    expect(out.result_summary).toBe('y');
    expect(out.dimensions).toEqual([{ name: 'a', score: 1 }]);
  });

  it('maps alternate field names onto the canonical ones', () => {
    const out = normalizeReviewFields({
      average_score: 0.5,
      most_impactful_missing_instruction: 'm',
      summary: 's',
      grades: { clarity: 0.4, depth: 0.6 },
    });
    expect(out.score).toBe(0.5);
    expect(out.missing_instruction).toBe('m');
    expect(out.result_summary).toBe('s');
    expect(out.dimensions).toEqual([
      { name: 'clarity', score: 0.4 },
      { name: 'depth', score: 0.6 },
    ]);
  });

  it('defaults dimensions to an empty list', () => {
    expect(normalizeReviewFields({ score: 1 }).dimensions).toEqual([]);
  });
});

describe('parseReviewJson', () => {
  it('parses a pure JSON review', () => {
    const out = parseReviewJson('{"score": 0.9, "result_summary": "ok"}');
    expect(out.score).toBe(0.9);
    expect(out.result_summary).toBe('ok');
  });

  it('extracts a JSON block wrapped in prose or code fences', () => {
    const text = 'Here is my review:\n```json\n{"score": 0.7}\n```\nThanks.';
    expect(parseReviewJson(text).score).toBe(0.7);
  });

  it('falls back to parsing the prose self-review block', () => {
    const text = [
      'Overall score: 0.94',
      '- clarity: 0.9',
      '- completeness: 0.98',
      'Missing instruction: add a rollback step',
      'Result summary: solid run overall',
    ].join('\n');
    const out = parseReviewJson(text);
    expect(out.score).toBe(0.94);
    expect(out.dimensions).toEqual([
      { name: 'clarity', score: 0.9 },
      { name: 'completeness', score: 0.98 },
    ]);
    expect(out.missing_instruction).toBe('add a rollback step');
    expect(out.result_summary).toBe('solid run overall');
    expect(out.review).toBe(text);
  });

  it('returns an empty object when nothing can be parsed', () => {
    expect(parseReviewJson('no score here')).toEqual({});
  });
});
