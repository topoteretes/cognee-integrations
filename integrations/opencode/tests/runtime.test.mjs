import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Outbox, sessionId, scrub, allowedTool } from '../dist/src/runtime.js';

test('native sessions and workspaces stay isolated', () => {
  assert.match(sessionId('/a', 'native'), /^opencode_/);
  assert.notEqual(sessionId('/a', 'one'), sessionId('/a', 'two'));
  assert.notEqual(sessionId('/a', 'one'), sessionId('/b', 'one'));
  assert.equal(sessionId('/a', 'one'), sessionId('/a/../a', 'one'));
});
test('filters sensitive paths and recursively redacts before persistence', () => {
  assert.equal(allowedTool('read', {filePath:'/project/.env.production'}, []), false);
  assert.equal(allowedTool('read', {path:'C:\\Users\\u\\.ssh\\id_rsa'}, []), false);
  assert.equal(allowedTool('shell', {}, ['edit']), false);
  assert.equal(allowedTool('edit', {filePath:'src/main.ts'}, ['edit']), true);
  const result = JSON.stringify(scrub({password:'secret-value', nested:'Authorization: Bearer abc123', key:'-----BEGIN PRIVATE KEY-----\nsensitive'}));
  for (const value of ['secret-value', 'abc123', 'sensitive']) assert.ok(!result.includes(value));
});
test('outbox survives restart, deduplicates IDs, and never retries an ambiguous write blindly', async () => {
  const root = mkdtempSync(join(tmpdir(),'opencode-outbox-'));
  try {
    let queue = new Outbox('scope', root);
    queue.enqueue('one','session',{type:'qa',question:'token',answer:'api_key=super-secret',context:'marker'});
    assert.ok(!readFileSync(queue.file,'utf8').includes('super-secret'));
    let calls = 0;
    await assert.rejects(queue.flush(async () => { calls++; throw new Error('lost response'); }, async () => false));
    queue = new Outbox('scope',root);
    queue.enqueue('one','session',{type:'qa'});
    await queue.flush(async () => { calls++; }, async () => false);
    assert.equal(calls,1);
    assert.equal(queue.status().uncertain,1);
    await queue.flush(async () => { calls++; }, async () => true);
    queue.enqueue('one','session',{type:'qa'});
    assert.deepEqual(queue.status(),{pending:0,uncertain:0,saved:1});
  } finally { rmSync(root,{recursive:true,force:true}); }
});

test('a definite authentication rejection can retry after credentials are fixed', async () => {
  const root = mkdtempSync(join(tmpdir(),'opencode-retry-'));
  try {
    const queue = new Outbox('scope',root);
    queue.enqueue('id','s',{type:'qa',question:'q',answer:'a'});
    await assert.rejects(queue.flush(async () => {throw new Error('Cognee request failed (401)');},async()=>false));
    assert.equal(queue.status().uncertain,0);
    await queue.flush(async()=>{},async()=>false);
    assert.equal(queue.status().saved,1);
  } finally {rmSync(root,{recursive:true,force:true});}
});
