import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { dispatch } from '../router.js';
import { resolvePrincipal } from '../auth/principal.js';
import { authorize } from '../auth/policy.js';
import { DirectoryUnavailable } from '../directoryClient.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Build the local web-playground server around the neutral command registry.
 *
 * @param {object} dependencies Registry, application context, and reset hook.
 * @returns {Promise<object>} Configured Fastify server.
 */
export async function buildServer({ commands, appContext, onReset }) {
  const server = Fastify({ logger: false });
  const dmHistory = new Map();
  const dmRequestTails = new Map();

  function enqueueDmRequest(discordId, request) {
    const previous = dmRequestTails.get(discordId) ?? Promise.resolve();
    const current = previous.then(request, request);
    dmRequestTails.set(discordId, current);
    return current.finally(() => {
      if (dmRequestTails.get(discordId) === current) dmRequestTails.delete(discordId);
    });
  }

  await server.register(fastifyStatic, {
    root: path.join(__dirname, 'public'),
    prefix: '/',
  });

  server.get('/api/commands', async () => {
    return Array.from(commands.values()).map((c) => ({
      name: c.name,
      description: c.description,
      auth: typeof c.auth === 'string' ? c.auth : 'dynamic',
      options: c.options,
      subcommands: c.subcommands.map((s) => ({
        name: s.name,
        description: s.description,
        auth: typeof s.auth === 'string' ? s.auth : 'dynamic',
        options: s.options,
      })),
    }));
  });

  server.get('/api/people', async () => {
    const people = await appContext.directory.listPeople();
    const results = [];
    for (const p of people) {
      let discord_id = null;
      try {
        const identifiers = await appContext.directory.listIdentifiers(p.id);
        const discord = identifiers.find((i) => i.provider === 'discord');
        if (discord) discord_id = discord.external_id;
      } catch (e) {
        console.warn(`/api/people: failed to fetch identifiers for ${p.id}:`, e.message);
      }
      results.push({
        id: p.id,
        discord_id,
        display_name: p.display_name,
      });
    }
    return results;
  });

  server.post('/api/reset', async (req, reply) => {
    if (!onReset) {
      reply.code(501);
      return { error: 'reset not available' };
    }
    dmHistory.clear();
    await onReset();
    return { ok: true };
  });

  server.post('/api/helper/dm', async (req, reply) => {
    const { actingAs, content } = req.body ?? {};
    if (typeof actingAs !== 'string' || !actingAs.trim() || typeof content !== 'string') {
      reply.code(400);
      return { error: 'actingAs and content are required' };
    }
    return enqueueDmRequest(actingAs, async () => {
      let principal;
      try {
        principal = await resolvePrincipal(appContext.directory, actingAs);
      } catch (e) {
        if (e instanceof DirectoryUnavailable) {
          reply.code(503);
          return { error: "I can't verify you right now — the directory is unavailable." };
        }
        throw e;
      }
      if (!authorize('linked', principal).ok) {
        reply.code(403);
        return { error: 'You need to link your account first. Run `/link` in Discord.' };
      }
      const turns = dmHistory.get(actingAs) ?? [];
      turns.push({ role: 'user', text: content.trim(), authorId: actingAs, authorName: 'You' });
      const { content: answer } = await appContext.helperService.answer({ turns, principal });
      turns.push({ role: 'assistant', text: answer ?? '' });
      dmHistory.set(actingAs, turns);
      return { content: answer ?? '' };
    });
  });

  server.post('/api/commands/:name/run', async (req, reply) => {
    const command = commands.get(req.params.name);
    if (!command) {
      reply.code(404);
      return { error: 'unknown command' };
    }
    const { options = {}, subcommand = null, actingAs } = req.body ?? {};

    if (typeof actingAs !== 'string' || actingAs.trim() === '') {
      reply.code(400);
      return { error: 'actingAs is required' };
    }

    const activeOptions = subcommand
      ? (command.subcommands.find((s) => s.name === subcommand)?.options ?? [])
      : command.options;
    const coerced = { ...options };
    for (const o of activeOptions) {
      const value = coerced[o.name];
      if (o.type === 'user' && typeof value === 'string') {
        coerced[o.name] = { id: value };
      } else if (o.type === 'boolean' && typeof value === 'string') {
        if (value === '') delete coerced[o.name];
        else coerced[o.name] = value === 'true';
      }
    }

    const intent = {
      surface: 'web',
      commandName: command.name,
      options: coerced,
      subcommand,
      discordUserId: String(actingAs),
      discordHandle: `spoof-${actingAs}`,
    };
    const payload = await dispatch(intent, { commands, appContext });
    return payload ?? {};
  });

  server.setErrorHandler(async (err, req, reply) => {
    reply.code(err.statusCode ?? 500).send({ content: `Error: ${err.message}`, ephemeral: true });
  });

  return server;
}

export async function startWebServer({ commands, appContext, port = 3001, onReset }) {
  const server = await buildServer({ commands, appContext, onReset });
  await server.listen({ port, host: '127.0.0.1' });
  console.log(`Web playground: http://127.0.0.1:${port}`);
  return server;
}
