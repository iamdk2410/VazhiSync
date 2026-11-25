import { describe, expect, it } from "vitest";

import { checkLogin, checkSignup, isEmail } from "./validation";

describe("validation", () => {
  it("checks emails like the design", () => {
    expect(isEmail("a@b.co")).toBe(true);
    expect(isEmail("  a@b.co ")).toBe(true);
    expect(isEmail("a@b")).toBe(false);
    expect(isEmail("a b@c.de")).toBe(false);
    expect(isEmail("")).toBe(false);
  });

  it("needs an email and a non-empty password to sign in", () => {
    expect(checkLogin("a@b.co", "x").valid).toBe(true);
    expect(checkLogin("a@b.co", "").passwordOk).toBe(false);
    expect(checkLogin("nope", "x").emailOk).toBe(false);
  });

  it("checks name, length and matching passwords for sign-up", () => {
    expect(checkSignup("Asha", "a@b.co", "12345678", "12345678").valid).toBe(true);
    expect(checkSignup("  ", "a@b.co", "12345678", "12345678").nameOk).toBe(false);
    expect(checkSignup("Asha", "a@b.co", "1234567", "1234567").lengthOk).toBe(false);
    const typing = checkSignup("Asha", "a@b.co", "12345678", "");
    expect(typing.matchOk).toBe(false);
    expect(typing.mismatch).toBe(false);
    expect(checkSignup("Asha", "a@b.co", "12345678", "1234567x").mismatch).toBe(true);
    expect(checkSignup("A".repeat(101), "a@b.co", "12345678", "12345678").nameOk).toBe(false);
  });
});
