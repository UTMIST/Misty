// Railway copies ordinary variables into PR environments. Only the persistent
// preview environment's exact ID may use the dev application's credentials.
export function runtimeMode(env = process.env, args = process.argv.slice(2)) {
  if (args.includes('--preview-idle')) return 'idle';
  if (!env.RAILWAY_ENVIRONMENT_ID && !env.RAILWAY_ENVIRONMENT_NAME) return 'normal';
  if (['production', 'staging'].includes(env.RAILWAY_ENVIRONMENT_NAME)) return 'normal';
  if (env.RAILWAY_ENVIRONMENT_NAME === 'dev') {
    if (
      !env.RAILWAY_ENVIRONMENT_ID ||
      env.RAILWAY_ENVIRONMENT_ID !== env.MISTY_PREVIEW_ENVIRONMENT_ID
    ) {
      throw new Error(
        'MISTY_PREVIEW_ENVIRONMENT_ID must identify this persistent dev environment.',
      );
    }
    return 'preview';
  }
  // Unknown Railway environments fail closed, including renamed PR copies.
  return 'idle';
}

export function discordEnvironment(env = process.env) {
  if (runtimeMode(env, []) !== 'preview') return env;
  const missing = ['DISCORD_TOKEN_DEV', 'DISCORD_CLIENT_ID_DEV', 'DISCORD_GUILD_ID_DEV'].filter(
    (key) => !env[key],
  );
  if (missing.length) throw new Error(`Missing preview env vars: ${missing.join(', ')}`);
  return {
    ...env,
    DISCORD_TOKEN: env.DISCORD_TOKEN_DEV,
    DISCORD_CLIENT_ID: env.DISCORD_CLIENT_ID_DEV,
    DISCORD_GUILD_ID: env.DISCORD_GUILD_ID_DEV,
  };
}
