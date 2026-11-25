/**
 * The one way the app talks to the VazhiSync API (CLAUDE.md: the frontend never reads the
 * database or the MCP servers directly). Request and response types come from the backend's
 * OpenAPI schema (`npm run gen:api` -> schema.d.ts), so they can't drift silently.
 */
import type { components } from "./schema";

export type Schemas = components["schemas"];
export type Role = Schemas["Role"];

export const API_URL: string = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";

export type ApiErrorCode =
  | "unauthorized" // 401: wrong credentials or the session ended
  | "forbidden" // 403
  | "not_found" // 404
  | "conflict" // 409: e.g. email already registered
  | "invalid" // 422: the server rejected the input
  | "rate_limited" // 429
  | "server" // 5xx
  | "network"; // no answer at all

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number;
  readonly detail: unknown;

  constructor(code: ApiErrorCode, status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

function codeFor(status: number): ApiErrorCode {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 422) return "invalid";
  if (status === 429) return "rate_limited";
  return "server";
}

function messageFrom(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === "string") return first.msg;
  }
  return fallback;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  token?: string | null;
  signal?: AbortSignal;
}

export async function request<T>(path: string, { method = "GET", body, token, signal }: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError("network", 0, "Can't reach VazhiSync right now.");
  }
  if (response.status === 204) return undefined as T;
  const data: unknown = await response.json().catch(() => undefined);
  if (!response.ok) {
    const detail = (data as { detail?: unknown } | undefined)?.detail;
    throw new ApiError(codeFor(response.status), response.status, messageFrom(detail, response.statusText), detail);
  }
  return data as T;
}
