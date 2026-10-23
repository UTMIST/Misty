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
import { createMeetingClient } from './meeting/meetingClient.js';
import { createMeetingSurface } from './meeting/meetingSurface.js';
import { createRecorder } from './meeting/recorder.js';
import { createMeetingRecordClient } from './clients/meetingRecordClient.js';
import { createMeetingDelivery } from './meeting/meetingDelivery.js';

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
 * @param {Function} [deps.postMeetingLink] Posts/edits an existing saved record's link.
 * @returns {object} Shared application context passed through the router.
 */
export function createAppContext(config, { poster, notify, postMeetingLink } = {}) {
  const directory = createDirectoryClient({
    baseUrl: config.directoryBaseUrl,
    apiKey: config.directoryApiKey,
  });
  const docClient = createDocClient({
    baseUrl: config.docBaseUrl,
    apiKey: config.docApiKey,
  });
  const meetingDelivery = postMeetingLink
    ? createMeetingDelivery({
        records: createMeetingRecordClient({
          baseUrl: config.docBaseUrl,
          apiKey: config.docApiKey,
        }),
        post: postMeetingLink,
      })
    : null;
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

  // Authorized tool-execution loop (#71). The registry includes only llmSafe
  // tools; document retrieval is scoped per-call by the destination the flow
  // supplies. Built and injected unconditionally; whether the helper actually
  // routes through it is gated by config.helperToolsEnabled below.
  const toolRegistry = buildToolRegistry([
    {
      name: 'documentTools',
      llmSafe: documentToolsLlmSafe,
      tools: createDocumentTools({ docClient }),
    },
  ]);
  const toolExecutor = createToolExecutor({ llmClient, registry: toolRegistry });

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
      deliverRecord: meetingDelivery?.deliver,
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

  return {
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
    toolRegistry,
    toolExecutor,
    helperToolsEnabled: Boolean(config.helperToolsEnabled),
    meetingSurface,
    meetingDelivery,
    discordGuildId: config.discordGuildId ?? null,
    infrastructureDiscordUsername: config.infrastructureDiscordUsername,
  };
}
