import { randomBytes } from 'node:crypto';

const ACTION_TTL_MS = 120000;
export class PendingActionsFull extends Error {}

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
        throw new PendingActionsFull('Too many pending actions. Cancel one or wait two minutes.');
      }
      const token = randomBytes(18).toString('base64url');
      const expires = Date.now() + ACTION_TTL_MS;
      action.setExpiresAt?.(expires);
      pending.set(token, { action, expires, presented: false });
      return token;
    },
    // Reserve before I/O, then start the user's window once the UI is delivered.
    // Reviewing the same token again must never keep it alive indefinitely.
    presented(token, userId) {
      if (!this.get(token, userId)) return;
      const entry = pending.get(token);
      if (entry.presented) return;
      entry.presented = true;
      entry.expires = Date.now() + ACTION_TTL_MS;
      entry.action.setExpiresAt?.(entry.expires);
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
