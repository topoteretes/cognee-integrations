/**
 * Opt-in live smoke for the Cognee n8n node's core contract:
 * add_text -> cognify -> search, asserting the remembered token comes back.
 *
 * The n8n node speaks the Cognee **Cloud** REST dialect (POST /add_text,
 * /cognify, /search with an X-Api-Key header) against a tenant base URL — NOT
 * the self-hosted server's /api/v1/* surface. So this smoke drives that exact
 * contract against a configured endpoint rather than booting a throwaway local
 * server the way the Python plugin smokes do. Point it at Cognee Cloud (or any
 * endpoint that exposes these routes) via env:
 *
 *   COGNEE_RUN_INTEGRATION=1 \
 *   COGNEE_BASE_URL=https://tenant-xxx.aws.cognee.ai \
 *   COGNEE_API_KEY=ck_... \
 *   npm test
 *
 * Skipped by default (no env) so CI stays green without creds.
 */

const RUN = process.env.COGNEE_RUN_INTEGRATION === '1';
const BASE_URL = (process.env.COGNEE_BASE_URL ?? '').replace(/\/$/, '');
const API_KEY = process.env.COGNEE_API_KEY ?? '';
const READY = RUN && Boolean(BASE_URL) && Boolean(API_KEY);

// The node prepends `/api` to every route (requestDefaults.baseURL =
// `{credentials.baseUrl}/api`), so COGNEE_BASE_URL is the bare tenant URL and we
// append `/api` here to hit the exact same endpoints the node does.
const API = `${BASE_URL}/api`;

const REASON =
  'set COGNEE_RUN_INTEGRATION=1, COGNEE_BASE_URL and COGNEE_API_KEY to run the n8n live smoke';

// describe.skip when unconfigured -> the suite is reported as skipped, exit 0.
(READY ? describe : describe.skip)(`n8n Cognee node live smoke (${REASON})`, () => {
  const headers = {
    'Content-Type': 'application/json',
    'X-Api-Key': API_KEY,
  };

  it('add_text -> cognify -> search returns the remembered token', async () => {
    const token = Math.random().toString(36).slice(2, 14);
    const dataset = `smoke_${token}`;
    const fact = `Integration smoke probe ${token}: the capital of Testland is ${token}ville.`;

    // 1. Add — mirrors the node's Add operation body { datasetName, textData }.
    const addRes = await fetch(`${API}/add_text`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ datasetName: dataset, textData: [fact] }),
    });
    expect(addRes.ok).toBe(true);

    // 2. Cognify synchronously so the graph is immediately queryable
    //    (mirrors { datasets, runInBackground }).
    const cognifyRes = await fetch(`${API}/cognify`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ datasets: [dataset], runInBackground: false }),
    });
    expect(cognifyRes.ok).toBe(true);

    // 3. Search — mirrors { searchType, datasets, query, topK }. The completion
    //    is LLM-synthesized, so retry a few times and assert the token appears.
    let found = false;
    for (let attempt = 0; attempt < 5 && !found; attempt++) {
      const searchRes = await fetch(`${API}/search`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          searchType: 'GRAPH_COMPLETION',
          datasets: [dataset],
          query: `What is the capital of Testland ${token}?`,
          topK: 5,
        }),
      });
      expect(searchRes.ok).toBe(true);
      const body = await searchRes.text();
      if (body.includes(token)) {
        found = true;
      } else {
        await new Promise((resolve) => setTimeout(resolve, 3000));
      }
    }

    expect(found).toBe(true);
  });
});