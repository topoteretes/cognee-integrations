import skillsCogneePlugin, { CogneeSkillsClient } from "../index";
import type { SkillsPluginConfig, SkillSummary, SkillDetail } from "../index";

describe("cognee-openclaw-skills", () => {
  it("exports the default plugin object with correct shape", () => {
    expect(skillsCogneePlugin).toBeDefined();
    expect(skillsCogneePlugin.id).toBe("cognee-openclaw-skills");
    expect(skillsCogneePlugin.kind).toBe("skills");
    expect(typeof skillsCogneePlugin.register).toBe("function");
  });

  it("exports CogneeSkillsClient class", () => {
    expect(CogneeSkillsClient).toBeDefined();
    const client = new CogneeSkillsClient("http://localhost:8000");
    expect(client).toBeInstanceOf(CogneeSkillsClient);
  });
});
