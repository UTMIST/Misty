import { createDirectoryClient } from './clients/directoryClient.js';
import { createDocClient } from './clients/docClient.js';
import { createVerificationClient } from './clients/verificationClient.js';
import { createLinkService } from './services/linkService.js';
import { createEmailService } from './services/emailService.js';
import { createSeedService } from './services/seedService.js';
import { createTeamService } from './services/teamService.js';
import { createChannelAccessService } from './services/channelAccessService.js';
import { createDocService } from './services/docService.js';
import { createLlmClient } from './clients/llmClient.js';
import { createHelperService } from './services/helperService.js';
import { createHelperRequestLimiter } from './helperRequestLimiter.js';
import { createDocumentTools, llmSafe as documentToolsLlmSafe } from './helperTools.js';
import { buildToolRegistry } from './toolRegistry.js';
import { createToolExecutor } from './toolExecutor.js';
import { createCommandTools, llmSafe as commandToolsLlmSafe } from './commandTools.js';
import { resolvePrincipal } from './auth/principal.js';
import { createHelperActions } from './helperActions.js';
import { createMeetingClient } from './meeting/meetingClient.js';
import { createMeetingSurface } from './meeting/meetingSurface.js';
import { createRecorder } from './meeting/recorder.js';

/**
 * Wire application services and deployment metadata once at startup.
 *
 * context.js stays surface-agnostic (no discord.js import): the Discord
 * attachment poster consumed by meetingSurface is injected by the caller
 * (src/index.js, which already imports adapters/discord/ to wire the
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
  const channelAccessService = createChannelAccessService({ directory });
  const docService = createDocService({ docClient, directory });
  const llmClient = createLlmClient({
    baseUrl: config.llmBaseUrl,
    apiKey: config.llmApiKey,
  });
  const helperService = createHelperService({ llmClient, directory });
  const helperRequestLimiter = createHelperRequestLimiter({
    maxRequests: config.helperUserMaxRequests,
    windowSeconds: config.helperUserWindowSeconds,
  });

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
      qna: async () => ({ status: 'unconfigured' }),
    };
  }

  const appContext = {
    directory,
    docClient,
    verification,
    linkService,
    emailService,
    seedService,
    teamService,
    channelAccessService,
    docService,
    llmClient,
    helperService,
    helperRequestLimiter,
    helperActions: createHelperActions(),
    helperToolsEnabled: Boolean(config.helperToolsEnabled),
    meetingSurface,
    discordGuildId: config.discordGuildId ?? null,
    infrastructureDiscordUsername: config.infrastructureDiscordUsername,
  };
  // Only llmSafe tools enter the registry. Document reads bind the destination;
  // command actions bind the requester and use the shared router policy check.
  // The flag gates use, not construction, of this injected loop.
  appContext.toolRegistry = buildToolRegistry([
    {
      name: 'documentTools',
      llmSafe: documentToolsLlmSafe,
      tools: createDocumentTools({ docClient }),
    },
    {
      name: 'commandTools',
      llmSafe: commandToolsLlmSafe,
      tools: createCommandTools({ appContext }),
    },
  ]);
  appContext.toolExecutor = createToolExecutor({
    llmClient,
    registry: appContext.toolRegistry,
    async authenticate(request, principal) {
      if (!appContext.helperToolsEnabled || !request?.discordUserId || !principal?.person?.id)
        return false;
      const current = await resolvePrincipal(appContext.directory, request.discordUserId);
      return Boolean(
        current && current.person.active !== false && current.person.id === principal.person.id,
      );
    },
  });
  return appContext;
}
