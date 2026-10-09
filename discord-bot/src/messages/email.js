export function renderAddEmailResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'CODE_SENT':
        return `📧 I've emailed a code to **${result.email}** — run \`/verify-email <code>\` to add it.`;
      case 'RATE_LIMITED':
        return 'That email was just sent a code. Please wait a moment and try again.';
      case 'VERIFICATION_DOWN':
        return 'The verification service is unavailable right now. Please try again shortly.';
      default:
        return 'Something went wrong. Please try again.';
    }
  })();
  return { content, ephemeral: true };
}

export function renderVerifyEmailResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'ADDED':
        return `✅ Added **${result.email}** to your account.`;
      case 'EMAIL_TAKEN':
        return "That email is already registered to a member, so I can't add it to your account.";
      case 'CODE_EXPIRED':
        return 'That code has expired. Run `/add-email` again to get a new one.';
      case 'TOO_MANY_ATTEMPTS':
        return 'Too many incorrect attempts. Run `/add-email` again to get a new code.';
      case 'INVALID_CODE':
        return "That code doesn't match. Double-check it and try again.";
      case 'NO_PENDING_CODE':
        return 'I have no pending code for you. Run `/add-email <email>` first.';
      case 'VERIFICATION_DOWN':
        return 'The verification service is unavailable right now. Please try again shortly.';
      case 'DIRECTORY_DOWN':
        return 'The directory is temporarily unavailable. Please try again shortly.';
      default:
        return 'Something went wrong. Please try again.';
    }
  })();
  return { content, ephemeral: true };
}
