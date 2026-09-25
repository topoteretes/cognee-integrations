import {
  DEFAULT_DATASET,
  DEFAULT_DATASET_PREFIX,
  buildTag,
  datasetNameForResource,
  resolveScope,
  resourceTag,
  sanitizeId,
  sessionIdFor,
  threadTag,
} from "../../src/scope.js";

describe("sanitizeId", () => {
  it("leaves safe characters untouched", () => {
    expect(sanitizeId("abc123_-XYZ")).toBe("abc123_-XYZ");
  });

  it("encodes colons and spaces", () => {
    expect(sanitizeId("user:1")).toBe("user%3A1");
    expect(sanitizeId("hello world")).toBe("hello%20world");
  });

  it("encodes dots (illegal in a cognee dataset name)", () => {
    const encoded = sanitizeId("a.b");
    expect(encoded).not.toContain(".");
    expect(encoded).toBe("a%2Eb");
  });

  it("never leaves a raw space or dot in its output, for arbitrary ids", () => {
    const ids = ["a b.c:d", "...", "   ", "no-op", "tab\ttab", "emoji 🎉 dept.", "a%2Eb"];
    for (const id of ids) {
      const out = sanitizeId(id);
      expect(out).not.toMatch(/[ .]/);
    }
  });

  it("is injective for a representative set of colliding-looking inputs", () => {
    const ids = ["a:b", "a%3Ab", "a.b", "a%2Eb", "a b", "a%20b", "resource:x", "thread:x", "a", "a "];
    const encoded = ids.map(sanitizeId);
    expect(new Set(encoded).size).toBe(ids.length);
  });
});

describe("buildTag / resourceTag / threadTag", () => {
  it("builds a stable kind:id tag", () => {
    expect(buildTag("resource", "acme")).toBe("resource:acme");
    expect(resourceTag("acme")).toBe("resource:acme");
    expect(threadTag("t-1")).toBe("thread:t-1");
  });

  it("produces collision-free tags for ids containing ':' or spaces", () => {
    // A resourceId that itself contains ':' must not be able to masquerade
    // as a different kind:id pair once embedded in the tag: "resource:evil"
    // built from resourceId="thread:evil" must differ from the real
    // threadTag("evil").
    const trickyResource = resourceTag("thread:evil");
    expect(trickyResource).toBe("resource:thread%3Aevil");
    expect(trickyResource).not.toBe(threadTag("evil"));
    expect(trickyResource).not.toBe("resource:thread:evil");

    const spaced = resourceTag("north america");
    expect(spaced).toBe("resource:north%20america");
    expect(spaced).not.toContain(" ");
  });

  it("two distinct resourceIds never collide after tagging", () => {
    const ids = ["a:b", "a%3Ab", "a b", "a.b", "plain"];
    const tags = ids.map(resourceTag);
    expect(new Set(tags).size).toBe(ids.length);
  });
});

describe("sessionIdFor", () => {
  it("prefixes with mastra_", () => {
    expect(sessionIdFor("thread-42")).toBe("mastra_thread-42");
  });

  it("passes ':' and spaces through unsanitized (no shared delimiter grammar to protect)", () => {
    expect(sessionIdFor("a:b")).toBe("mastra_a:b");
    expect(sessionIdFor("a b")).toBe("mastra_a b");
  });

  it("returns '' for a missing threadId", () => {
    expect(sessionIdFor(undefined)).toBe("");
    expect(sessionIdFor(null)).toBe("");
    expect(sessionIdFor("")).toBe("");
  });

  it("is injective for distinct thread ids (fixed prefix + distinct suffix)", () => {
    const ids = ["a", "b", "a:b", "ab", ":ab"];
    const sessions = ids.map(sessionIdFor);
    expect(new Set(sessions).size).toBe(ids.length);
  });
});

