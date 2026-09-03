import { describe, expect, it } from 'vitest';
import type { IDataObject, IExecuteSingleFunctions, IHttpRequestOptions, INodeExecutionData } from 'n8n-workflow';

import {
  buildForgetBody,
  buildRecallBody,
  buildRememberBody,
  buildRememberEntryBody,
  parseCognifyGraphModel,
  simplifyRecallOutput,
} from '../nodes/Cognee/Cognee.node';

function fakeContext(
  params: Record<string, unknown>,
  input: Partial<INodeExecutionData> = {},
  binaryBuffers: Record<string, Buffer> = {},
): IExecuteSingleFunctions {
  return {
    getNodeParameter: (name: string, fallback?: unknown) => (name in params ? params[name] : fallback),
    getNode: () => ({ name: 'Cognee', type: 'cognee', typeVersion: 1, position: [0, 0], parameters: {} }),
    getInputData: () => ({ json: {}, ...input }),
    helpers: {
      getBinaryDataBuffer: async (propertyName: string) => binaryBuffers[propertyName],
    },
  } as unknown as IExecuteSingleFunctions;
}

async function parseMultipart(options: IHttpRequestOptions): Promise<FormData> {
  const contentType = String(options.headers?.['Content-Type']);
  expect(contentType).toMatch(/^multipart\/form-data; boundary=/);
  return new Response(new Uint8Array(options.body as Buffer), {
    headers: { 'Content-Type': contentType },
  }).formData();
}

describe('buildRememberBody', () => {
  it('uploads text items as memory-N.txt with dataset and optional fields', async () => {
    const ctx = fakeContext({
      rememberInputType: 'text',
      rememberText: ['Einstein was born in Ulm.', 'He moved to Bern.'],
      rememberDatasetName: 'facts',
      rememberAdditionalFields: {
        sessionId: 'chat-1',
        nodeSet: ['people'],
        runInBackground: true,
        customPrompt: 'Focus on people.',
        chunkSize: 1024.7,
      },
    });
    const form = await parseMultipart(await buildRememberBody.call(ctx, { url: '/v1/remember' }));
    const files = form.getAll('data') as File[];
    expect(files.map((f) => f.name)).toEqual(['memory-1.txt', 'memory-2.txt']);
    expect(await files[0].text()).toBe('Einstein was born in Ulm.');
    expect(form.get('datasetName')).toBe('facts');
    expect(form.get('session_id')).toBe('chat-1');
    expect(form.getAll('node_set')).toEqual(['people']);
    expect(form.get('run_in_background')).toBe('true');
    expect(form.get('custom_prompt')).toBe('Focus on people.');
    expect(form.get('chunk_size')).toBe('1024');
  });

  it('prefers datasetId over datasetName and honours the file name prefix', async () => {
    const ctx = fakeContext({
      rememberInputType: 'text',
      rememberText: 'single',
      rememberDatasetName: 'ignored',
      rememberAdditionalFields: { datasetId: 'd-1', fileNamePrefix: 'note' },
    });
    const form = await parseMultipart(await buildRememberBody.call(ctx, { url: '/v1/remember' }));
    expect(form.get('datasetId')).toBe('d-1');
    expect(form.has('datasetName')).toBe(false);
    expect((form.get('data') as File).name).toBe('note-1.txt');
  });

  it('uploads the input binary property with its file name and mime type', async () => {
    const pdf = Buffer.from('%PDF-1.4 fake');
    const ctx = fakeContext(
      { rememberInputType: 'binary', rememberBinaryProperty: 'file', rememberDatasetName: 'docs' },
      { binary: { file: { data: '', fileName: 'report.pdf', mimeType: 'application/pdf' } } },
      { file: pdf },
    );
    const form = await parseMultipart(await buildRememberBody.call(ctx, { url: '/v1/remember' }));
    const file = form.get('data') as File;
    expect(file.name).toBe('report.pdf');
    expect(file.type).toBe('application/pdf');
    expect(Buffer.from(await file.arrayBuffer())).toEqual(pdf);
    expect(form.get('datasetName')).toBe('docs');
  });

  it('fails clearly when the binary property is missing or text is empty', async () => {
    await expect(
      buildRememberBody.call(
        fakeContext({ rememberInputType: 'binary', rememberBinaryProperty: 'nope', rememberDatasetName: 'd' }),
        { url: '/v1/remember' },
      ),
    ).rejects.toThrow(/No binary data found in property "nope"/);
    await expect(
      buildRememberBody.call(
        fakeContext({ rememberInputType: 'text', rememberText: ['', ' '], rememberDatasetName: 'd' }),
        { url: '/v1/remember' },
      ),
    ).rejects.toThrow(/at least one non-empty text item/);
  });
});

