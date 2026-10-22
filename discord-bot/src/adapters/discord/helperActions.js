import { MessageFlags } from 'discord.js';
import { safeReply } from './interactions.js';
import { sendDiscordMessage } from './messages.js';

function buttons(token, confirm) {
  const choices = confirm
    ? [
        ['confirm', 'Confirm', 3],
        ['cancel', 'Cancel', 2],
      ]
    : [['review', 'View privately', 2]];
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
    content: `Review this action:\n\`\`\`\n${action.summary}\n\`\`\``,
    allowedMentions: { parse: [] },
  });
  await send({
    content:
      'Nothing has changed yet. Only you can confirm. This request expires within two minutes.',
    components: buttons(token, true),
    allowedMentions: { parse: [] },
  });
}

/** Deliver command output directly; private data never goes through the model. */
export async function presentHelperAction(message, send, action, appContext) {
  if (!message || action.userId !== message.author.id) return;
  let token;
  try {
    await action.check();
    if (action.confirm) token = appContext.helperActions.add(action);
    let dm;
    try {
      dm = await message.author.createDM();
    } catch {
      /* Offer a private button below. */
    }
    if (dm) {
      const sendPrivate = (payload) =>
        sendDiscordMessage(async (part) => {
          await action.check();
          const { ephemeral: _ephemeral, ...body } = privatePayload(part);
          return dm.send(body);
        }, payload);
      try {
        if (action.confirm) await preview(action, token, sendPrivate);
        else await sendPrivate(await action.execute());
        // A failed source-channel acknowledgement must not invalidate buttons
        // already delivered privately, or cause a successful read to run again.
        await send({
          content: action.confirm
            ? 'I sent you a private confirmation. Nothing has changed yet.'
            : 'I sent the result to you privately.',
          allowedMentions: { parse: [] },
        }).catch(() => {});
        return;
      } catch {
        /* No private payload is ever included in the public fallback. */
      }
    }
    token ??= appContext.helperActions.add(action);
    await send({
      content: 'Open this private response to continue.',
      components: buttons(token, false),
      allowedMentions: { parse: [] },
    });
  } catch {
    if (token) appContext.helperActions.take(token, action.userId);
    await send({
      content:
        'This action is unavailable or expired. Check your account permissions and try again shortly.',
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
    } catch {
      await reply({ content: 'This request expired or your permissions changed. Ask again.' });
    }
    return;
  }
  // Single use, including failures. Concurrent clicks cannot execute twice.
  if (!appContext.helperActions.take(token, userId)) return;
  try {
    await action.check();
    const payload = await action.execute();
    await action.check();
    await sendPrivate(payload);
  } catch {
    await reply({
      content:
        'I could not complete or deliver this action. I have not retried it. Check the current state before requesting it again.',
    });
  }
}
