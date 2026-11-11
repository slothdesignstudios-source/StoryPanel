import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "@api-types";
import { sessionGeneration } from "./session";

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

const issuedUnder = new WeakMap<Request, number>();

const middleware: Middleware = {
  async onRequest({ request }) {
    issuedUnder.set(request, sessionGeneration());
    const token = await tokenSource();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    // Every mutation carries a key so a retried request cannot create or charge twice.
    if (request.method !== "GET" && !request.headers.has("Idempotency-Key")) {
      request.headers.set("Idempotency-Key", crypto.randomUUID());
    }
    // Start a W3C trace in the browser so one id follows the click through API, workflow and worker.
    if (!request.headers.has("traceparent")) {
      const hex = (n: number) => Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => b.toString(16).padStart(2, "0")).join("");
      request.headers.set("traceparent", `00-${hex(16)}-${hex(8)}-01`);
    }
    return request;
  },
  async onResponse({ request, response }) {
    // A response to a request sent for a previous identity must never reach the next one.
    const gen = issuedUnder.get(request);
    if (gen !== undefined && gen !== sessionGeneration()) {
      throw new ApiError("session_changed", "Your session changed. Please try again.", 0, false, null);
    }
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
