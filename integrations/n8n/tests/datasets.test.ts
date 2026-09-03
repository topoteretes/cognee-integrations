import { describe, expect, it } from 'vitest';
import type { IExecuteSingleFunctions, IHttpRequestOptions, INodeExecutionData } from 'n8n-workflow';

import { datasetsToOptions, flattenStatusMap } from '../nodes/Cognee/helpers';
import { Cognee, buildUpdateBody, flattenStatusOutput } from '../nodes/Cognee/Cognee.node';

describe('datasetsToOptions', () => {
  it('maps datasets to sorted name/value options with the id as description', () => {
    const options = datasetsToOptions([
      { id: 'b-id', name: 'zeta', created_at: '2026-01-01', owner_id: 'u' },
      { id: 'a-id', name: 'alpha', created_at: '2026-01-01', owner_id: 'u' },
    ]);
    expect(options).toEqual([
      { name: 'alpha', value: 'a-id', description: 'a-id' },
      { name: 'zeta', value: 'b-id', description: 'b-id' },
    ]);
  });

  it('tolerates unexpected shapes', () => {
    expect(datasetsToOptions(null)).toEqual([]);
    expect(datasetsToOptions({ error: 'x' })).toEqual([]);
    expect(datasetsToOptions([{ name: 'no id' }, { id: 'x' }])).toEqual([{ name: 'x', value: 'x', description: 'x' }]);
  });
});

describe('flattenStatusMap', () => {
  it('flattens the single-pipeline shape', () => {
    expect(flattenStatusMap({ 'd-1': 'completed', 'd-2': 'running' })).toEqual([
      { dataset_id: 'd-1', status: 'completed' },
      { dataset_id: 'd-2', status: 'running' },
    ]);
  });

  it('flattens the progress shape', () => {
    expect(flattenStatusMap({ 'd-1': { status: 'running', progress: { completed: 1, total: 3 } } })).toEqual([
      { dataset_id: 'd-1', status: 'running', progress: { completed: 1, total: 3 } },
    ]);
  });

  it('flattens the multi-pipeline shapes into one row per pipeline', () => {
    expect(
      flattenStatusMap({
        'd-1': { cognify_pipeline: 'completed', add_pipeline: { status: 'running', progress: null } },
      }),
    ).toEqual([
      { dataset_id: 'd-1', pipeline: 'cognify_pipeline', status: 'completed' },
      { dataset_id: 'd-1', pipeline: 'add_pipeline', status: 'running', progress: null },
    ]);
  });

  it('returns nothing for non-object bodies', () => {
    expect(flattenStatusMap(null)).toEqual([]);
    expect(flattenStatusMap([1, 2])).toEqual([]);
    expect(flattenStatusMap('x')).toEqual([]);
  });
});

function fakeContext(
  params: Record<string, unknown>,
  input: Partial<INodeExecutionData> = {},
  binaryBuffers: Record<string, Buffer> = {},
): IExecuteSingleFunctions {
  return {
    getNodeParameter: (name: string, fallback?: unknown) => (name in params ? params[name] : fallback),
    getNode: () => ({ name: 'Cognee', type: 'cognee', typeVersion: 1, position: [0, 0], parameters: {} }),
    getInputData: () => ({ json: {}, ...input }),
    helpers: { getBinaryDataBuffer: async (propertyName: string) => binaryBuffers[propertyName] },
  } as unknown as IExecuteSingleFunctions;
}

async function parseMultipart(options: IHttpRequestOptions): Promise<FormData> {
  const contentType = String(options.headers?.['Content-Type']);
  expect(contentType).toMatch(/^multipart\/form-data; boundary=/);
  return new Response(new Uint8Array(options.body as Buffer), { headers: { 'Content-Type': contentType } }).formData();
}

describe('flattenStatusOutput', () => {
  it('emits one n8n item per dataset row', async () => {
    const response = { body: { 'd-1': 'completed', 'd-2': 'failed' }, headers: {}, statusCode: 200 };
    const items = await flattenStatusOutput.call(fakeContext({}), [{ json: response.body }], response);
    expect(items.map((i) => i.json)).toEqual([
      { dataset_id: 'd-1', status: 'completed' },
      { dataset_id: 'd-2', status: 'failed' },
    ]);
  });
});

describe('buildUpdateBody', () => {
  it('uploads replacement text as update.txt plus node sets, leaving query params to routing', async () => {
    const ctx = fakeContext({
      updateInputType: 'text',
      updateText: 'new content',
      updateAdditionalFields: { nodeSet: ['team-a'] },
    });
    const result = await buildUpdateBody.call(ctx, { url: '/v1/update', qs: { data_id: 'x', dataset_id: 'd' } });
    expect(result.qs).toEqual({ data_id: 'x', dataset_id: 'd' });
    const form = await parseMultipart(result);
    const file = form.get('data') as File;
    expect(file.name).toBe('update.txt');
    expect(await file.text()).toBe('new content');
    expect(form.getAll('node_set')).toEqual(['team-a']);
  });

  it('uploads a binary property with an optional file name override', async () => {
    const bytes = Buffer.from('%PDF');
    const ctx = fakeContext(
      { updateInputType: 'binary', updateBinaryProperty: 'file', updateAdditionalFields: { fileName: 'renamed.pdf' } },
      { binary: { file: { data: '', fileName: 'orig.pdf', mimeType: 'application/pdf' } } },
      { file: bytes },
    );
    const form = await parseMultipart(await buildUpdateBody.call(ctx, { url: '/v1/update' }));
    const file = form.get('data') as File;
    expect(file.name).toBe('renamed.pdf');
    expect(file.type).toBe('application/pdf');
    expect(Buffer.from(await file.arrayBuffer())).toEqual(bytes);
  });

  it('rejects empty text and a missing binary property', async () => {
    await expect(buildUpdateBody.call(fakeContext({ updateInputType: 'text', updateText: ' ' }), { url: '/v1/update' })).rejects.toThrow(
      /Provide the new text/,
    );
    await expect(
      buildUpdateBody.call(fakeContext({ updateInputType: 'binary', updateBinaryProperty: 'nope' }), { url: '/v1/update' }),
    ).rejects.toThrow(/No binary data found/);
  });
});

describe('loadOptions.getDatasets', () => {
  it('calls GET {baseUrl}/api/v1/datasets with the credential and returns options', async () => {
    const calls: unknown[] = [];
    const ctx = {
      getCredentials: async () => ({ baseUrl: 'https://tenant.example.com/', apiKey: 'k' }),
      helpers: {
        httpRequestWithAuthentication: async function (credentialType: string, options: IHttpRequestOptions) {
          calls.push({ credentialType, options });
          return [{ id: 'd-1', name: 'docs' }];
        },
      },
    };
    const node = new Cognee();
    const options = await node.methods.loadOptions.getDatasets.call(ctx as never);
    expect(options).toEqual([{ name: 'docs', value: 'd-1', description: 'd-1' }]);
    expect(calls).toEqual([
      {
        credentialType: 'cogneeApi',
        options: { method: 'GET', url: 'https://tenant.example.com/api/v1/datasets', json: true },
      },
    ]);
  });
});
