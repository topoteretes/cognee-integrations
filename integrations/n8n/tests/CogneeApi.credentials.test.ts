import { CogneeApi } from '../credentials/CogneeApi.credentials';

describe('CogneeApi credential', () => {
	const credential = new CogneeApi();

	describe('identity', () => {
		it('exposes the credential name n8n resolves it by', () => {
			expect(credential.name).toBe('cogneeApi');
		});

		it('exposes display metadata', () => {
			expect(credential.displayName).toBe('Cognee API');
			expect(credential.icon).toBe('file:cognee.svg');
			expect(credential.documentationUrl).toMatch(/^https:\/\/docs\.cognee\.ai\//);
		});
	});

	describe('schema', () => {
		it('defines exactly the baseUrl and apiKey fields', () => {
			expect(credential.properties.map((p) => p.name)).toEqual(['baseUrl', 'apiKey']);
		});

		it('defines baseUrl as a plain string with a dashboard placeholder', () => {
			const baseUrl = credential.properties.find((p) => p.name === 'baseUrl')!;
			expect(baseUrl.type).toBe('string');
			expect(baseUrl.default).toBe('');
			expect(baseUrl.placeholder).toBe('https://tenant-xxx.aws.cognee.ai');
			expect(baseUrl.typeOptions?.password).toBeUndefined();
		});

		it('defines apiKey as a masked string secret', () => {
			const apiKey = credential.properties.find((p) => p.name === 'apiKey')!;
			expect(apiKey.type).toBe('string');
			expect(apiKey.default).toBe('');
			expect(apiKey.typeOptions?.password).toBe(true);
		});
	});

	describe('credential test request', () => {
		it('probes /health on the raw base URL (no /api suffix)', () => {
			expect(credential.test.request.baseURL).toBe('={{$credentials.baseUrl}}');
			expect(credential.test.request.url).toBe('/health');
		});

		it('authenticates the probe with the X-Api-Key header', () => {
			expect(credential.test.request.headers).toEqual({
				'X-Api-Key': '={{$credentials.apiKey}}',
			});
		});
	});

	it('has no authenticate block (auth is injected by the node requestDefaults)', () => {
		expect((credential as { authenticate?: unknown }).authenticate).toBeUndefined();
	});
});
