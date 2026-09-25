import { randomUUID } from 'node:crypto';

/**
 * Dependency-free multipart/form-data encoder.
 *
 * Cognee's v1 ingestion endpoints (/v1/add, /v1/remember, /v1/update) only
 * accept content as uploaded files, so text has to travel as a file part.
 * n8n's request helper sends a Buffer body untouched when a Content-Type
 * header is already set, and community nodes may not depend on the
 * `form-data` package, so the body is encoded by hand here.
 */

export interface MultipartField {
  name: string;
  value: string;
}

export interface MultipartFile {
  name: string;
  filename: string;
  contentType: string;
  data: Buffer;
}

export type MultipartPart = MultipartField | MultipartFile;

export function isMultipartFile(part: MultipartPart): part is MultipartFile {
  return 'filename' in part;
}

export function createMultipartBoundary(): string {
  return `----cognee-n8n-${randomUUID()}`;
}

export function multipartContentType(boundary: string): string {
  return `multipart/form-data; boundary=${boundary}`;
}

/** Keep header parameters single-line and quote-safe (RFC 7578 §4.2). */
function headerParam(value: string): string {
  return value.replace(/["\r\n]/g, (char) => encodeURIComponent(char));
}

export function encodeMultipart(parts: MultipartPart[], boundary: string): Buffer {
  const CRLF = '\r\n';
  const chunks: Buffer[] = [];

  for (const part of parts) {
    if (isMultipartFile(part)) {
      chunks.push(
        Buffer.from(
          `--${boundary}${CRLF}` +
            `Content-Disposition: form-data; name="${headerParam(part.name)}"; ` +
            `filename="${headerParam(part.filename)}"${CRLF}` +
            `Content-Type: ${part.contentType}${CRLF}${CRLF}`,
          'utf8',
        ),
        part.data,
        Buffer.from(CRLF),
      );
    } else {
      chunks.push(
        Buffer.from(
          `--${boundary}${CRLF}` +
            `Content-Disposition: form-data; name="${headerParam(part.name)}"${CRLF}${CRLF}` +
            `${part.value}${CRLF}`,
          'utf8',
        ),
      );
    }
  }

  chunks.push(Buffer.from(`--${boundary}--${CRLF}`));
  return Buffer.concat(chunks);
}

export interface TextIngestionOptions {
  /** Text items; each becomes one uploaded `data` file part. Empty items are skipped. */
  texts: string[];
  datasetName?: string;
  datasetId?: string;
  nodeSet?: string[];
  runInBackground?: boolean;
  /** Base name for generated files; item N is uploaded as `<prefix>-N.txt`. */
  filenamePrefix?: string;
}

/**
 * Build the form parts shared by Cognee's text-ingestion endpoints.
 * Cognee derives each data item's name from the upload filename stem, so the
 * generated names show up later in dataset listings.
 */
export function buildTextIngestionParts(options: TextIngestionOptions): MultipartPart[] {
  const prefix = options.filenamePrefix ?? 'text';
  const parts: MultipartPart[] = [];

  options.texts
    .filter((text) => typeof text === 'string' && text.trim().length > 0)
    .forEach((text, index) => {
      parts.push({
        name: 'data',
        filename: `${prefix}-${index + 1}.txt`,
        contentType: 'text/plain; charset=utf-8',
        data: Buffer.from(text, 'utf8'),
      });
    });

  if (options.datasetId) {
    parts.push({ name: 'datasetId', value: options.datasetId });
  } else if (options.datasetName) {
    parts.push({ name: 'datasetName', value: options.datasetName });
  }

  for (const nodeSet of options.nodeSet ?? []) {
    if (nodeSet && nodeSet.trim()) {
      parts.push({ name: 'node_set', value: nodeSet.trim() });
    }
  }

  if (options.runInBackground) {
    parts.push({ name: 'run_in_background', value: 'true' });
  }

  return parts;
}
