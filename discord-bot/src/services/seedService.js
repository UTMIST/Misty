import { PersonExists, DirectoryUnavailable } from '../clients/directoryClient.js';
import { rankOf } from '../auth/policy.js';

export const llmSafe = true;

export function createSeedService({ directory }) {
  async function seedPerson({ email, displayName, level }, { caller }) {
    if (rankOf(level) > rankOf(caller?.access_level)) {
      return {
        outcome: 'ESCALATION_DENIED',
        callerLevel: caller?.access_level ?? null,
        requestedLevel: level,
      };
    }
    try {
      const person = await directory.createPerson({
        displayName,
        primaryEmail: email,
        accessLevel: level,
      });
      return { outcome: 'SEEDED', person };
    } catch (e) {
      if (e instanceof PersonExists) return { outcome: 'EXISTS', detail: e.detail };
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }
  return { seedPerson };
}
