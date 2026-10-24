/**
 * Which companies and projects this plugin may touch, and which cognee
 * datasets stand for them. Dataset names are opaque hashes of instance key +
 * company id + project id: organisation, not authorisation. Access is enforced
 * here (allowlists) and by the backend credential's own permissions.
 */
import { createHash } from "node:crypto";
import type { PluginConfig } from "./config.js";

export function datasetFor(instanceKey: string, companyId: string, projectId: string): string {
  const digest = createHash("sha256").update(`${instanceKey}\u0000${companyId}\u0000${projectId}`, "utf8").digest("hex");
  return `pc_${digest.slice(0, 16)}`;
}

export class ScopePolicy {
  constructor(private readonly config: PluginConfig) {}

  isCompanyEnabled(companyId: string | null | undefined): boolean {
    return typeof companyId === "string" && this.config.enabledCompanyIds.includes(companyId);
  }

  isProjectEnabled(companyId: string | null | undefined, projectId: string | null | undefined): boolean {
    return this.isCompanyEnabled(companyId) && typeof projectId === "string" && this.config.enabledProjectIds.includes(projectId);
  }

  /** The dataset a project's records are written to. */
  projectDataset(companyId: string, projectId: string): string {
    return datasetFor(this.config.instanceKey, companyId, projectId);
  }

  sharedDataset(): string | undefined {
    return this.config.companySharedDataset || undefined;
  }

  /**
   * Datasets an agent run may read: its own project's dataset (when the project
   * is enabled) plus the company-shared dataset when one is configured. A run
   * outside an enabled company reads nothing.
   */
  readDatasetsFor(companyId: string, projectId: string | null | undefined): string[] {
    if (!this.isCompanyEnabled(companyId)) return [];
    const datasets: string[] = [];
    if (projectId && this.isProjectEnabled(companyId, projectId)) datasets.push(this.projectDataset(companyId, projectId));
    const shared = this.sharedDataset();
    if (shared && !datasets.includes(shared)) datasets.push(shared);
    return datasets;
  }

  /** Keep only items whose dataset the run may read (defence in depth). */
  filterReadable<T extends { dataset_name?: string | null }>(items: T[], permitted: string[]): { kept: T[]; dropped: number } {
    const allowed = new Set(permitted);
    const kept: T[] = [];
    let dropped = 0;
    for (const item of items) {
      if (typeof item.dataset_name === "string" && allowed.has(item.dataset_name)) kept.push(item);
      else dropped += 1;
    }
    return { kept, dropped };
  }
}
