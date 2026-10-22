import { randomBytes } from 'node:crypto';

/** Short-lived, user-bound actions. Claim synchronously before any awaited work. */
export function createHelperActions() {
  const pending = new Map();
  function prune() {
    for (const [id, entry] of pending) if (entry.expires <= Date.now()) pending.delete(id);
  }
  return {
    add(action) {
      prune();
      if (
        pending.size >= 500 ||
        [...pending.values()].filter((e) => e.action.userId === action.userId).length >= 8
      ) {
        throw new Error('Too many pending actions. Cancel one or wait two minutes.');
      }
      const token = randomBytes(18).toString('base64url');
      pending.set(token, { action, expires: Date.now() + 120000 });
      return token;
    },
    get(token, userId) {
      prune();
      const entry = pending.get(token);
      return entry?.action.userId === userId ? entry.action : null;
    },
    take(token, userId) {
      const action = this.get(token, userId);
      if (action) pending.delete(token);
      return action;
    },
  };
}
