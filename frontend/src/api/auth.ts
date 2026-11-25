import { request, type Schemas } from "./client";

export type LoginRequest = Schemas["LoginRequest"];
export type RegisterRequest = Schemas["RegisterRequest"];
export type TokenResponse = Schemas["TokenResponse"];
export type User = Schemas["UserOut"];

export const authApi = {
  login: (body: LoginRequest) => request<TokenResponse>("/auth/login", { method: "POST", body }),
  /** Always creates a passenger (the server ignores any role; CLAUDE.md rule 10). */
  register: (body: RegisterRequest) => request<User>("/auth/register", { method: "POST", body }),
  me: (token: string, signal?: AbortSignal) => request<User>("/auth/me", { token, signal }),
};
