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
//   * Tool-call, document-request, elapsed-time, and output-token limits cap
//     the work one mention can trigger, including publication checks.
//   * Assistant turns (including opaque reasoning/continuation blocks) are
//     replayed verbatim; tool results answer every outstanding call, in order,
//     in the immediately following user message, as /chat requires.
//
// Document tools supply revalidation callbacks using the same authenticated
// API and destination. Check all previously read evidence before each model
// call and before publication; the surface checks again at each send boundary.
// Callback errors fail closed. These callbacks are never sent to the model.

import { combineHelperActions, MAX_HELPER_ACTIONS } from './helperActions.js';
import { checkOnce } from './helperChecks.js';

const PREPARATION_CLARIFICATION =
  "All command preparations failed. Nothing was changed. Explain the tool errors or ask for the missing details; for an invalid user ID ask for an explicit member mention, and for an unavailable action suggest checking command access with /help privately. Do not infer the requester's role or disclose account details. Do not call any tools or retry a subset of the request. The user must clarify or submit a new request before any action can be prepared.";

export const DEFAULT_MAX_TOOL_ROUNDS = 4;
export const DEFAULT_MAX_OUTPUT_TOKENS = 8000;
export const DEFAULT_PER_CALL_MAX_TOKENS = 4096;
export const DEFAULT_MAX_TOOL_CALLS = 4;
export const DEFAULT_MAX_DOCUMENT_REQUESTS = 32;
export const DEFAULT_MAX_DURATION_MS = 120000;

// Outcomes the caller renders into a surface-appropriate reply.
export const OUTCOME = {
  ACTION: 'action', // terminal private result or confirmation, never model replay
  INCOMPLETE_ACTION: 'incomplete_action', // a command proposal failed; nothing executed
  ANSWER: 'answer', // content is the final text; citations may accompany it
  EMPTY: 'empty', // the model produced no usable text
  WITHHELD: 'withheld', // access/evidence could not be revalidated; nothing published
  EXHAUSTED: 'exhausted', // a work limit was reached before a final answer
};

function toolUseBlocks(blocks) {
  return (blocks ?? []).filter((b) => b && b.type === 'tool_use');
}