describe('buildRememberBody graph-building options', () => {
  it('forwards chunks_per_batch, ontology keys and a validated graph model', async () => {
    const model = { title: 'PeopleGraph', type: 'object', properties: {} };
    const ctx = fakeContext({
      rememberInputType: 'text',
      rememberText: 'Ada met Charles.',
      rememberDatasetName: 'facts',
      rememberAdditionalFields: {
        chunksPerBatch: 12.9,
        ontologyKeys: ['people-v1', ' ', 'orgs'],
        graphModel: JSON.stringify(model),
      },
    });
    const form = await parseMultipart(await buildRememberBody.call(ctx, { url: '/v1/remember' }));
    expect(form.get('chunks_per_batch')).toBe('12');
    expect(form.getAll('ontology_key')).toEqual(['people-v1', 'orgs']);
    expect(JSON.parse(String(form.get('graph_model')))).toEqual(model);
  });

  it('rejects a graph model without a title', async () => {
    const ctx = fakeContext({
      rememberInputType: 'text',
      rememberText: 't',
      rememberDatasetName: 'd',
      rememberAdditionalFields: { graphModel: '{"type": "object"}' },
    });
    await expect(buildRememberBody.call(ctx, { url: '/v1/remember' })).rejects.toThrow(/top-level "title"/);
  });
});

describe('parseCognifyGraphModel', () => {
  it('parses the routed graph_model string into an object and drops it when empty', async () => {
    const parsed = await parseCognifyGraphModel.call(fakeContext({}), {
      url: '/v1/cognify',
      body: { datasets: ['d'], graph_model: '{"title": "G", "type": "object"}' },
    });
    expect(parsed.body).toEqual({ datasets: ['d'], graph_model: { title: 'G', type: 'object' } });

    const dropped = await parseCognifyGraphModel.call(fakeContext({}), {
      url: '/v1/cognify',
      body: { datasets: ['d'], graph_model: '' },
    });
    expect(dropped.body).toEqual({ datasets: ['d'] });
  });

  it('leaves bodies without graph_model untouched and rejects invalid schemas', async () => {
    const untouched = await parseCognifyGraphModel.call(fakeContext({}), { url: '/v1/cognify', body: { datasets: ['d'] } });
    expect(untouched.body).toEqual({ datasets: ['d'] });
    await expect(
      parseCognifyGraphModel.call(fakeContext({}), { url: '/v1/cognify', body: { graph_model: '{"no": "title"}' } }),
    ).rejects.toThrow(/top-level "title"/);
  });
});

describe('buildRememberEntryBody', () => {
  it('builds a qa entry JSON body', async () => {
    const ctx = fakeContext({
      entryType: 'qa',
      entrySessionId: 'chat-1',
      entryDatasetName: 'main_dataset',
      entryQuestion: 'Q?',
      entryAnswer: 'A.',
      entryAdditionalFields: { context: 'ctx', feedbackScore: 4 },
    });
    const result = await buildRememberEntryBody.call(ctx, { url: '/v1/remember/entry', headers: {} });
    expect(result.headers?.['Content-Type']).toBe('application/json');
    expect(result.body).toEqual({
      entry: { type: 'qa', question: 'Q?', answer: 'A.', context: 'ctx', feedback_score: 4 },
      session_id: 'chat-1',
      dataset_name: 'main_dataset',
    });
  });

  it('surfaces validation errors as node errors', async () => {
    const ctx = fakeContext({ entryType: 'feedback', entrySessionId: 's', entryQaId: '' });
    await expect(buildRememberEntryBody.call(ctx, { url: '/v1/remember/entry' })).rejects.toThrow(/QA ID is required/);
  });
});

