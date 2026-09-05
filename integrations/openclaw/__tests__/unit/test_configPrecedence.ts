import { resolveConfig } from "../../src/config";

const original = process.env;
beforeEach(() => { process.env = { ...original }; for (const key of Object.keys(process.env)) if (key.startsWith("COGNEE_")) delete process.env[key]; });
afterEach(() => { process.env = original; });

it("explicit credentials beat env, while omitted credentials use env", () => {
  process.env.COGNEE_API_KEY = "env-key";
  expect(resolveConfig({ apiKey: "explicit-key" }).apiKey).toBe("explicit-key");
  expect(resolveConfig({}).apiKey).toBe("env-key");
});
it("honors false and rejects an unresolved placeholder", () => {
  expect(resolveConfig({ autoRecall: false }).autoRecall).toBe(false);
  expect(() => resolveConfig({ apiKey: "${COGNEE_MISSING_KEY}" })).toThrow();
});
it("documents the cloud mode environment override", () => {
  process.env.COGNEE_MODE = "cloud";
  expect(resolveConfig({ mode: "local" }).mode).toBe("cloud");
});
