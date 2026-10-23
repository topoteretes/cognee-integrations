/**
 * Inbound connector authentication. Lovable's custom MCP setup offers
 * "bearer token or API key" for servers without OAuth; this connector takes a
 * bearer token in the Authorization header (`Authorization: Bearer <token>`).
 * The token only unlocks the scope the deployment was configured with — it is
 * never forwarded to cognee and never appears in logs or tool results.
 */
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

export interface AuthDecision {
  ok: boolean;
  /** Index of the matching configured token (useful during rotation). */
  tokenIndex?: number;
  reason?: "missing" | "malformed" | "mismatch";
}

export type Authenticator = (authorizationHeader: string | undefined) => AuthDecision;

function digest(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}

export function createBearerAuthenticator(tokens: string[]): Authenticator {
  const digests = tokens.map(digest);
  return (header) => {
    if (!header) return { ok: false, reason: "missing" };
    const match = /^\s*Bearer\s+(\S+)\s*$/i.exec(header);
    if (!match) return { ok: false, reason: "malformed" };
    const presented = digest(match[1]);
    // Compare against every configured token, constant-time per comparison and
    // without early exit, so timing reveals neither which token matched nor
    // how many exist.
    let found = -1;
    for (let i = 0; i < digests.length; i += 1) {
      if (timingSafeEqual(presented, digests[i]) && found === -1) found = i;
    }
    return found === -1 ? { ok: false, reason: "mismatch" } : { ok: true, tokenIndex: found };
  };
}

/** A fresh high-entropy connector token for operators (`lovable-memory token`). */
export function generateToken(): string {
  return `lvm_${randomBytes(32).toString("base64url")}`;
}
