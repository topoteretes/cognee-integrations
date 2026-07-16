import { runReview } from './helpers';


 // Exercises the reviewSkill postReceive transform (parseReviewScore and the
 // unwrap/parse helpers behind it) against mocked Cognee /v1/search responses.

describe('reviewSkill response transformation', () => {
	describe('clean JSON answer', () => {
		const reviewJson =
			'{"score":0.85,"missing_instruction":"Add X","result_summary":"Did Y",' +
			'"dimensions":[{"name":"accuracy","score":0.9}]}';

		it('parses the canonical search_result envelope', async () => {
			const result = await runReview([{ search_result: reviewJson }]);
			expect(result).toMatchObject({
				score: 0.85,
				score_parse_ok: true,
				missing_instruction: 'Add X',
				result_summary: 'Did Y',
				dimensions: [{ name: 'accuracy', score: 0.9 }],
				raw_answer: reviewJson,
			});
		});

		it.each([
			['bare string body', reviewJson],
			['result key', { result: reviewJson }],
			['answer key', { answer: reviewJson }],
			['text key', { text: reviewJson }],
			['doubly nested search_result array', [{ search_result: [reviewJson] }]],
		])('unwraps the %s envelope', async (_label, body) => {
			const result = await runReview(body);
			expect(result.score).toBe(0.85);
			expect(result.score_parse_ok).toBe(true);
			expect(result.raw_answer).toBe(reviewJson);
		});

		it('stringifies objects without a known answer key', async () => {
			const result = await runReview({ unexpected: 'shape' });
			expect(result.raw_answer).toBe('{"unexpected":"shape"}');
			expect(result.score_parse_ok).toBe(false);
		});
	});

	describe('JSON embedded in prose or a fenced code block', () => {
		it('extracts the first {...} block', async () => {
			const answer =
				'Here is my review:\n```json\n{"score": 0.7, "result_summary": "ok"}\n```\nDone.';
			const result = await runReview([{ search_result: answer }]);
			expect(result.score).toBe(0.7);
			expect(result.score_parse_ok).toBe(true);
			expect(result.result_summary).toBe('ok');
		});
	});

	describe('prose self-review fallback', () => {
		it('extracts the overall score, dimensions and summary from prose', async () => {
			const answer = [
				'Overall score: 0.94',
				'- accuracy: 0.95',
				'- completeness: 0.93',
				'Missing instruction: none',
				'Result summary: solid run',
			].join('\n');
			const result = await runReview([{ search_result: answer }]);
			expect(result).toMatchObject({
				score: 0.94,
				score_parse_ok: true,
				missing_instruction: 'none',
				result_summary: 'solid run',
				dimensions: [
					{ name: 'accuracy', score: 0.95 },
					{ name: 'completeness', score: 0.93 },
				],
				review: answer,
			});
		});
	});

	describe('unparseable answers default to a failing review', () => {
		it('treats plain prose without a score as score 0', async () => {
			const answer = 'I could not evaluate this.';
			const result = await runReview([{ search_result: answer }]);
			expect(result.score).toBe(0);
			expect(result.score_parse_ok).toBe(false);
			expect(result.result_summary).toContain('Could not parse');
			expect(result.raw_answer).toBe(answer);
		});

		it('treats an empty response body as score 0', async () => {
			const result = await runReview([]);
			expect(result.score).toBe(0);
			expect(result.score_parse_ok).toBe(false);
			expect(result.raw_answer).toBe('');
		});

		it('treats a non-numeric JSON score as score 0', async () => {
			const result = await runReview([{ search_result: '{"score":"high"}' }]);
			expect(result.score).toBe(0);
			expect(result.score_parse_ok).toBe(false);
		});
	});

	describe('score clamping', () => {
		it('clamps scores above 1 down to 1', async () => {
			const result = await runReview([{ search_result: '{"score": 1.4}' }]);
			expect(result.score).toBe(1);
			expect(result.score_parse_ok).toBe(true);
		});

		it('clamps scores below 0 up to 0', async () => {
			const result = await runReview([{ search_result: '{"score": -0.2}' }]);
			expect(result.score).toBe(0);
			expect(result.score_parse_ok).toBe(true);
		});
	});

	describe('alternate LLM field-name normalization', () => {
		it('maps average_score / most_impactful_missing_instruction / summary', async () => {
			const answer =
				'{"average_score":0.8,"most_impactful_missing_instruction":"do Z","summary":"ok"}';
			const result = await runReview([{ search_result: answer }]);
			expect(result).toMatchObject({
				score: 0.8,
				score_parse_ok: true,
				missing_instruction: 'do Z',
				result_summary: 'ok',
			});
		});

		it('converts a grades object into a dimensions array', async () => {
			const answer = '{"score":0.9,"grades":{"accuracy":0.95,"style":0.85}}';
			const result = await runReview([{ search_result: answer }]);
			expect(result.dimensions).toEqual([
				{ name: 'accuracy', score: 0.95 },
				{ name: 'style', score: 0.85 },
			]);
		});

		it('defaults dimensions to an empty array', async () => {
			const result = await runReview([{ search_result: '{"score":0.9}' }]);
			expect(result.dimensions).toEqual([]);
		});
	});
});
