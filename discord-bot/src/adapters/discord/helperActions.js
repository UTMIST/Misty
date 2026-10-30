import { MessageFlags } from 'discord.js';
import { safeReply } from './interactions.js';
import { chunkForDiscord, sendDiscordMessage } from './messages.js';
import { PendingActionsFull } from '../../helperActions.js';

const ACTION_FAILED =
  'I could not complete or deliver this action. I have not retried it. Check the current state before requesting it again.';

function buttons(token, confirm, needsConfirmation = false) {
  const choices = confirm
    ? [
        ['confirm', 'Confirm', 3],
        ['cancel', 'Cancel', 2],
      ]
    : [['review', needsConfirmation ? 'Review privately' : 'View privately', 2]];
  return [
    {
      type: 1,
      components: choices.map(([verb, label, style]) => ({
        type: 2,
        style,
        label,
        custom_id: `helper:${verb}:${token}`,
      })),
    },
  ];
}

function privatePayload(payload) {
  return { ...payload, ephemeral: true, allowedMentions: { parse: [] } };
}

async function preview(action, token, send) {
  await action.check();
  action.reviewPages ??= { pages: chunkForDiscord(action.summary), shown: new Set() };
  if (action.reviewPages.pages.length > 1) return previewPage(action, token, 0, send);
  // Send the complete preview before the buttons, even when it spans messages.
  await send({
    content: `Review your request:\n\n${action.summary}`,
    allowedMentions: { parse: [] },
  });
  await send({
    content:
      'Nothing has changed yet. Only you can confirm. This request expires two minutes after this confirmation is first shown.',
    components: buttons(token, true),
    allowedMentions: { parse: [] },
  });
  action.reviewComplete = true;
}

async function previewPage(action, token, index, send) {
  const state = action.reviewPages;
  if (!state || !Number.isSafeInteger(index) || index < 0 || index >= state.pages.length)
    throw new Error('Invalid review page');
  await action.check();
  const complete = state.pages.every((_, i) => i === index || state.shown.has(i));
  const navigation = [];
  for (const [label, page] of [
    ['Previous', index - 1],
    ['Next', index + 1],
  ]) {
    if (page >= 0 && page < state.pages.length)
      navigation.push({ type: 2, style: 2, label, custom_id: `helper:page:${token}:${page}` });
  }
  const controls = buttons(token, true)[0].components.filter(
    (button) => complete || button.label !== 'Confirm',
  );
  await send({
    content: `Review your request — page ${index + 1} of ${state.pages.length}.\n${complete ? 'Confirm approves the entire request, across all pages.' : 'Read every page to unlock confirmation of the entire request.'}\nNothing has changed yet. This request expires two minutes after the first private review.`,
    embeds: [{ description: state.pages[index] }],
    components: [{ type: 1, components: [...navigation, ...controls] }],
    allowedMentions: { parse: [] },
  });
  state.shown.add(index);
  action.reviewComplete = state.pages.every((_, i) => state.shown.has(i));
}

async function deliverResult(payload, sendPrivate, sendPublic, defaultCheck) {
  let privateDelivered = false;
  for (const reply of payload?.replies ?? [payload]) {
    const { publicReply, keepPrivate, check = defaultCheck, safeFailure, ...privateReply } = reply;
    if (safeFailure) {
      // Only the batch's generic failure receipt may bypass a failed permission
      // check, just like ACTION_FAILED; it contains no command data or labels.
      await sendPrivate(privateReply, async () => {});
      privateDelivered = true;
    } else {
      if (!publicReply || !sendPublic || keepPrivate) {
        await sendPrivate(privateReply, check);
        privateDelivered = true;
      }
      if (publicReply && sendPublic) await sendPublic(publicReply, check);
    }
  }
  return privateDelivered;
}

