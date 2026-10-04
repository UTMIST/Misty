import { createDirectoryClient } from './directoryClient.js';
import { createDocClient } from './docClient.js';
import { createVerificationClient } from './verificationClient.js';
import { createLinkService } from './linkService.js';
import { createEmailService } from './emailService.js';
import { createSeedService } from './seedService.js';
import { createTeamService } from './teamService.js';
import { createDocService } from './docService.js';
import { createLlmClient } from './llmClient.js';
import { createHelperService } from './helperService.js';
import { createMeetingClient } from './meeting/meetingClient.js';
import { createMeetingSurface } from './meeting/meetingSurface.js';
import { createRecorder } from './meeting/recorder.js';

/**
 * Wire application services and deployment metadata once at startup.
 *
 * context.js stays surface-agnostic (no discord.js import): the Discord
 * attachment poster consumed by meetingSurface is injected by the caller
 * (src/index.js, which already imports adapters/discord.js to wire the
 * client) rather than imported here. If omitted, a no-op poster is used —
 * fine for tests/other surfaces, but production wiring must pass the real
 * one for /record to actually post meeting minutes.
 *
 * @param {object} config Validated bot configuration.
 * @param {object} [deps] Optional injected collaborators.
 * @param {Function} [deps.poster] Posts a decoded meeting report to a channel.
 * @returns {object} Shared application context passed through the router.
 */
export function createAppContext(config, { poster, notify } = {}) {
  const directory = createDirectoryClient({
    baseUrl: config.directoryBaseUrl,
    apiKey: config.directoryApiKey,
  });
  const docClient = createDocClient({
    baseUrl: config.docBaseUrl,
    apiKey: config.docApiKey,
  });
  const verification = createVerificationClient({
    baseUrl: config.verificationBaseUrl,
    apiKey: config.verificationApiKey,
  });
  const linkService = createLinkService({ directory, verification });
  const emailService = createEmailService({ directory, verification });
  const seedService = createSeedService({ directory });
  const teamService = createTeamService({ directory });
  const docService = createDocService({ docClient, directory });
  const llmClient = createLlmClient({
    baseUrl: config.llmBaseUrl,
    apiKey: config.llmApiKey,
  });
  const helperService = createHelperService({ llmClient, directory });

  let meetingSurface;
  if (config.meetingBaseUrl) {
    const meetingClient = createMeetingClient({
      baseUrl: config.meetingBaseUrl,
      wsUrl: config.meetingWsUrl,
      apiKey: config.meetingApiKey,
    });
    meetingSurface = createMeetingSurface({
      meetingClient,
      makeRecorder: (o) => createRecorder(o),
      poster: poster ?? (async () => {}),
      notify: notify ?? (async () => {}),
      now: Date.now,
    });
  } else {
    // No meeting service configured — /record degrades gracefully instead of
    // crashing the bot at startup.
    meetingSurface = {
      start: () => ({ status: 'unconfigured' }),
      status: () => ({ status: 'not-recording' }),
      stop: async () => ({ status: 'not-recording' }),
    };
  }

  return {
    directory,
    docClient,
    verification,
    linkService,
    emailService,
    seedService,
    teamService,
    docService,
    llmClient,
    helperService,
    meetingSurface,
    discordGuildId: config.discordGuildId ?? null,
    infrastructureDiscordUsername: config.infrastructureDiscordUsername,
  };
}
