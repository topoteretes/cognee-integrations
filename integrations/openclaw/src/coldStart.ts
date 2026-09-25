/** A bounded first-recall policy, independent for each conversation. */
export class FirstRecall {
  private readonly seen = new Set<string>();
  claim(key: string | undefined): boolean {
    if (!key || this.seen.has(key)) return false;
    this.seen.add(key);
    return true;
  }
  end(key: string): void { this.seen.delete(key); }
}

export function retryable(error: unknown): boolean {
  return error instanceof Error && (error.name === "AbortError" || /Cognee request failed \(504\)/.test(error.message));
}
function nonnegative(value: string | undefined, fallback: number): number {
  const number = value === undefined || value.trim() === "" ? NaN : Number(value);
  return Number.isFinite(number) && number >= 0 ? number : fallback;
}
export async function coldRecall<T>(first: boolean, deadline: number, attemptMs: number, call: (timeout: number) => Promise<T>): Promise<T> {
  const retries = first ? Math.min(3, Math.floor(nonnegative(process.env.COGNEE_RECALL_RETRIES, 1))) : 0;
  const backoff = nonnegative(process.env.COGNEE_RECALL_BACKOFF_MS, 500);
  for (let attempt = 0; ; attempt++) {
    const remaining = deadline - performance.now();
    if (remaining <= 0) throw new DOMException("recall budget exceeded", "AbortError");
    const timeout = Math.min(Math.max(1, attemptMs), remaining);
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        call(timeout),
        new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new DOMException("recall budget exceeded", "AbortError")), timeout); }),
      ]);
    } catch (error) {
      if (attempt >= retries || !retryable(error)) throw error;
      const delay = backoff * (attempt + 1);
      if (performance.now() + delay >= deadline) throw error;
      await new Promise((resolve) => setTimeout(resolve, delay));
    } finally { if (timer) clearTimeout(timer); }
  }
}

export async function withinRecallBudget<T>(deadline: number, work: () => Promise<T>, onTimeout: () => void = () => {}): Promise<T | undefined> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      work(),
      new Promise<undefined>((resolve) => { timer = setTimeout(() => { onTimeout(); resolve(undefined); }, Math.max(0, deadline - performance.now())); }),
    ]);
  } finally { if (timer) clearTimeout(timer); }
}
