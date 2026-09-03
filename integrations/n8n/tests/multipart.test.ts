import { describe, expect, it } from 'vitest';

import {
  buildTextIngestionParts,
  createMultipartBoundary,
  encodeMultipart,
  isMultipartFile,
  multipartContentType,
} from '../nodes/Cognee/multipart';

/** Parse an encoded body with Node's built-in multipart parser (undici). */
async function parse(body: Buffer, boundary: string): Promise<FormData> {
  const response = new Response(new Uint8Array(body), {
    headers: { 'Content-Type': multipartContentType(boundary) },
  });
  return response.formData();
}

describe('createMultipartBoundary', () => {
  it('produces unique boundaries made of safe characters', () => {
    const a = createMultipartBoundary();
    const b = createMultipartBoundary();
    expect(a).not.toBe(b);
    expect(a).toMatch(/^[A-Za-z0-9'()+_,\-./:=?]+$/);
    expect(a.length).toBeLessThanOrEqual(70);
  });
});

describe('encodeMultipart', () => {
  it('round-trips plain fields and file parts through a standard parser', async () => {
    const boundary = createMultipartBoundary();
    const body = encodeMultipart(
      [
        { name: 'datasetName', value: 'support_docs' },
        { name: 'node_set', value: 'alpha' },
        { name: 'node_set', value: 'beta' },
        {
          name: 'data',
          filename: 'text-1.txt',
          contentType: 'text/plain; charset=utf-8',
          data: Buffer.from('hello world', 'utf8'),
        },
      ],
      boundary,
    );

    const form = await parse(body, boundary);
    expect(form.get('datasetName')).toBe('support_docs');
    expect(form.getAll('node_set')).toEqual(['alpha', 'beta']);

    const file = form.get('data') as File;
    expect(file).toBeInstanceOf(File);
    expect(file.name).toBe('text-1.txt');
    expect(file.type.replace(/\s/g, '')).toBe('text/plain;charset=utf-8');
    expect(await file.text()).toBe('hello world');
  });

  it('keeps multi-byte UTF-8 content intact', async () => {
    const boundary = createMultipartBoundary();
    const text = 'Zdravo svete — čćžšđ 日本語 🚀';
    const body = encodeMultipart(
      [
        { name: 'datasetName', value: 'ünïcödé' },
        {
          name: 'data',
          filename: 'text-1.txt',
          contentType: 'text/plain; charset=utf-8',
          data: Buffer.from(text, 'utf8'),
        },
      ],
      boundary,
    );

    const form = await parse(body, boundary);
    expect(form.get('datasetName')).toBe('ünïcödé');
    expect(await (form.get('data') as File).text()).toBe(text);
  });

  it('preserves binary file bytes', async () => {
    const boundary = createMultipartBoundary();
    const bytes = Buffer.from(Array.from({ length: 256 }, (_, i) => i));
    const body = encodeMultipart(
      [{ name: 'data', filename: 'blob.bin', contentType: 'application/octet-stream', data: bytes }],
      boundary,
    );

    const file = (await parse(body, boundary)).get('data') as File;
    expect(Buffer.from(await file.arrayBuffer())).toEqual(bytes);
  });

  it('escapes quotes and newlines in names and filenames', async () => {
    const boundary = createMultipartBoundary();
    const body = encodeMultipart(
      [
        {
          name: 'data',
          filename: 'we"ird\r\nname.txt',
          contentType: 'text/plain',
          data: Buffer.from('x'),
        },
      ],
      boundary,
    );
    const headerBlock = body.toString('utf8').split('\r\n\r\n')[0];
    expect(headerBlock.split('\r\n')).toHaveLength(3); // boundary, disposition, content-type
    expect(headerBlock).toContain('filename="we%22ird%0D%0Aname.txt"');
    const file = (await parse(body, boundary)).get('data') as File;
    expect(await file.text()).toBe('x');
  });

  it('terminates the body with the closing boundary', () => {
    const boundary = 'abc123';
    const body = encodeMultipart([{ name: 'k', value: 'v' }], boundary).toString('utf8');
    expect(body.endsWith(`--${boundary}--\r\n`)).toBe(true);
    expect(body.startsWith(`--${boundary}\r\n`)).toBe(true);
  });
});

describe('buildTextIngestionParts', () => {
  it('creates one file part per non-empty text and a datasetName field', () => {
    const parts = buildTextIngestionParts({
      texts: ['first', '', '   ', 'second'],
      datasetName: 'docs',
    });
    const files = parts.filter(isMultipartFile);
    expect(files.map((f) => f.filename)).toEqual(['text-1.txt', 'text-2.txt']);
    expect(files.map((f) => f.data.toString())).toEqual(['first', 'second']);
    expect(parts.filter((p) => !isMultipartFile(p))).toEqual([
      { name: 'datasetName', value: 'docs' },
    ]);
  });

  it('prefers datasetId over datasetName and adds node sets and background flag', () => {
    const parts = buildTextIngestionParts({
      texts: ['t'],
      datasetName: 'ignored',
      datasetId: '11111111-2222-3333-4444-555555555555',
      nodeSet: ['agent-a', ' ', 'project-x '],
      runInBackground: true,
      filenamePrefix: 'memory',
    });
    expect(parts.filter(isMultipartFile)[0].filename).toBe('memory-1.txt');
    expect(parts.filter((p) => !isMultipartFile(p))).toEqual([
      { name: 'datasetId', value: '11111111-2222-3333-4444-555555555555' },
      { name: 'node_set', value: 'agent-a' },
      { name: 'node_set', value: 'project-x' },
      { name: 'run_in_background', value: 'true' },
    ]);
  });

  it('omits run_in_background when false', () => {
    const parts = buildTextIngestionParts({ texts: ['t'], datasetName: 'd', runInBackground: false });
    expect(parts.find((p) => p.name === 'run_in_background')).toBeUndefined();
  });
});
