import { describe, expect, it } from 'vitest';
import type { IExecuteSingleFunctions, IHttpRequestOptions } from 'n8n-workflow';

import { buildAddDataBody } from '../nodes/Cognee/Cognee.node';

function fakeContext(params: Record<string, unknown>): IExecuteSingleFunctions {
  return {
    getNodeParameter: (name: string, fallback?: unknown) =>
      name in params ? params[name] : fallback,
    getNode: () => ({ name: 'Cognee', type: 'cognee', typeVersion: 1, position: [0, 0], parameters: {} }),
  } as unknown as IExecuteSingleFunctions;
}

async function parseBody(options: IHttpRequestOptions): Promise<FormData> {
  const contentType = String(options.headers?.['Content-Type']);
  expect(contentType).toMatch(/^multipart\/form-data; boundary=/);
  return new Response(new Uint8Array(options.body as Buffer), {
    headers: { 'Content-Type': contentType },
  }).formData();
}

describe('buildAddDataBody', () => {
  it('encodes text items and dataset name as multipart and keeps existing headers', async () => {
    const ctx = fakeContext({
      datasetName: 'support_docs',
      textData: ['FAQ: reset password', 'Guide: export CSV'],
    });
    const result = await buildAddDataBody.call(ctx, {
      url: '/v1/add',
      headers: { Accept: 'application/json', 'X-Api-Key': 'k' },
    });

    expect(result.headers?.Accept).toBe('application/json');
    expect(result.headers?.['X-Api-Key']).toBe('k');

    const form = await parseBody(result);
    expect(form.get('datasetName')).toBe('support_docs');
    expect(form.has('node_set')).toBe(false);
    expect(form.has('run_in_background')).toBe(false);
    const files = form.getAll('data') as File[];
    expect(files.map((f) => f.name)).toEqual(['text-1.txt', 'text-2.txt']);
    expect(await files[1].text()).toBe('Guide: export CSV');
  });

  it('accepts a single string for Text Data and forwards additional fields', async () => {
    const ctx = fakeContext({
      datasetName: 'docs',
      textData: 'just one',
      addAdditionalFields: { nodeSet: ['team-a'], runInBackground: true },
    });
    const form = await parseBody(await buildAddDataBody.call(ctx, { url: '/v1/add' }));
    expect((form.getAll('data') as File[]).length).toBe(1);
    expect(form.getAll('node_set')).toEqual(['team-a']);
    expect(form.get('run_in_background')).toBe('true');
  });

  it('rejects a request with no non-empty text', async () => {
    const ctx = fakeContext({ datasetName: 'docs', textData: ['', '  '] });
    await expect(buildAddDataBody.call(ctx, { url: '/v1/add' })).rejects.toThrow(
      /at least one non-empty Text Data item/,
    );
  });
});
