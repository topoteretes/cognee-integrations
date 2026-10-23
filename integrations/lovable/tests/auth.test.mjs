import test from "node:test";
import assert from "node:assert/strict";
import { createBearerAuthenticator, generateToken } from "../dist/index.js";

test("bearer tokens are matched exactly, case-insensitively on the scheme only", () => {
  const auth = createBearerAuthenticator(["lvm_one_0123456789abcdef", "lvm_two_0123456789abcdef"]);
  assert.deepEqual(auth("Bearer lvm_one_0123456789abcdef"), { ok: true, tokenIndex: 0 });
  assert.deepEqual(auth("bearer lvm_two_0123456789abcdef"), { ok: true, tokenIndex: 1 });
  assert.deepEqual(auth("Bearer LVM_ONE_0123456789ABCDEF"), { ok: false, reason: "mismatch" });
  assert.deepEqual(auth("Bearer lvm_one_0123456789abcde"), { ok: false, reason: "mismatch" });
  assert.deepEqual(auth(undefined), { ok: false, reason: "missing" });
  assert.deepEqual(auth("Basic abc"), { ok: false, reason: "malformed" });
  assert.deepEqual(auth("Bearer"), { ok: false, reason: "malformed" });
});

test("generated tokens are long, URL-safe and unique", () => {
  const a = generateToken();
  const b = generateToken();
  assert.match(a, /^lvm_[A-Za-z0-9_-]{40,}$/);
  assert.notEqual(a, b);
});
