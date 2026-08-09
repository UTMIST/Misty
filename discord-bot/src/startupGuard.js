/**
 * Startup checks that only apply to the web surface.
 * The presence of `dev:spoof` on the API key IS the "this is a dev environment"
 * declaration — the bot never boots into spoof mode against a production directory.
 */
export async function ensureDevSpoofScope(ctx) {
  const scopes = await ctx.directory.getSelfKeyScopes();
  if (!scopes.includes('dev:spoof')) {
    throw new Error(
      `Startup guard failed: this bot's team-tracking API key lacks the ` +
        `\`dev:spoof\` scope, which is required to enable the web playground. ` +
        `Issue a new key against a non-production team-tracking with ` +
        `\`--scopes ... dev:spoof\`.`,
    );
  }
}
