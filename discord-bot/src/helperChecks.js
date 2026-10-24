/** Share work only within one validation step, never across effects or sends. */
export function checkOnce(checks, key, check) {
  if (!checks.has(key)) checks.set(key, Promise.resolve().then(check));
  return checks.get(key);
}
