// Collects the model-exposed tools from the bot's services, enforcing the
// llmSafe boundary (#71) as an explicit gate rather than a naming convention:
// a provider's tools are included ONLY when it exports `llmSafe === true`.
// Identity actions (linkService) and side-effecting senders (emailService)
// export `llmSafe = false`, or nothing, and so can never become model tools,
// even if they declare `tools`. Anything other than a literal `true` is unsafe.
//
// Each provider is `{ name, llmSafe, tools }` where `tools` is a list of
// `{ definition, run }`:
//   - `definition` is a /chat ToolDefinition (name, description, input_schema).
//   - `run(input, ctx)` executes the call. `ctx` carries the trusted, bound
//     retrieval context and collaborators; `input` is the model's arguments and
//     is never trusted to carry scope.
export function buildToolRegistry(providers) {
  const definitions = [];
  const handlers = new Map();
  for (const provider of providers ?? []) {
    if (provider?.llmSafe !== true) continue;
    for (const spec of provider.tools ?? []) {
      const name = spec?.definition?.name;
      if (!name) throw new Error(`tool from ${provider.name} has no definition name`);
      if (handlers.has(name)) {
        throw new Error(`duplicate tool name across services: ${name}`);
      }
      if (typeof spec.run !== 'function') {
        throw new Error(`tool ${name} has no run handler`);
      }
      definitions.push(spec.definition);
      handlers.set(name, spec.run);
    }
  }
  return {
    definitions,
    handlers,
    has(name) {
      return handlers.has(name);
    },
  };
}
