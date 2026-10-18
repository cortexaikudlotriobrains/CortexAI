export const MIN_PASSWORD_LENGTH = 12;
export const MAX_PASSWORD_LENGTH = 128;

export interface PasswordCheck {
  id: "length" | "email" | "varied";
  label: string;
  met: boolean;
}

/** Client-side mirror of the backend policy; the server remains authoritative. */
export function checkPassword(password: string, email: string): PasswordCheck[] {
  const normalizedEmail = email.trim().toLowerCase();
  const localPart = normalizedEmail.split("@", 1)[0] ?? "";
  const lowered = password.toLowerCase();
  const containsEmail =
    password.length > 0 &&
    ((normalizedEmail.length > 0 && lowered.includes(normalizedEmail)) ||
      (localPart.length >= 4 && lowered.includes(localPart)));
  return [
    {
      id: "length",
      label: `At least ${MIN_PASSWORD_LENGTH} characters`,
      met: password.length >= MIN_PASSWORD_LENGTH && password.length <= MAX_PASSWORD_LENGTH,
    },
    {
      id: "email",
      label: "Doesn't contain your email address",
      met: password.length > 0 && !containsEmail,
    },
    {
      id: "varied",
      label: "Not a repeated character",
      met: new Set(password).size >= 5,
    },
  ];
}

export function passwordMeetsPolicy(password: string, email: string): boolean {
  return checkPassword(password, email).every((check) => check.met);
}

const EMAIL_PATTERN = /^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$/;

export function isValidEmail(value: string): boolean {
  const email = value.trim();
  return email.length <= 254 && EMAIL_PATTERN.test(email);
}
