import { REST, Routes, SlashCommandBuilder } from 'discord.js';
import { loadConfig } from './config.js';
import { commands, partitionCommands } from './commands/index.js';

function applyOption(target, o) {
  if (o.type === 'string') {
    target.addStringOption((so) => {
      so.setName(o.name).setDescription(o.description).setRequired(!!o.required);
      if (o.autocomplete) so.setAutocomplete(true);
      else if (o.choices) so.addChoices(...o.choices);
      return so;
    });
  } else if (o.type === 'boolean') {
    target.addBooleanOption((bo) =>
      bo.setName(o.name).setDescription(o.description).setRequired(!!o.required),
    );
  } else if (o.type === 'user') {
    target.addUserOption((uo) =>
      uo.setName(o.name).setDescription(o.description).setRequired(!!o.required),
    );
  }
  // extend as needed
}

export function buildDiscordData(command) {
  const b = new SlashCommandBuilder().setName(command.name).setDescription(command.description);
  if (command.subcommands.length) {
    for (const sub of command.subcommands) {
      b.addSubcommand((s) => {
        s.setName(sub.name).setDescription(sub.description);
        for (const o of sub.options) applyOption(s, o);
        return s;
      });
    }
  } else {
    for (const o of command.options) applyOption(b, o);
  }
  return b.toJSON();
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const config = loadConfig();
  const { stable, beta } = partitionCommands([...commands.values()]);
  const stableBody = stable.map((c) => buildDiscordData(c));
  const betaBody = beta.map((c) => buildDiscordData(c));

  const rest = new REST({ version: '10' }).setToken(config.discordToken);

  // Stable commands register GLOBALLY — available in every server (production).
  // Global commands can take up to ~1 hour to propagate.
  await rest.put(Routes.applicationCommands(config.discordClientId), { body: stableBody });
  console.log(`Registered ${stableBody.length} stable commands globally (all servers).`);

  // Beta commands register ONLY to the dedicated testing guild, so they stay
  // exclusive to that server and never reach production.
  if (config.discordGuildId) {
    // Registering betaBody (which may be empty) also CLEARS any previously
    // registered guild commands, so promoted/removed beta commands don't linger.
    await rest.put(Routes.applicationGuildCommands(config.discordClientId, config.discordGuildId), {
      body: betaBody,
    });
    console.log(
      `Registered ${betaBody.length} beta commands to testing guild ${config.discordGuildId} (exclusive).`,
    );
  } else if (betaBody.length > 0) {
    console.warn(
      `${betaBody.length} beta command(s) NOT registered: set DISCORD_GUILD_ID to a testing guild to register them.`,
    );
  }
}
