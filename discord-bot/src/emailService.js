import { DirectoryUnavailable, EmailAlreadyRegistered } from './directoryClient.js';
import {
  VerificationUnavailable,
  RateLimited,
  CodeExpired,
  TooManyAttempts,
  InvalidCode,
  NoPendingCode,
} from './verificationClient.js';

// /add-email establishes a verified email — must be user-invoked, never LLM-invoked.
export const llmSafe = false;

function subjectFor(discordUserId) {
  return `email:${discordUserId}`;
}

export function createEmailService({ directory, verification }) {
  async function requestEmailCode({ discordUserId, email }) {
    try {
      await verification.requestCode({ subject: subjectFor(discordUserId), email });
      return { outcome: 'CODE_SENT', email };
    } catch (e) {
      if (e instanceof RateLimited) return { outcome: 'RATE_LIMITED' };
      if (e instanceof VerificationUnavailable) return { outcome: 'VERIFICATION_DOWN' };
      throw e;
    }
  }

  async function confirmAndAddEmail({ personId, discordUserId, code }) {
    try {
      const confirmation = await verification.confirmCode({
        subject: subjectFor(discordUserId),
        code,
      });
      await directory.addEmailIdentifier(personId, confirmation.email);
      return { outcome: 'ADDED', email: confirmation.email };
    } catch (e) {
      if (e instanceof EmailAlreadyRegistered) return { outcome: 'EMAIL_TAKEN' };
      if (e instanceof CodeExpired) return { outcome: 'CODE_EXPIRED' };
      if (e instanceof TooManyAttempts) return { outcome: 'TOO_MANY_ATTEMPTS' };
      if (e instanceof InvalidCode) return { outcome: 'INVALID_CODE' };
      if (e instanceof NoPendingCode) return { outcome: 'NO_PENDING_CODE' };
      if (e instanceof VerificationUnavailable) return { outcome: 'VERIFICATION_DOWN' };
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  return { requestEmailCode, confirmAndAddEmail };
}
