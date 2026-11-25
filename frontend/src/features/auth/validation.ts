/** The designs' validation rules (and the backend's: password 8-128, name 1-100). */
export const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const PASSWORD_MIN = 8;
export const PASSWORD_MAX = 128;
export const NAME_MAX = 100;

export const isEmail = (value: string) => EMAIL_PATTERN.test(value.trim());
export const isName = (value: string) => value.trim().length > 0 && value.trim().length <= NAME_MAX;

export interface LoginCheck {
  emailOk: boolean;
  passwordOk: boolean;
  valid: boolean;
}

export function checkLogin(email: string, password: string): LoginCheck {
  const emailOk = isEmail(email);
  const passwordOk = password.length > 0;
  return { emailOk, passwordOk, valid: emailOk && passwordOk };
}

export interface SignupCheck {
  nameOk: boolean;
  emailOk: boolean;
  lengthOk: boolean;
  matchOk: boolean; // second password typed and equal
  mismatch: boolean; // second password typed and different
  valid: boolean;
}

export function checkSignup(name: string, email: string, password: string, again: string): SignupCheck {
  const nameOk = isName(name);
  const emailOk = isEmail(email);
  const lengthOk = password.length >= PASSWORD_MIN && password.length <= PASSWORD_MAX;
  const matchOk = again.length > 0 && again === password;
  const mismatch = again.length > 0 && again !== password;
  return { nameOk, emailOk, lengthOk, matchOk, mismatch, valid: nameOk && emailOk && lengthOk && matchOk };
}
