export function renderLinkResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'CODE_SENT':
        return `📧 I've emailed a code to **${result.email}** — run \`/verify-code <code>\` to finish linking.`;
      case 'NOT_A_MEMBER':
        return "I couldn't find that email in the directory. Ask an exec to add you, then run `/link` again.";
      case 'DIRECTORY_DOWN':
        return 'The directory is temporarily unavailable. Please try again shortly.';
      case 'VERIFICATION_DOWN':
        return 'The verification service is temporarily unavailable. Please try again shortly.';
      case 'RATE_LIMITED':
        return "You've requested too many codes recently. Please wait a bit before trying again.";
      default:
        return 'Something went wrong. Please try again.';
    }
  })();
  return { content, ephemeral: true };
}

export function renderVerifyCodeResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'LINKED':
        return `✅ Linked! You're now identified as **${result.person.display_name}**.`;
      case 'NOT_A_MEMBER':
        return "I couldn't find that email in the directory. Ask an exec to add you, then run `/link` again.";
      case 'ALREADY_LINKED':
        return `That link couldn't be created: ${result.detail}`;
      case 'CODE_EXPIRED':
        return 'That code has expired. Run `/link` again to get a new one.';
      case 'TOO_MANY_ATTEMPTS':
        return 'Too many incorrect attempts. Run `/link` again to get a new code.';
      case 'INVALID_CODE':
        return "That code isn't right. Double check it and try again.";
      case 'NO_PENDING_CODE':
        return "I don't have a pending code for you. Run `/link` first.";
      case 'VERIFICATION_DOWN':
        return 'The verification service is temporarily unavailable. Please try again shortly.';
      case 'DIRECTORY_DOWN':
        return 'The directory is temporarily unavailable. Please try again shortly.';
      default:
        return 'Something went wrong. Please try again.';
    }
  })();
  return { content, ephemeral: true };
}
