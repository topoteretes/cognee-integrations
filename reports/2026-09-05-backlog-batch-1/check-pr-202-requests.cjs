const assert = require('node:assert/strict');
const path = require('node:path');
if (!process.argv[2]) throw new Error('Pass the path to a built integrations/n8n checkout');
const { Cognee } = require(path.resolve(process.argv[2], 'dist/nodes/Cognee/Cognee.node.js'));
const properties = new Cognee().description.properties;
function operation(resource) {
  const prop = properties.find(p => p.name === 'operation' && p.displayOptions?.show?.resource?.includes(resource));
  return prop.options.find(o => o.value === resource);
}
async function prepare(resource, params) {
  let request = {headers: {'X-Test': 'preserved'}};
  const context = {getNodeParameter: (name, fallback) => params[name] ?? fallback};
  for (const hook of operation(resource).routing.send.preSend) request = await hook.call(context, request);
  return request;
}
(async () => {
 const defaults = await prepare('recall', {recallQuery:'audit question'});
 assert.deepEqual(defaults.body, {query:'audit question', search_type:null,scope:'auto',top_k:15});
 const scoped = await prepare('recall', {recallQuery:'question',recallSearchType:'CHUNKS',recallDatasets:'one',recallNodeName:['first','second'],recallSessionId:'sid',recallTopK:0});
 assert.deepEqual(scoped.body, {query:'question',search_type:'CHUNKS',scope:'auto',datasets:['one'],node_name:['first','second'],session_id:'sid'});
 const input = 'Unicode: café 漢字\r\nline two\n"quoted"';
 const remembered = await prepare('remember', {rememberText:input,rememberDatasetName:'notes',rememberDatasetId:'dataset-id',rememberSessionId:'sid',rememberNodeSet:['first','second']});
 assert.ok(Buffer.isBuffer(remembered.body));
 const form = await new Response(remembered.body,{headers:remembered.headers}).formData();
 assert.equal(await form.get('data').text(), input);
 assert.equal(form.get('data').name,'note.txt');
 assert.equal(form.get('datasetName'),'notes');
 assert.equal(form.get('datasetId'),'dataset-id');
 assert.equal(form.get('session_id'),'sid');
 assert.deepEqual(form.getAll('node_set'),['first','second']);
 assert.equal(form.get('run_in_background'),'true');
 assert.equal(remembered.headers['X-Test'],'preserved');
 const minimal = await prepare('remember',{rememberText:'note',rememberDatasetName:'notes',rememberRunInBackground:false});
 const minimalForm = await new Response(minimal.body,{headers:minimal.headers}).formData();
 assert.equal(minimalForm.get('run_in_background'),'false');
 for (const key of ['datasetId','session_id','node_set']) assert.equal(minimalForm.has(key),false);
 console.log('PASS: Recall defaults, scoped request, Unicode multipart upload, repeated tags, optional-field omission, background toggle.');
})().catch(error=>{console.error(error);process.exitCode=1;});
