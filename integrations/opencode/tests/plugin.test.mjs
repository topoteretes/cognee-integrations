import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import plugin from '../dist/index.js';
import { CogneeHttpClient } from '../dist/src/client.js';

const json = (value,status=200) => new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
test('per-turn scopes, completed QA, traces, dedup, and lifecycle use the native session', async () => {
  const original = globalThis.fetch;
  const root = mkdtempSync(join(tmpdir(),'opencode-plugin-'));
  const calls=[];
  globalThis.fetch=async (url,init={}) => {
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : null;
    calls.push({url:String(url),body});
    if (String(url).endsWith('/datasets')) return json([{id:'a',name:'agent_sessions'},{id:'u',name:'user-facts'}]);
    if (String(url).endsWith('/recall')) return json(['use strict types']);
    return json({status:'session_stored',entry_id:'saved'});
  };
  let hooks;
  try {
    const messages=[{info:{id:'u1',role:'user'},parts:[{type:'text',text:'Question'}]},{info:{id:'a1',parentID:'u1',role:'assistant',time:{completed:1}},parts:[{type:'text',text:'Answer'}]}];
    hooks=await plugin({directory:root,project:{id:'p'},client:{session:{messages:async()=>({data:messages})}}},{cognee:{apiKey:'key',stateDir:root,readScopes:{user:'user-facts'}}});
    const input={sessionID:'native-one'};
    const output={message:{id:'u1'},parts:[{type:'text',text:'Question'}]};
    await hooks['chat.message'](input,output);
    await hooks['chat.message'](input,output);
    const system={system:[]};
    await hooks['experimental.chat.system.transform'](input,system);
    await hooks['experimental.chat.system.transform'](input,system);
    assert.equal(system.system.length,1);
    assert.match(system.system[0],/<user_memory>/);
    assert.equal(calls.filter(c=>c.url.endsWith('/recall')).length,2);
    await hooks['tool.execute.after']({sessionID:'native-one',callID:'call1',tool:'edit',args:{filePath:'src/main.ts'}},{output:'updated password=secret'});
    await hooks.event({event:{type:'session.idle',properties:{sessionID:'native-one'}}});
    await hooks.event({event:{type:'session.idle',properties:{sessionID:'native-one'}}});
    const entries=calls.filter(c=>c.url.endsWith('/remember/entry')).map(c=>c.body);
    assert.equal(entries.length,2);
    assert.ok(entries.every(e=>e.session_id.startsWith('opencode_')));
    assert.equal(entries.find(e=>e.entry.type==='qa').entry.answer,'Answer');
    assert.ok(!JSON.stringify(entries).includes('password=secret'));
    const registration=calls.find(c=>c.url.endsWith('/register')).body;
    assert.equal(registration.type,'opencode'); assert.equal(registration.source,'api');
  } finally { if(hooks) await hooks.dispose(); globalThis.fetch=original; rmSync(root,{recursive:true,force:true}); }
  assert.ok(calls.some(c=>c.url.endsWith('/unregister')));
});
test('404 lifecycle is optional but authentication failures propagate', async () => {
 const original=globalThis.fetch;
 try { const client=new CogneeHttpClient('https://example.test','key');
 globalThis.fetch=async()=>json({},404); assert.equal(await client.lifecycle('s','d'),false);
 globalThis.fetch=async()=>json({},401); await assert.rejects(client.lifecycle('s','d'),/401/);
 } finally {globalThis.fetch=original;}
});

test('the host entry point exports only plugin initializers', async () => {
 const module=await import('../dist/index.js');
 assert.deepEqual(Object.keys(module),['default']);
 const root=mkdtempSync(join(tmpdir(),'opencode-entry-'));
 const hooks=await module.default({directory:root,project:{id:'p'},client:{}},{cognee:{stateDir:root,autoCapture:false,autoRecall:false}});
 try { assert.equal(typeof hooks.dispose,'function'); }
 finally { await hooks.dispose(); rmSync(root,{recursive:true,force:true}); }
});
