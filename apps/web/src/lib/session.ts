/**
 * Per-identity isolation of private client state.
 *
 * Everything cached for a signed-in person (query data, signed asset URLs, editor drafts)
 * belongs to one identity. When the identity changes (sign-out, sign-in as someone else,
 * session switch in another tab) the old state is cancelled and discarded before the new
 * identity's screens render:
 * - providers.tsx gives each identity its own QueryClient and remounts the app subtree;
 * - private query keys start with the identity (`pk`), so even a shared cache could not
 *   serve one person's data to another;
 * - api.ts drops responses to requests issued under a previous identity;
 * - modules with their own stores register a reset (`onIdentityReset`).
 */

let identity: string | null = null;
let generation = 0;
const resets = new Set<() => void>();

export function currentIdentity(): string | null {
  return identity;
}

/** Monotonic counter, bumped on every identity change. Captured by in-flight requests. */
export function sessionGeneration(): number {
  return generation;
}

export function onIdentityReset(fn: () => void): () => void {
  resets.add(fn);
  return () => resets.delete(fn);
}

/** Record the active identity. Returns true when it changed (and private state was reset). */
export function setIdentity(next: string | null): boolean {
  if (next === identity) return false;
  identity = next;
  generation += 1;
  for (const fn of resets) {
    try {
      fn();
    } catch {
      /* a failing reset must not block the others */
    }
  }
  return true;
}

/** Query key for private, per-person data. */
export function pk<T extends readonly unknown[]>(...parts: T): readonly ["user", string, ...T] {
  return ["user", identity ?? "anonymous", ...parts] as const;
}
