// Authorization: evaluate a command's declarative auth policy against a Principal.
//
// Vocabulary:
//   'public'    -> no identity required
//   'linked'    -> caller must be authenticated (a non-null Principal)
//   'admin'     -> caller's access_level rank must be >= admin
//   'superuser' -> caller's access_level rank must be >= superuser
//
// access_level is a global privilege on the directory Person (member < admin <
// superuser). Unknown policies deny (fail closed).
export const ACCESS_RANK = { member: 0, admin: 1, superuser: 2 };

export function rankOf(level) {
  return ACCESS_RANK[level] ?? 0;
}

export function authorize(policy, principal) {
  if (policy === 'public') return { ok: true };
  if (principal?.person?.active === false) return { ok: false, reason: 'not_linked' };
  if (policy === 'linked') {
    return principal ? { ok: true } : { ok: false, reason: 'not_linked' };
  }
  if (policy === 'admin' || policy === 'superuser') {
    if (!principal) return { ok: false, reason: 'not_linked' };
    return rankOf(principal.person?.access_level) >= ACCESS_RANK[policy]
      ? { ok: true }
      : { ok: false, reason: 'forbidden' };
  }
  return { ok: false, reason: 'unknown_policy' };
}
