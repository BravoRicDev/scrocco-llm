import bcrypt from "bcryptjs";

export async function hashPassword(pw) {
  if (typeof pw !== "string" || pw.length < 8) throw new Error("password troppo corta (min 8)");
  return bcrypt.hash(pw, 10);
}

export async function verifyPassword(pw, hash) {
  if (!pw || !hash) return false;
  try {
    return await bcrypt.compare(pw, hash);
  } catch {
    return false;
  }
}
