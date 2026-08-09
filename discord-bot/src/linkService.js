import { AlreadyLinked, DirectoryUnavailable } from './directoryClient.js';
import {
  VerificationUnavailable,
  RateLimited,
  CodeExpired,
  TooManyAttempts,
  InvalidCode,
  NoPendingCode,
} from './verificationClient.js';

// /link establishes identity — must be user-invoked, never LLM-invoked.
export const llmSafe = false;

function subjectFor(discordUserId) {
  return `discord:${discordUserId}`;
}

export function createLinkService({ directory, verification }) {
  async function linkByEmail({ email, discordUserId }) {
    try {
      const person = await directory.getPersonByEmail(email);
      if (!person) return { outcome: 'NOT_A_MEMBER' };

      await verification.requestCode({ subject: subjectFor(discordUserId), email });
      return { outcome: 'CODE_SENT', email };
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      if (e instanceof VerificationUnavailable) return { outcome: 'VERIFICATION_DOWN' };
      if (e instanceof RateLimited) return { outcome: 'RATE_LIMITED' };
      throw e;
    }
  }

  async function confirmAndLink({ discordUserId, discordHandle, code }) {
    try {
      const confirmation = await verification.confirmCode({
        subject: subjectFor(discordUserId),
        code,
      });
      const person = await directory.getPersonByEmail(confirmation.email);
      if (!person) return { outcome: 'NOT_A_MEMBER' };

      await directory.linkDiscord(person.id, {
        externalId: discordUserId,
        handle: discordHandle,
      });
      return { outcome: 'LINKED', person };
    } catch (e) {
      if (e instanceof AlreadyLinked) return { outcome: 'ALREADY_LINKED', detail: e.detail };
      if (e instanceof CodeExpired) return { outcome: 'CODE_EXPIRED' };
      if (e instanceof TooManyAttempts) return { outcome: 'TOO_MANY_ATTEMPTS' };
      if (e instanceof InvalidCode) return { outcome: 'INVALID_CODE' };
      if (e instanceof NoPendingCode) return { outcome: 'NO_PENDING_CODE' };
      if (e instanceof VerificationUnavailable) return { outcome: 'VERIFICATION_DOWN' };
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  return { linkByEmail, confirmAndLink };
}
