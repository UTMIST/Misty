import { randomBytes } from 'node:crypto';

const ACTION_TTL_MS = 120000;
export class PendingActionsFull extends Error {}

/** One review and single-use claim for every command proposed in a tool batch. */
export function combineHelperActions(actions) {
  if (actions.length === 1) return actions[0];
  if (actions.some((action) => action.userId !== actions[0].userId)) {
    throw new Error('Actions must belong to the same requester.');
  }
  return {
    userId: actions[0].userId,
    confirm: actions.some((action) => action.confirm),
    publicRead: actions.every((action) => action.publicRead && !action.confirm),
    summary: actions.map((action, i) => `${i + 1}. ${action.summary}`).join('\n\n'),
    async check() {
      for (const action of actions) await action.check();
    },
    setExpiresAt(expiresAt) {
      for (const action of actions) action.setExpiresAt?.(expiresAt);
    },
    async execute() {
      // Preflight the entire proposal before any command can change state.
      await this.check();
      const replies = [];
      for (const [index, action] of actions.entries()) {
        await action.check();
        try {
          replies.push(await action.execute());
        } catch {
          replies.push({
            content: `Action ${index + 1} could not be completed. Earlier actions may have completed; remaining actions were not run. Check the current state before asking again.`,
          });
          break;
        }
      }
      return { replies };
    },
  };
}

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
