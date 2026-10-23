function safeTitle(title) {
  return (title || 'Meeting')
    .replace(/[\r\n]+/g, ' ')
    .replace(/([\\`*_{}[\]()#+.!|>~<>])/g, '\\$1')
    .slice(0, 400);
}

/** A pointer only: never copy summary, decisions, participants or transcript. */
export function meetingLinkMessage(record, { editing = false } = {}) {
  const requester = record.requester_discord_id;
  const time = Date.parse(record.started_at);
  const started = Number.isFinite(time) ? ` · <t:${Math.floor(time / 1000)}:f>` : '';
  const adminOnly = !record.audience_team_ids?.length;
  return {
    content:
      `${requester ? `<@${requester}> ` : ''}📄 **${safeTitle(record.title)}**${started}\n` +
      `Saved Google Doc with minutes and the full transcript: <${record.doc_url}>\n` +
      (adminOnly ? 'This record is admin-only. ' : '') +
      'You need your own Drive access; this link does not grant access.\n' +
      `Meeting ID: \`${record.meeting_id}\``,
    allowedMentions: { parse: [], users: requester && !editing ? [requester] : [] },
  };
}

/** Describe persistence and delivery separately, without claiming a partial save succeeded. */
export function meetingRecordNotice(record, meetingId, delivery) {
  const reference = ` Meeting ID: ${meetingId}.`;
  const status = delivery?.persistenceStatus ?? record.status;
  if (delivery?.status === 'posted') return null;
  if (delivery?.status === 'disabled' || ['removing', 'removed', 'inaccessible'].includes(status)) {
    return (
      '⚠️ The Google Doc record is unavailable; keep the PDF and contact a Misty admin.' + reference
    );
  }
  if (status === 'saved') {
    return (
      '⚠️ The Google Doc is saved, but its link could not be delivered. Misty will retry delivery automatically from the saved meeting reference.' +
      reference
    );
  }
  if (status === 'failed') {
    return (
      '⚠️ The Google Doc save failed. Keep the PDF and contact a Misty admin for recovery.' +
      reference
    );
  }
  if (status === 'handoff_pending') {
    return (
      '⚠️ The Google Doc save is delayed. Misty will retry the hand-off for up to 30 minutes; keep the PDF and contact a Misty admin if it remains unavailable.' +
      reference
    );
  }
  return (
    'The Google Doc save is still processing. Its link will post here after saving succeeds; keep the PDF.' +
    reference
  );
}
