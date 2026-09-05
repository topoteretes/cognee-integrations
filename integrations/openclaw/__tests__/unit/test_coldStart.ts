import { coldRecall, FirstRecall } from "../../src/coldStart";
const original = process.env;
beforeEach(() => { process.env = { ...original, COGNEE_RECALL_BACKOFF_MS: "0", COGNEE_RECALL_RETRIES: "1" }; });
afterEach(() => { process.env = original; });
it("tracks overlapping sessions independently", () => {
  const first = new FirstRecall();
  expect(first.claim("a")).toBe(true);
  expect(first.claim("b")).toBe(true);
  first.end("a");
  expect(first.claim("b")).toBe(false);
  expect(first.claim("a")).toBe(true);
  expect(first.claim(undefined)).toBe(false);
});
it("retries only first-call cold failures", async () => {
  const call = jest.fn().mockRejectedValueOnce(new Error("Cognee request failed (504)")).mockResolvedValue("ok");
  expect(await coldRecall(true, performance.now() + 1000, 100, call)).toBe("ok");
  expect(call).toHaveBeenCalledTimes(2);
  const denied = jest.fn().mockRejectedValue(new Error("Cognee request failed (401)"));
  await expect(coldRecall(true, performance.now() + 1000, 100, denied)).rejects.toThrow("401");
  expect(denied).toHaveBeenCalledTimes(1);
});
it("bounds a hung read and refuses backoff beyond the deadline", async () => {
  const started = performance.now();
  await expect(coldRecall(true, started + 40, 1000, () => new Promise(() => {}))).rejects.toThrow();
  expect(performance.now() - started).toBeLessThan(250);
  process.env.COGNEE_RECALL_BACKOFF_MS = "1000";
  const call = jest.fn().mockRejectedValue(new Error("Cognee request failed (504)"));
  await expect(coldRecall(true, performance.now() + 50, 100, call)).rejects.toThrow();
  expect(call).toHaveBeenCalledTimes(1);
});
it("does not retry subsequent turns", async () => {
  const call = jest.fn().mockRejectedValue(new DOMException("timeout", "AbortError"));
  await expect(coldRecall(false, performance.now() + 1000, 100, call)).rejects.toThrow();
  expect(call).toHaveBeenCalledTimes(1);
});
