/**
 * The scope a deployment is bound to. One dataset receives writes; that one
 * plus an explicit shared list are readable. Nothing the model sends can widen
 * this: tool handlers take dataset names from here, never from arguments, and
 * recall results naming any other dataset are dropped before they are returned.
 */
export class ScopePolicy {
  readonly writeDataset: string;
  readonly readDatasets: readonly string[];
  private readonly readable: Set<string>;

  constructor(projectDataset: string, sharedDatasets: readonly string[] = []) {
    this.writeDataset = projectDataset;
    const reads = [projectDataset, ...sharedDatasets.filter((name) => name !== projectDataset)];
    this.readDatasets = Object.freeze(Array.from(new Set(reads)));
    this.readable = new Set(this.readDatasets);
  }

  isReadable(datasetName: string | null | undefined): boolean {
    return typeof datasetName === "string" && this.readable.has(datasetName);
  }

  isWritable(datasetName: string): boolean {
    return datasetName === this.writeDataset;
  }

  /** Keep only items whose dataset this scope may read (defense in depth). */
  filterReadable<T extends { dataset_name?: string | null }>(items: T[]): { kept: T[]; dropped: number } {
    const kept: T[] = [];
    let dropped = 0;
    for (const item of items) {
      // An item without a dataset name cannot be attributed to a permitted
      // scope, so it is not returned either.
      if (this.isReadable(item.dataset_name)) kept.push(item);
      else dropped += 1;
    }
    return { kept, dropped };
  }
}
