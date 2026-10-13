// Presentation only: the server's command registry still owns options and auth.
export const categories = [
  { id: 'conversation', label: 'Conversations', symbol: '✦' },
  { id: 'account', label: 'Your account', symbol: '◎' },
  { id: 'teams', label: 'Teams & people', symbol: '◈' },
  { id: 'documents', label: 'Documents', symbol: '▤' },
  { id: 'meetings', label: 'Meetings', symbol: '◷' },
  { id: 'admin', label: 'Administration', symbol: '⚙' },
  { id: 'support', label: 'Help & support', symbol: '?' },
  { id: 'other', label: 'Other commands', symbol: '⋯' },
];

const titles = {
  whoami: 'Your profile',
  link: 'Link your account',
  'verify-code': 'Confirm your account',
  'add-email': 'Add an email',
  'verify-email': 'Confirm your email',
  'my-teams': 'Your teams',
  'team:list': 'Browse teams',
  'team:roster': 'View a roster',
  'team:create': 'Create a team',
  'team:rename': 'Rename a team',
  'team:add': 'Add a teammate',
  'team:remove': 'Remove a teammate',
  'doc:list': 'Browse documents',
  'doc:show': 'View a document',
  'doc:add': 'Add a document',
  'doc:remove': 'Remove a document',
  'record:start': 'Start a recording',
  'record:status': 'Recording status',
  'record:stop': 'Stop a recording',
  seed: 'Add a directory member',
  help: 'Command guide',
  bug: 'Report a bug',
};

export function humanize(value) {
  const text = value.replace(/[_:-]/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function categoryFor(name, auth) {
  if (name === 'team' || name === 'my-teams') return 'teams';
  if (name === 'doc') return 'documents';
  if (name === 'record') return 'meetings';
  if (['link', 'verify-code', 'add-email', 'verify-email', 'whoami'].includes(name)) {
    return 'account';
  }
  if (name === 'help' || name === 'bug') return 'support';
  if (name === 'seed' || auth === 'admin' || auth === 'superuser') return 'admin';
  return 'other';
}

export function flattenCommands(commands) {
  return commands.flatMap((command) => {
    const entries = command.subcommands.length ? command.subcommands : [command];
    return entries.map((entry) => {
      const subName = entry === command ? null : entry.name;
      const key = subName ? `${command.name}:${subName}` : command.name;
      const auth = entry.auth ?? command.auth;
      return {
        key,
        parentName: command.name,
        subName,
        displayName: subName ? `${command.name} ${subName}` : command.name,
        title: titles[key] || humanize(key),
        category: categoryFor(command.name, auth),
        description: entry.description,
        options: entry.options,
        auth,
      };
    });
  });
}

export function matchesSearch(command, query) {
  const category = categories.find((item) => item.id === command.category)?.label || '';
  const haystack = `${command.title} ${command.displayName} ${command.description} ${category}`;
  return query
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .every((word) => haystack.toLowerCase().includes(word));
}

export function fieldLabel(option) {
  const labels = {
    email: 'Email address',
    code: 'Verification code',
    url: 'Document URL',
    id: 'Document ID',
    user: 'Discord member',
    slug: 'Team handle',
    team: 'Team handle',
    label: 'Display name',
    new_label: 'New display name',
    level: 'Access level',
    active_only: 'Active teams only',
    team_admin: 'Team administrator',
  };
  return labels[option.name] || humanize(option.name);
}

export function collectOptions(entries, definitions) {
  const values = new Map(entries);
  const options = {};
  for (const definition of definitions) {
    const value = values.get(definition.name);
    // Keep explicit false/zero. An omitted optional field leaves server defaults intact.
    if (value === undefined || (value === '' && !definition.required)) continue;
    options[definition.name] = value;
  }
  return options;
}
