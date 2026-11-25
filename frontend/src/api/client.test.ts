import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, request } from "./client";

const respond = (status: number, body: unknown) =>
  vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

describe("request", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends JSON and the bearer token", async () => {
    const fetchMock = respond(200, { ok: true });
    vi.stubGlobal("fetch", fetchMock);
    await request("/auth/me", { token: "abc", method: "POST", body: { a: 1 } });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/auth/me");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer abc");
    expect(init.body).toBe('{"a":1}');
  });

  it.each([
    [401, "unauthorized"],
    [409, "conflict"],
    [422, "invalid"],
    [429, "rate_limited"],
    [500, "server"],
  ])("maps %i to %s", async (status, code) => {
    vi.stubGlobal("fetch", respond(status, { detail: "nope" }));
    await expect(request("/x")).rejects.toMatchObject({ code, status, message: "nope" });
  });

  it("uses the first validation message", async () => {
    vi.stubGlobal("fetch", respond(422, { detail: [{ msg: "value is not a valid email address" }] }));
    await expect(request("/x")).rejects.toMatchObject({ message: "value is not a valid email address" });
  });

  it("reports a network failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const err = await request("/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("network");
  });
});