describe("datasetNameForResource", () => {
  it("tagged mode (default) always returns the single shared dataset", () => {
    expect(datasetNameForResource({}, "resource-1")).toBe(DEFAULT_DATASET);
    expect(datasetNameForResource({ scope: "tagged" }, "resource-1")).toBe(DEFAULT_DATASET);
    expect(datasetNameForResource({ scope: "tagged", dataset: "myapp" }, "resource-1")).toBe("myapp");
    // resourceId is irrelevant in tagged mode
    expect(datasetNameForResource({ dataset: "myapp" }, undefined)).toBe("myapp");
  });

  it("dataset-per-resource mode derives one dataset name per resource", () => {
    expect(datasetNameForResource({ scope: "dataset-per-resource" }, "acme")).toBe(
      `${DEFAULT_DATASET_PREFIX}acme`,
    );
  });

  it("dataset-per-resource honours a custom prefix", () => {
    expect(
      datasetNameForResource({ scope: "dataset-per-resource", datasetPrefix: "app_" }, "acme"),
    ).toBe("app_acme");
  });

  it("dataset-per-resource sanitizes an unsafe resourceId (no space/dot in the result)", () => {
    const name = datasetNameForResource({ scope: "dataset-per-resource" }, "acme corp.inc");
    expect(name).not.toMatch(/[ .]/);
    expect(name).toBe(`${DEFAULT_DATASET_PREFIX}acme%20corp%2Einc`);
  });

  it("dataset-per-resource falls back to a fixed suffix with no resourceId", () => {
    expect(datasetNameForResource({ scope: "dataset-per-resource" }, undefined)).toBe(
      `${DEFAULT_DATASET_PREFIX}default`,
    );
  });

  it("two distinct resourceIds never collide into the same per-resource dataset name", () => {
    const ids = ["a b", "a.b", "a:b", "a%20b"];
    const names = ids.map((id) => datasetNameForResource({ scope: "dataset-per-resource" }, id));
    expect(new Set(names).size).toBe(ids.length);
  });
});

describe("resolveScope", () => {
  it("tagged mode: dataset fixed, session_id prefixed, node_set has both tags", () => {
    const resolved = resolveScope({ dataset: "myapp" }, { threadId: "t1", resourceId: "r1" });
    expect(resolved.dataset).toBe("myapp");
    expect(resolved.sessionId).toBe("mastra_t1");
    expect(resolved.nodeSet).toEqual(["resource:r1", "thread:t1"]);
    expect(resolved.nodeNameFilter).toEqual(["resource:r1"]);
  });

  it("appends config.nodeSet extras after the resource/thread tags", () => {
    const resolved = resolveScope(
      { dataset: "myapp", nodeSet: ["env:prod", "team:core"] },
      { threadId: "t1", resourceId: "r1" },
    );
    expect(resolved.nodeSet).toEqual(["resource:r1", "thread:t1", "env:prod", "team:core"]);
  });

  it("omits a tag whose id is absent, and narrows the recall filter to the thread with no resourceId", () => {
    const resolved = resolveScope({ dataset: "myapp" }, { threadId: "t1" });
    expect(resolved.nodeSet).toEqual(["thread:t1"]);
    expect(resolved.nodeNameFilter).toEqual(["thread:t1"]);
    expect(resolved.sessionId).toBe("mastra_t1");
  });

  it("clears the recall filter only when neither id is present", () => {
    const resolved = resolveScope({ dataset: "myapp" }, {});
    expect(resolved.nodeNameFilter).toEqual([]);
    expect(resolved.nodeSet).toEqual([]);
  });

  it("prefers the resource filter over the thread filter when both ids are present", () => {
    const resolved = resolveScope({ dataset: "myapp" }, { threadId: "t1", resourceId: "r1" });
    expect(resolved.nodeNameFilter).toEqual(["resource:r1"]);
  });

  it("no threadId at all: sessionId is '', thread tag absent", () => {
    const resolved = resolveScope({ dataset: "myapp" }, { resourceId: "r1" });
    expect(resolved.sessionId).toBe("");
    expect(resolved.nodeSet).toEqual(["resource:r1"]);
  });

  it("dataset-per-resource mode: dataset derives from resourceId, tags unaffected", () => {
    const resolved = resolveScope(
      { scope: "dataset-per-resource", datasetPrefix: "app_" },
      { threadId: "t1", resourceId: "acme" },
    );
    expect(resolved.dataset).toBe("app_acme");
    expect(resolved.nodeSet).toEqual(["resource:acme", "thread:t1"]);
    expect(resolved.nodeNameFilter).toEqual(["resource:acme"]);
  });

  it("stays stable and collision-free for ids containing ':' or spaces end to end", () => {
    const a = resolveScope({ dataset: "myapp" }, { threadId: "team:a b", resourceId: "user:1" });
    const b = resolveScope({ dataset: "myapp" }, { threadId: "team", resourceId: "a b:user:1" });
    expect(a.nodeSet).not.toEqual(b.nodeSet);
    expect(new Set([...a.nodeSet, ...b.nodeSet]).size).toBe(a.nodeSet.length + b.nodeSet.length);
    expect(a.sessionId).toBe("mastra_team:a b");
  });
});
