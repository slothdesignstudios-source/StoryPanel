import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "@api-types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** The API's typed error contract, as a throwable. */
export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly retryable: boolean,
    readonly requestId: string | null,
    readonly fields: { field: string; message: string }[] = [],
  ) {
    super(message);
  }
}

let tokenSource: () => Promise<string | null> = async () => null;
export function setTokenSource(fn: () => Promise<string | null>) {
  tokenSource = fn;
}
export async function authHeader(): Promise<Record<string, string>> {
  const token = await tokenSource();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const middleware: Middleware = {
  async onRequest({ request }) {
    const token = await tokenSource();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    // Every mutation carries a key so a retried request cannot create or charge twice.
    if (request.method !== "GET" && !request.headers.has("Idempotency-Key")) {
      request.headers.set("Idempotency-Key", crypto.randomUUID());
    }
    return request;
  },
  async onResponse({ response }) {
    if (response.ok) return response;
    let body: { error?: { code?: string; message?: string; retryable?: boolean; request_id?: string; fields?: { field: string; message: string }[] } } = {};
    try {
      body = await response.clone().json();
    } catch {
      /* non-JSON error body */
    }
    const e = body.error ?? {};
    throw new ApiError(
      e.code ?? "internal_error",
      e.message ?? "Something went wrong. Please try again.",
      response.status,
      e.retryable ?? response.status >= 500,
      e.request_id ?? response.headers.get("x-request-id"),
      e.fields ?? [],
    );
  },
  onError() {
    return new ApiError("network_error", "Can't reach StoryPanel. Check your connection and try again.", 0, true, null);
  },
};

export const api = createClient<paths>({ baseUrl: API_URL });
api.use(middleware);

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}
