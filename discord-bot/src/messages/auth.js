export const authMessages = {
  unavailable: () => ({
    content:
      "I can't verify you right now — the directory is unavailable. Please try again shortly.",
    ephemeral: true,
  }),
  denied: (reason) => ({
    content:
      reason === 'not_linked'
        ? 'You need to link your account first. Run `/link` to identify yourself, then try again.'
        : reason === 'forbidden'
          ? "You don't have permission to do that."
          : "You're not allowed to do that.",
    ephemeral: true,
  }),
  internalError: () => ({
    content: 'Something went wrong. Please try again.',
    ephemeral: true,
  }),
};
