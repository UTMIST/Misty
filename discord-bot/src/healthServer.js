import Fastify from 'fastify';

export function buildHealthServer(client, { idle = false } = {}) {
  const server = Fastify({ logger: false });
  server.get('/health/ready', async (_request, reply) => {
    if (idle) return { status: 'ok', discord: 'disabled' };
    if (!client.isReady()) {
      reply.code(503);
      return { status: 'discord bot unavailable' };
    }
    return { status: 'ok' };
  });
  return server;
}

export async function startHealthServer(client, port, options) {
  const server = buildHealthServer(client, options);
  await server.listen({ port: Number(port), host: '0.0.0.0' });
  return server;
}
