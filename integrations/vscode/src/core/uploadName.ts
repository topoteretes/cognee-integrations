import { createHash } from "node:crypto";

const HASH_LENGTH = 32;
const HASH_SUFFIX_RE = new RegExp(`-[0-9a-f]{${HASH_LENGTH}}(?=(\\.[^.]+)?$)`);

/**
 * `name.ext` → `name-<sha256[:32]>.ext`. cognee >= 1.6.0 treats an upload's
 * filename as its identity in the dataset and refuses (409) a known name with new
 * content, so a fixed name failed every note after the first, and two files
 * sharing a basename collided. The extension is kept: the server routes by it.
 */
export function contentAddressedName(filename: string, data: string): string {
  const dot = filename.lastIndexOf(".");
  const stem = dot > 0 ? filename.slice(0, dot) : filename;
  const ext = dot > 0 ? filename.slice(dot) : "";
  const digest = createHash("sha256").update(data, "utf8").digest("hex").slice(0, HASH_LENGTH);
  return `${stem}-${digest}${ext}`;
}

/**
 * The name the user uploaded, from a stored document name: drops the suffix
 * `contentAddressedName` added, with or without the extension the backend may
 * have stripped. Names without the suffix pass through unchanged.
 */
export function originalUploadName(documentName: string): string {
  return documentName.replace(HASH_SUFFIX_RE, "");
}
