const REQUIRED = [
  'DISCORD_TOKEN',
  'DISCORD_CLIENT_ID',
  'DIRECTORY_BASE_URL',
  'DIRECTORY_API_KEY',
  'DOC_BASE_URL',
  'DOC_API_KEY',
  'LLM_BASE_URL',
  'LLM_API_KEY',
  'VERIFICATION_BASE_URL',
  'VERIFICATION_API_KEY',
];

export function loadConfig(env = process.env) {
  const missing = REQUIRED.filter((k) => !env[k]);
  if (missing.length > 0) {
    throw new Error(`Missing required env vars: ${missing.join(', ')}`);
  }
  return {
    discordToken: env.DISCORD_TOKEN,
    discordClientId: env.DISCORD_CLIENT_ID,
    // Optional dedicated testing guild. Commands always register globally (prod);
    // when this is set they ALSO register to this guild for instant dev updates.
    discordGuildId: env.DISCORD_GUILD_ID || undefined,
    directoryBaseUrl: env.DIRECTORY_BASE_URL.replace(/\/+$/, ''),
    directoryApiKey: env.DIRECTORY_API_KEY,
    docBaseUrl: env.DOC_BASE_URL.replace(/\/+$/, ''),
    docApiKey: env.DOC_API_KEY,
    llmBaseUrl: env.LLM_BASE_URL.replace(/\/+$/, ''),
    llmApiKey: env.LLM_API_KEY,
    verificationBaseUrl: env.VERIFICATION_BASE_URL.replace(/\/+$/, ''),
    verificationApiKey: env.VERIFICATION_API_KEY,
    meetingBaseUrl: env.MEETING_BASE_URL ? env.MEETING_BASE_URL.replace(/\/+$/, '') : undefined,
    meetingApiKey: env.MEETING_API_KEY || undefined,
    meetingWsUrl:
      env.MEETING_WS_URL ||
      (env.MEETING_BASE_URL
        ? env.MEETING_BASE_URL.replace(/\/+$/, '').replace(/^http(s?):\/\//, (_m, s) =>
            s ? 'wss://' : 'ws://',
          )
        : undefined),
  };
}
