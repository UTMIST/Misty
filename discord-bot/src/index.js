import { Client, GatewayIntentBits } from 'discord.js';
import { loadConfig } from './config.js';
import { createAppContext } from './context.js';
import { commands } from './commands/index.js';
import {
  wireDiscordClient,
  makeAttachmentPoster,
  makeChannelNotifier,
} from './adapters/discord.js';

async function main() {
  const config = loadConfig();
  // Inject the Discord attachment poster here (not inside context.js) so
  // context.js stays surface-agnostic — index.js is one of the few modules
  // allowed to import from adapters/discord.js.
  const appContext = createAppContext(config, {
    poster: makeAttachmentPoster(),
    notify: makeChannelNotifier(),
  });

  const enableDiscord = process.env.ENABLE_DISCORD !== 'false';
  const enableWeb = process.env.ENABLE_WEB === 'true';

  if (enableDiscord) {
    // GuildMessages (non-privileged) is enough for the @mention helper: Discord
    // always delivers message.content for messages that mention the bot, so the
    // privileged MessageContent intent is intentionally NOT requested.
    // (Consequence: thread messages that don't re-mention the bot arrive with
    // empty content and are ignored — matches the "re-mention to follow up" design.)
    const client = new Client({
      intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.GuildVoiceStates,
      ],
    });
    wireDiscordClient(client, { commands, appContext });
    client.once('clientReady', (c) => console.log(`Bot ready as ${c.user.tag}`));
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
