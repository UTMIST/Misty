function text(value) {
  return String(value)
    .replaceAll('\r', '\\r')
    .replaceAll('\n', '\\n')
    .replace(/([\\`*_{}[\]()<>#|~])/g, '\\$1')
    .replaceAll('@', '@\u200b');
}

/** Shared recording updates omit requester and voice-channel details. */
export function renderRecordingAnnouncement(subcommand, elapsedMs = 0) {
  switch (subcommand) {
    case 'start':
      return '🔴 Misty started recording this meeting.';
    case 'status':
      return `🔴 Misty is recording this meeting (${Math.round(elapsedMs / 1000)}s elapsed).`;
    case 'stop':
      return '⏳ Misty stopped recording this meeting. Processing — minutes will post here shortly.';
    default:
      return null;
  }
}

/** Render trusted labels and the exact bound arguments for private review. */
export function renderActionSummary({
  commandName,
  subcommand,
  operation,
  options,
  request,
  people,
  team,
}) {
  const member = options.user && people[options.user.id];
  const userLabel = options.user
    ? `${member?.display_name ? `**${text(member.display_name)}** ` : ''}(<@${options.user.id}>)`
    : null;
  const teamSlug = options.team ?? (commandName === 'team' ? options.slug : null);
  const teamLabel = teamSlug
    ? `${team?.label ? `**${text(team.label)}** ` : ''}(${text(teamSlug)})`
    : null;
  const membership = commandName === 'team' && ['add', 'remove'].includes(subcommand);
  const heading = membership
    ? `${subcommand === 'remove' ? 'Remove' : 'Add'} ${userLabel} ${subcommand === 'remove' ? 'from' : 'to'} ${teamLabel}.`
    : `**${text(operation.description.replace(/ \(admin\)$/, ''))}**`;
  const args = Object.entries(options).flatMap(([key, value]) => {
    if (membership && ['user', 'team'].includes(key)) return [];
    const label = text(key.replaceAll('_', ' '));
    const definition = operation.options.find((option) => option.name === key);
    const rendered =
      definition?.type === 'user'
        ? `${people[value.id]?.display_name ? `${text(people[value.id].display_name)} ` : ''}(<@${value.id}>)`
        : key === 'team' || (commandName === 'team' && key === 'slug')
          ? teamLabel
          : text(JSON.stringify(value));
    return [`${label}: ${rendered}`];
  });
  return [
    heading,
    ...args,
    request.discordGuildId
      ? `Requested in: <#${request.sourceChannelId ?? request.discordChannelId}> · ${text(request.guildLabel ?? request.discordGuildId)}`
      : 'Requested in this direct message.',
    request.channelUsedParent && ['channel-access', 'helper-limits'].includes(commandName)
      ? `Applies to parent channel: <#${request.discordChannelId}>`
      : null,
    commandName === 'record' && subcommand === 'start'
      ? `Recording location: your voice channel at confirmation. Minutes channel: <#${request.sourceChannelId}>`
      : null,
    commandName === 'record' && subcommand === 'start'
      ? `Save transcript and minutes as a Google Doc: ${(options.save ?? true) ? 'yes' : 'no'}`
      : null,
    commandName === 'record' && subcommand === 'stop'
      ? `Recording voice channel: ${request.recordingVoiceChannelId ? `<#${request.recordingVoiceChannelId}>` : 'no active recording'}. Applies only to the recording active when you asked.`
      : null,
  ]
    .filter(Boolean)
    .join('\n');
}
