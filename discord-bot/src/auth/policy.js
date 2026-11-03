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

/**
 * Global promotion policy (#361). The caller must be admin/superuser, the
 * target must currently rank below the caller, and the destination must rank
 * above the target and at most the caller. Unknown levels never default to
 * member here: they are rejected before any write.
 */
export function promotionDecision({ callerLevel, targetLevel, requestedLevel }) {
  const known = (l) => typeof l === 'string' && Object.hasOwn(ACCESS_RANK, l);
  if (![callerLevel, targetLevel, requestedLevel].every(known)) return 'INVALID_LEVEL';
  const caller = ACCESS_RANK[callerLevel];
  const target = ACCESS_RANK[targetLevel];
  const requested = ACCESS_RANK[requestedLevel];
  if (caller < ACCESS_RANK.admin) return 'FORBIDDEN';
  if (target >= caller) return 'TARGET_NOT_LOWER';
  if (requested === target) return 'UNCHANGED';
  if (requested < target) return 'DEMOTION_UNSUPPORTED';
  if (requested > caller) return 'ESCALATION_DENIED';
  return 'ALLOWED';
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
