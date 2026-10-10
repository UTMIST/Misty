import { Client, GatewayIntentBits } from 'discord.js';
import { loadConfig } from './config.js';
import { createAppContext } from './context.js';
import { commands } from './commands/index.js';
import { startHealthServer } from './healthServer.js';
import { wireDiscordClient } from './adapters/discord/index.js';
import { makeAttachmentPoster, makeChannelNotifier } from './adapters/discord/meetingPosts.js';
import { runtimeMode } from './runtimeMode.js';

async function main() {
  const mode = runtimeMode();
  if (mode === 'idle') {
    await startHealthServer(null, process.env.PORT || 3002, { idle: true });
    console.log('Discord disabled in this Railway environment. Select a PR in dev.');
    return;
  }
  if (mode === 'preview' && !process.env.RAILWAY_VOLUME_MOUNT_PATH) {
    throw new Error('dev requires a Railway volume to prevent overlapping bot deployments.');
  }
  if (
    mode === 'preview' &&
    (process.env.ENABLE_DISCORD === 'false' || process.env.ENABLE_WEB === 'true')
  ) {
    throw new Error('dev requires ENABLE_DISCORD=true and ENABLE_WEB=false.');
  }
  const config = loadConfig();
  // Inject the Discord attachment poster here (not inside context.js) so
  // context.js stays surface-agnostic — index.js is one of the few modules
  // allowed to import from adapters/discord/.
  const appContext = createAppContext(config, {
    poster: makeAttachmentPoster(),
    notify: makeChannelNotifier(),
  });

  const enableDiscord = process.env.ENABLE_DISCORD !== 'false';
  const enableWeb = process.env.ENABLE_WEB === 'true';

  if (enableDiscord) {
    // MessageContent is required to pass full helper-thread history to the LLM.
    // Discord always exposes content for direct mentions, but otherwise sends
    // an empty string unless this privileged intent is enabled both here and in
    // the application's Developer Portal settings.
    const client = new Client({
      intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent,
        GatewayIntentBits.GuildVoiceStates,
      ],
    });
    wireDiscordClient(client, { commands, appContext });
    client.once('clientReady', (c) => console.log(`Bot ready as ${c.user.tag}`));
    await startHealthServer(client, process.env.PORT || 3002);
    await client.login(config.discordToken).catch((err) => {
      console.error('Discord login failed:', err.message);
      process.exit(1);
    });
  }

  if (enableWeb) {
    const { ensureDevSpoofScope } = await import('./startupGuard.js');
    try {
      await ensureDevSpoofScope(appContext);
    } catch (e) {
      console.error(e.message);
      process.exit(2);
    }
    const { startWebServer } = await import('./web/server.js');
    const port = Number(process.env.WEB_PORT || 3001);
    await startWebServer({ commands, appContext, port });
  }

  if (!enableDiscord && !enableWeb) {
    console.error('No surface enabled. Set ENABLE_DISCORD=true or ENABLE_WEB=true.');
    process.exit(2);
  }
}

main().catch((err) => {
  console.error('Fatal:', err);
  process.exit(1);
});