describe('buildRecallBody and simplifyRecallOutput', () => {
  it('builds the recall body from node parameters', async () => {
    const ctx = fakeContext({
      recallQuery: 'Where was Einstein born?',
      recallSearchType: 'AUTO',
      recallDatasets: ['facts'],
      recallTopK: 5,
      recallAdditionalOptions: { sessionId: 'chat-1', scope: ['graph', 'session'], onlyContext: true },
    });
    const result = await buildRecallBody.call(ctx, { url: '/v1/recall' });
    expect(result.body).toEqual({
      query: 'Where was Einstein born?',
      search_type: null,
      datasets: ['facts'],
      top_k: 5,
      session_id: 'chat-1',
      scope: ['graph', 'session'],
      only_context: true,
    });
  });

  it('splits the array response into one item per hit and flattens when Simplify is on', async () => {
    const hits = [
      { source: 'graph', text: 'Ulm', kind: 'graph_completion' },
      { source: 'session', question: 'q', answer: 'a' },
    ];
    // n8n passes the whole body as a single item when a postReceive hook exists.
    const items: INodeExecutionData[] = [{ json: hits as unknown as IDataObject }];
    const response = { body: hits, headers: {}, statusCode: 200 };

    const simplified = await simplifyRecallOutput.call(fakeContext({ recallSimplify: true }), items, response);
    expect(simplified).toHaveLength(2);
    expect(simplified.map((i) => i.json.text)).toEqual(['Ulm', 'a']);
    expect(simplified[1].json.raw).toEqual(hits[1]);

    const raw = await simplifyRecallOutput.call(fakeContext({ recallSimplify: false }), items, response);
    expect(raw.map((i) => i.json)).toEqual(hits);
  });

  it('wraps a non-array body in a single item', async () => {
    const response = { body: { source: 'system', text: 'warming up', status: 'warming_up' }, headers: {}, statusCode: 200 };
    const out = await simplifyRecallOutput.call(fakeContext({ recallSimplify: true }), [{ json: response.body }], response);
    expect(out).toHaveLength(1);
    expect(out[0].json).toMatchObject({ source: 'system', text: 'warming up' });
  });
});

describe('buildForgetBody', () => {
  it('builds dataset, data item and everything payloads', async () => {
    const byName = await buildForgetBody.call(
      fakeContext({ forgetMode: 'dataset', forgetDatasetBy: 'name', forgetDatasetName: 'docs', forgetMemoryOnly: true }),
      { url: '/v1/forget' },
    );
    expect(byName.body).toEqual({ dataset: 'docs', memory_only: true });

    const item = await buildForgetBody.call(
      fakeContext({ forgetMode: 'dataItem', forgetDatasetBy: 'id', forgetDatasetId: 'd-1', forgetDataId: 'x-1' }),
      { url: '/v1/forget' },
    );
    expect(item.body).toEqual({ dataset_id: 'd-1', data_id: 'x-1' });

    const everything = await buildForgetBody.call(
      fakeContext({ forgetMode: 'everything', forgetConfirmEverything: true }),
      { url: '/v1/forget' },
    );
    expect(everything.body).toEqual({ everything: true });
  });

  it('blocks Forget Everything without the confirmation toggle', async () => {
    await expect(
      buildForgetBody.call(fakeContext({ forgetMode: 'everything', forgetConfirmEverything: false }), { url: '/v1/forget' }),
    ).rejects.toThrow(/confirmation toggle/);
  });
});