/** Deliver command output directly; private data never goes through the model. */
export async function presentHelperAction(message, send, action, appContext) {
  if (!message || action.userId !== message.author.id) return;
  let token;
  let executionStarted = false;
  try {
    // Admission must precede checks that may read documents, delivery and work.
    token = appContext.helperActions.add(action);
    await action.check();
    // Retain the trusted destination for later button clicks and mixed batches.
    // Only an explicit public projection may leave the private result path.
    if (message.guild || message.guildId) {
      action.sendPublic = ({ payload, send: destinationSend = send }, check = action.check) =>
        sendDiscordMessage(async (part) => {
          const validation = new Map();
          await check(validation);
          const { ephemeral: _ephemeral, ...body } = part;
          return destinationSend({ ...body, allowedMentions: { parse: [] } }, validation);
        }, payload);
      if (action.publicRead && !action.confirm) {
        if (!appContext.helperActions.take(token, action.userId)) throw new Error('Expired');
        executionStarted = true;
        await deliverResult(
          await action.execute(),
          async () => {
            throw new Error('Public result is no longer available');
          },
          action.sendPublic,
          action.check,
        );
        return;
      }
    }
    let dm;
    if (!message.guild && !message.guildId) {
      try {
        dm = await message.author.createDM();
      } catch {
        /* Offer a private button below. */
      }
    }
    if (dm) {
      const sendPrivate = (payload, check = action.check) =>
        sendDiscordMessage(async (part) => {
          await check();
          const { ephemeral: _ephemeral, ...body } = privatePayload(part);
          return dm.send(body);
        }, payload);
      try {
        if (action.confirm) {
          await preview(action, token, sendPrivate);
          appContext.helperActions.presented(token, action.userId);
        } else {
          // createDM succeeds even with closed DMs. Test actual delivery before
          // running a read/Q&A; never rerun an action after a later send fails.
          await sendPrivate({ content: 'Preparing your private result…' });
          if (!appContext.helperActions.take(token, action.userId)) throw new Error('Expired');
          executionStarted = true;
          await deliverResult(await action.execute(), sendPrivate, null, action.check);
        }
        // A failed source-channel acknowledgement must not invalidate buttons
        // already delivered privately, or cause a successful read to run again.
        await send({
          content: action.confirm
            ? 'I sent you a private confirmation. Nothing has changed yet.'
            : 'I sent the result to you privately.',
          allowedMentions: { parse: [] },
        }).catch(() => {});
        return;
      } catch (error) {
        if (executionStarted) throw error;
        /* No private payload is ever included in the public fallback. */
      }
    }
    await send({
      content: action.confirm
        ? 'Review and confirm your request privately here. Only you can open it; nothing has changed yet.'
        : 'Open your private response here. Only you can view it.',
      components: buttons(token, false, action.confirm),
      allowedMentions: { parse: [] },
    });
    if (!action.confirm) appContext.helperActions.presented(token, action.userId);
  } catch (error) {
    if (token) appContext.helperActions.take(token, action.userId);
    await send({
      content:
        error instanceof PendingActionsFull
          ? error.message
          : executionStarted
            ? ACTION_FAILED
            : 'This action is unavailable or expired. Check your account permissions and try again shortly.',
      allowedMentions: { parse: [] },
    });
  }
}

export async function handleHelperActionInteraction(interaction, appContext) {
  const match = /^helper:(review|confirm|cancel|page):([A-Za-z0-9_-]{24})(?::([0-9]{1,3}))?$/.exec(
    interaction.customId ?? '',
  );
  if (!match) return;
  const [, verb, token, page] = match;
  const userId = interaction.user.id;
  const action = appContext.helperActions?.get(token, userId);
  if (verb === 'page' && action?.confirm) {
    await interaction.deferUpdate();
    try {
      await previewPage(action, token, Number(page), async (payload) => {
        await action.check();
        if (appContext.helperActions.get(token, userId) !== action) throw new Error('Expired');
        await interaction.editReply(payload);
      });
    } catch {
      await interaction.followUp({
        content: 'This request expired or your permissions changed. Ask again.',
        flags: MessageFlags.Ephemeral,
        allowedMentions: { parse: [] },
      });
    }
    return;
  }
  await interaction.deferReply({ flags: MessageFlags.Ephemeral });
  const reply = (payload) => safeReply(interaction, privatePayload(payload));
  if (!action)
    return reply({ content: 'This request is expired, already used, or belongs to another user.' });
  if (verb === 'page') return reply({ content: 'This request has no review pages.' });
  const sendPrivate = async (payload, check = action.check) => {
    const delivered = await safeReply(interaction, privatePayload(payload), {
      beforeSend: check,
    });
    if (!delivered) throw new Error('Private delivery failed');
  };
  if (verb === 'cancel') {
    appContext.helperActions.take(token, userId);
    return reply({ content: 'Cancelled. Nothing was changed.' });
  }
  if (verb === 'review' && action.confirm) {
    try {
      await preview(action, token, sendPrivate);
      appContext.helperActions.presented(token, userId);
    } catch {
      await reply({ content: 'This request expired or your permissions changed. Ask again.' });
    }
    return;
  }
  if (action.confirm && !action.reviewComplete)
    return reply({
      content:
        'Open the private review and read every page before confirming. Nothing was changed.',
    });
  const validation = new Map();
  try {
    await action.check(validation);
  } catch {
    appContext.helperActions.take(token, userId);
    return reply({ content: 'This request expired or your permissions changed. Ask again.' });
  }
  // Single use, including failures. Concurrent clicks cannot execute twice.
  if (!appContext.helperActions.take(token, userId))
    return reply({ content: 'This request is expired or already used.' });
  try {
    const payload = await action.execute(validation);
    const privateDelivered = await deliverResult(
      payload,
      sendPrivate,
      action.sendPublic,
      action.check,
    );
    // Discord deferred an ephemeral reply even when every result was public.
    if (!privateDelivered && action.sendPublic)
      await reply({ content: 'Posted the result in the channel.' });
  } catch {
    await reply({
      content: ACTION_FAILED,
    });
  }
}