export function createToolExecutor({
  llmClient,
  registry,
  authenticate,
  maxToolRounds = DEFAULT_MAX_TOOL_ROUNDS,
  maxOutputTokens = DEFAULT_MAX_OUTPUT_TOKENS,
  perCallMaxTokens = DEFAULT_PER_CALL_MAX_TOKENS,
  maxToolCalls = DEFAULT_MAX_TOOL_CALLS,
  maxDocumentRequests = DEFAULT_MAX_DOCUMENT_REQUESTS,
  maxDurationMs = DEFAULT_MAX_DURATION_MS,
}) {
  if (
    !Number.isSafeInteger(maxToolRounds) ||
    maxToolRounds < 0 ||
    !Number.isSafeInteger(maxOutputTokens) ||
    maxOutputTokens < 0 ||
    !Number.isSafeInteger(perCallMaxTokens) ||
    perCallMaxTokens <= 0 ||
    !Number.isSafeInteger(maxToolCalls) ||
    maxToolCalls < 0 ||
    !Number.isSafeInteger(maxDocumentRequests) ||
    maxDocumentRequests < 0 ||
    !Number.isSafeInteger(maxDurationMs) ||
    maxDurationMs <= 0 ||
    maxDurationMs > 2147483647
  )
    throw new RangeError('tool limits must be safe integers within their allowed range');

  return {
    /**
     * @param messages   prepared transcript turns ({role, content}) — already
     *                   rendered/attributed by the caller.
     * @param system     system prompt string.
     * @param context    trusted retrieval context ({onBehalfOf} | {guildId, channelId}).
     * @param principal  authenticated caller, passed to handlers for per-call authz.
     * @param scopeCheck  optional async () => boolean; false/throw means access
     *                    narrowed. Document evidence is always checked separately.
     */
    async run({ messages, system, context, principal, scopeCheck, request }) {
      const history = [...messages];
      const citations = [];
      const seen = new Set();
      let outputTokens = 0;
      let toolCalls = 0;
      let documentRequests = 0;
      let exhausted = false;
      let clarifyPreparation = false;
      let deadline = performance.now() + maxDurationMs;
      const isExhausted = () => exhausted || performance.now() >= deadline;
      // Text answers keep the generation deadline through delivery. A terminal
      // command action adopts the pending UI deadline below, keeping read limits.
      // Abort the client request and stop waiting even if a fake/adapter ignores
      // cancellation. An already accepted backend operation may still finish.
      const withDeadline = async (operation) => {
        if (isExhausted()) throw new Error('helper work limit reached');
        const controller = new AbortController();
        let timer;
        try {
          return await Promise.race([
            new Promise((_, reject) => {
              timer = setTimeout(
                () => {
                  exhausted = true;
                  controller.abort();
                  reject(new Error('helper deadline reached'));
                },
                Math.ceil(deadline - performance.now()),
              );
            }),
            operation(controller.signal),
          ]);
        } finally {
          clearTimeout(timer);
        }
      };
      // All document reads, including revalidation, must use this gate. Count
      // attempts before I/O; failed requests do not refund capacity.
      const readDocument = async (_key, read) => {
        if (isExhausted() || documentRequests >= maxDocumentRequests) {
          exhausted = true;
          throw new Error('helper work limit reached');
        }
        documentRequests += 1;
        return withDeadline(read);
      };
      const checkCaller = async () =>
        !authenticate || (await authenticate(request, principal)) === true;
      const checks = [checkCaller, ...(scopeCheck ? [scopeCheck] : [])];
      let withheld = false;
      const failedCheck = () => ({
        outcome: isExhausted() ? OUTCOME.EXHAUSTED : OUTCOME.WITHHELD,
      });
      const canPublish = async () => {
        if (withheld || isExhausted()) return false;
        // Share a fresh lookup only within this pass. Each callback still
        // validates its own original evidence; never reuse an earlier grant.
        const reads = new Map();
        const readForPass = (key, read) => {
          if (!reads.has(key)) reads.set(key, readDocument(key, read));
          return reads.get(key);
        };
        try {
          for (const check of checks) {
            if ((await withDeadline(() => check(readForPass))) !== true) {
              withheld = true;
              return false;
            }
          }
          return true;
        } catch {
          withheld = true;
          return false;
        }
      };

      const collect = (list) => {
        for (const c of list ?? []) {
          const key = c?.doc_id ?? JSON.stringify(c);
          if (seen.has(key)) continue;
          seen.add(key);
          citations.push(c);
        }
      };

      for (let round = 0; ; round += 1) {
        const remaining = maxOutputTokens - outputTokens;
        if (remaining <= 0 || isExhausted()) return { outcome: OUTCOME.EXHAUSTED };
        if (!(await canPublish())) return failedCheck();
        const maxTokens = Math.min(perCallMaxTokens, remaining);

        let res;
        try {
          res = await withDeadline((signal) =>
            llmClient.chat({
              messages: history,
              system: clarifyPreparation
                ? [system, PREPARATION_CLARIFICATION].filter(Boolean).join('\n\n')
                : system,
              // Replay requires the original definitions even during a final
              // clarification. The execution gate below rejects further calls.
              tools: registry.definitions,
              maxTokens,
              signal,
            }),
          );
        } catch (error) {
          if (isExhausted()) return { outcome: OUTCOME.EXHAUSTED };
          throw error;
        }
        // Missing/invalid usage cannot create free rounds: reserve the entire
        // requested allocation. A response exceeding it is never published.
        const reported = res.usage?.output_tokens;
        const used = Number.isSafeInteger(reported) && reported >= 0 ? reported : maxTokens;
        outputTokens += used;
        if (used > maxTokens) return { outcome: OUTCOME.EXHAUSTED };

        // Partial/filtered text is not a completed answer. Never execute or
        // replay blocks from those responses, and do not retry automatically.
        if (res.stop_reason === 'max_tokens') return { outcome: OUTCOME.EXHAUSTED };
        if (res.stop_reason === 'end_turn') {
          if (!(await canPublish())) return failedCheck();
          const content = (res.content ?? '').trim();
          if (!content) return { outcome: OUTCOME.EMPTY };
          return { outcome: OUTCOME.ANSWER, content, citations, canPublish };
        }
        if (res.stop_reason !== 'tool_use') return { outcome: OUTCOME.EMPTY };
        if (clarifyPreparation) return { outcome: OUTCOME.INCOMPLETE_ACTION };
        const calls = toolUseBlocks(res.content_blocks);
        if (!calls.length) return { outcome: OUTCOME.EMPTY };
        const commandBatch = calls.some((call) => registry.terminalTools?.has(call.name));
        const proposedActions = commandBatch
          ? calls.reduce(
              (count, call) => count + (registry.actionCounts?.get(call.name)?.(call.input) ?? 1),
              0,
            )
          : 0;

        // The model wants tools but we've spent our budget: stop rather than
        // start another round we can't afford.
        if (
          round >= maxToolRounds ||
          outputTokens >= maxOutputTokens ||
          calls.length > (commandBatch ? MAX_HELPER_ACTIONS : maxToolCalls - toolCalls) ||
          proposedActions > MAX_HELPER_ACTIONS
        ) {
          return { outcome: OUTCOME.EXHAUSTED };
        }
        // Reject oversized batches before executing any of their calls.
        if (!commandBatch) toolCalls += calls.length;

        // A terminal command cannot consume document results from this same
        // response. Reject the incompatible batch before any provider work;
        // do not silently drop either requested operation.
        if (
          calls.some((call) => registry.terminalTools?.has(call.name)) &&
          calls.some(
            (call) => registry.handlers.has(call.name) && !registry.terminalTools?.has(call.name),
          )
        )
          return { outcome: OUTCOME.INCOMPLETE_ACTION, reason: 'mixed_tools' };

        // Replay the assistant turn verbatim (opaque continuation blocks and all)
        // so the signed/encrypted reasoning transcript stays intact.
        history.push({ role: 'assistant', content: res.content_blocks });

        // Execute each call in order; one tool_result per call id, all before any
        // text, as the /chat history contract requires.
        const results = [];
        const actions = [];
        for (const call of calls) {
          if (isExhausted()) return { outcome: OUTCOME.EXHAUSTED };
          const handler = registry.handlers.get(call.name);
          if (!handler) {
            // Unknown or non-exposed tool: refuse, never execute.
            results.push(errorResult(call.id, 'tool is not available'));
            continue;
          }
          try {
            if (!(await withDeadline(checkCaller))) return { outcome: OUTCOME.WITHHELD };
            const out = await handler(call.input, {
              context,
              principal,
              readDocument,
              request,
              withDeadline,
            });
            if (out?.action) {
              actions.push(...(out.action.actions ?? [out.action]));
              if (actions.length > MAX_HELPER_ACTIONS) return { outcome: OUTCOME.EXHAUSTED };
              continue;
            }
            if (typeof out?.revalidate === 'function') checks.push(out.revalidate);
            collect(out?.citations);
            results.push({
              type: 'tool_result',
              tool_use_id: call.id,
              content: out?.content ?? null,
              is_error: out?.is_error === true,
            });
          } catch {
            if (isExhausted()) return { outcome: OUTCOME.EXHAUSTED };
            results.push(errorResult(call.id, 'tool failed'));
          }
        }
        if (actions.length || calls.some((call) => registry.terminalTools?.has(call.name))) {
          // Never silently omit a failed proposal or replay private command data.
          if (
            !actions.length &&
            results.every((result) => result.is_error) &&
            calls.every((call) => registry.terminalTools?.has(call.name))
          ) {
            // Only sanitized preparation errors exist: no private previews or
            // command output. Allow one explanation, never an executable retry.
            history.push({ role: 'user', content: results });
            clarifyPreparation = true;
            continue;
          }
          if (!(await canPublish())) return failedCheck();
          if (!actions.length || results.some((result) => result.is_error))
            return { outcome: OUTCOME.INCOMPLETE_ACTION };
          for (const item of actions) {
            const check = item.check;
            item.check = async (validation = new Map()) => {
              if (!(await checkOnce(validation, canPublish, canPublish)))
                throw new Error('This request expired or access changed. Ask again.');
              return check?.(validation);
            };
          }
          const action = combineHelperActions(actions);
          const setExpiresAt = action.setExpiresAt;
          action.setExpiresAt = (expiresAt) => {
            deadline = performance.now() + Math.max(0, expiresAt - Date.now());
            setExpiresAt?.(expiresAt);
          };
          return { outcome: OUTCOME.ACTION, action, canPublish };
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
