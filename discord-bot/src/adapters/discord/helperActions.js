import { MessageFlags } from 'discord.js';
import { safeReply } from './interactions.js';
import { sendDiscordMessage } from './messages.js';
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
}

async function deliverResult(payload, sendPrivate, sendPublic) {
  for (const reply of payload?.replies ?? [payload]) {
    const { publicReply, ...privateReply } = reply;
    if (publicReply && sendPublic) await sendPublic(publicReply);
    else await sendPrivate(privateReply);
  }
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
      action.sendPublic = ({ payload, send: destinationSend = send }) =>
        sendDiscordMessage(async (part) => {
          await action.check();
          const { ephemeral: _ephemeral, ...body } = part;
          return destinationSend({ ...body, allowedMentions: { parse: [] } });
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
      const sendPrivate = (payload) =>
        sendDiscordMessage(async (part) => {
          await action.check();
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
          await deliverResult(await action.execute(), sendPrivate);
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
  const match = /^helper:(review|confirm|cancel):([A-Za-z0-9_-]{24})$/.exec(
    interaction.customId ?? '',
  );
  if (!match) return;
  await interaction.deferReply({ flags: MessageFlags.Ephemeral });
  const reply = (payload) => safeReply(interaction, privatePayload(payload));
  const [, verb, token] = match;
  const userId = interaction.user.id;
  const action = appContext.helperActions?.get(token, userId);
  if (!action)
    return reply({ content: 'This request is expired, already used, or belongs to another user.' });
  const sendPrivate = async (payload) => {
    const delivered = await safeReply(interaction, privatePayload(payload), {
      beforeSend: action.check,
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
  try {
    await action.check();
  } catch {
    appContext.helperActions.take(token, userId);
    return reply({ content: 'This request expired or your permissions changed. Ask again.' });
  }
  // Single use, including failures. Concurrent clicks cannot execute twice.
  if (!appContext.helperActions.take(token, userId))
    return reply({ content: 'This request is expired or already used.' });
  try {
    const payload = await action.execute();
    await action.check();
    await deliverResult(payload, sendPrivate, action.sendPublic);
    // Discord deferred an ephemeral reply even when every result was public.
    if ((payload?.replies ?? [payload]).every((result) => result.publicReply) && action.sendPublic)
      await sendPrivate({ content: 'Posted the result in the channel.' });
  } catch {
    await reply({
      content: ACTION_FAILED,
    });
  }
}
