// The authorized tool-execution loop (#71). Given a prepared transcript and a
// trusted retrieval context, it drives /chat through client-executed tool
// rounds until the model produces a final answer, enforcing the bot's policy at
// every step:
//
//   * Only registry tools exist. The registry already excludes non-llmSafe
//     services, so a tool_use for an unknown name is refused with an error
//     result and never executed.
//   * The retrieval context is bound here, once, from the answer's destination.
//     It is passed to every handler and never taken from model arguments, so a
//     person's privileges can't widen a channel's scope and the model can't
//     assert its own team list.
//   * A tool-round limit and a cumulative output-token (cost) bound cap the
//     work one mention can trigger.
//   * Assistant turns (including opaque reasoning/continuation blocks) are
//     replayed verbatim; tool results answer every outstanding call, in order,
//     in the immediately following user message, as /chat requires.
//
// Fail-closed note: the document service itself is the authorization boundary.
// It re-checks current grants on every /search and every /docs read and fails
// closed (503 on a scope lookup it can't complete, empty results for an
// unconfigured channel), so the loop does not re-derive authorization. The
// optional `scopeCheck` below is a DORMANT seam for a surface that can detect
// mid-generation revocation; no current surface supplies one, so by default the
// loop does not re-check and never returns WITHHELD. It is not an enforced
// guarantee today — do not describe it as one.

export const DEFAULT_MAX_TOOL_ROUNDS = 4;
export const DEFAULT_MAX_OUTPUT_TOKENS = 8000;
export const DEFAULT_PER_CALL_MAX_TOKENS = 1024;

// Outcomes the caller renders into a surface-appropriate reply.
export const OUTCOME = {
  ANSWER: 'answer', // content is the final text; citations may accompany it
  EMPTY: 'empty', // the model produced no usable text
  WITHHELD: 'withheld', // a supplied scopeCheck reported revocation; nothing published
  EXHAUSTED: 'exhausted', // tool-round or cost bound hit before a final answer
};

function toolUseBlocks(blocks) {
  return (blocks ?? []).filter((b) => b && b.type === 'tool_use');
}

export function createToolExecutor({
  llmClient,
  registry,
  maxToolRounds = DEFAULT_MAX_TOOL_ROUNDS,
  maxOutputTokens = DEFAULT_MAX_OUTPUT_TOKENS,
  perCallMaxTokens = DEFAULT_PER_CALL_MAX_TOKENS,
}) {
  async function stillAuthorized(scopeCheck) {
    // No checker supplied -> the dormant seam is off; scope is treated as fixed
    // for this run (the doc service remains the real boundary). A checker that
    // throws fails closed.
    if (typeof scopeCheck !== 'function') return true;
    try {
      return (await scopeCheck()) === true;
    } catch {
      return false;
    }
  }

  return {
    /**
     * @param messages   prepared transcript turns ({role, content}) — already
     *                   rendered/attributed by the caller.
     * @param system     system prompt string.
     * @param context    trusted retrieval context ({onBehalfOf} | {guildId, channelId}).
     * @param principal  authenticated caller, passed to handlers for per-call authz.
     * @param scopeCheck  optional async () => boolean; false/throw means access
     *                    narrowed. Dormant unless a surface supplies it.
     */
    async run({ messages, system, context, principal, scopeCheck }) {
      const history = [...messages];
      const citations = [];
      const seen = new Set();
      let outputTokens = 0;

      const collect = (list) => {
        for (const c of list ?? []) {
          const key = c?.doc_id ?? JSON.stringify(c);
          if (seen.has(key)) continue;
          seen.add(key);
          citations.push(c);
        }
      };

      for (let round = 0; ; round += 1) {
        if (!(await stillAuthorized(scopeCheck))) return { outcome: OUTCOME.WITHHELD };

        const res = await llmClient.chat({
          messages: history,
          system,
          tools: registry.definitions,
          maxTokens: perCallMaxTokens,
        });
        outputTokens += res.usage?.output_tokens ?? 0;

        const calls = res.stop_reason === 'tool_use' ? toolUseBlocks(res.content_blocks) : [];

        if (!calls.length) {
          if (!(await stillAuthorized(scopeCheck))) return { outcome: OUTCOME.WITHHELD };
          const content = (res.content ?? '').trim();
          if (!content) return { outcome: OUTCOME.EMPTY };
          return { outcome: OUTCOME.ANSWER, content, citations };
        }

        // The model wants tools but we've spent our budget: stop rather than
        // start another round we can't afford.
        if (round >= maxToolRounds || outputTokens > maxOutputTokens) {
          return { outcome: OUTCOME.EXHAUSTED };
        }

        // Replay the assistant turn verbatim (opaque continuation blocks and all)
        // so the signed/encrypted reasoning transcript stays intact.
        history.push({ role: 'assistant', content: res.content_blocks });

        // Execute each call in order; one tool_result per call id, all before any
        // text, as the /chat history contract requires.
        const results = [];
        for (const call of calls) {
          const handler = registry.handlers.get(call.name);
          if (!handler) {
            // Unknown or non-exposed tool: refuse, never execute.
            results.push(errorResult(call.id, 'tool is not available'));
            continue;
          }
          try {
            const out = await handler(call.input, { context, principal });
            collect(out?.citations);
            results.push({
              type: 'tool_result',
              tool_use_id: call.id,
              content: out?.content ?? null,
              is_error: out?.is_error === true,
            });
          } catch {
            results.push(errorResult(call.id, 'tool failed'));
          }
        }
        history.push({ role: 'user', content: results });
      }
    },
  };
}

function errorResult(toolUseId, message) {
  return {
    type: 'tool_result',
    tool_use_id: toolUseId,
    content: { error: message },
    is_error: true,
  };
}
