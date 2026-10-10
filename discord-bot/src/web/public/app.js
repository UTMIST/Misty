import { hydrateMentions } from './mentions.js';
import {
  categories,
  flattenCommands,
  matchesSearch,
  fieldLabel,
  humanize,
  collectOptions,
} from './catalog.js';

const state = {
  commands: [],
  people: [],
  peopleMap: new Map(),
  actingAs: localStorage.getItem('actingAs') || '',
  selectedKey: null,
  pending: false,
  openCategories: new Set(['conversation', 'account']),
};
const $ = (id) => document.getElementById(id);
const actingAsInput = $('actingAs');
const commandList = $('command-list');
const transcript = $('transcript');
const formStrip = $('form-strip');
const helper = {
  key: 'helper-reply',
  title: 'Reply to Misty',
  displayName: 'Reply to Misty',
  description: 'Continue a conversation in a simulated Discord thread.',
  category: 'conversation',
  auth: 'linked',
};

async function request(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok)
    throw new Error(payload.error || payload.content || `Request failed (${response.status}).`);
  return payload;
}

async function loadCommands() {
  state.commands = flattenCommands(await request('/api/commands'));
  renderSidebar();
  renderQuickActions();
}

async function loadPeople() {
  state.people = await request('/api/people');
  state.peopleMap = new Map(
    state.people.filter((p) => p.discord_id).map((p) => [p.discord_id, p.display_name]),
  );
  $('people-list').replaceChildren();
  for (const person of state.people.filter((p) => p.discord_id)) {
    const option = document.createElement('option');
    option.value = person.discord_id;
    option.label = person.display_name;
    $('people-list').append(option);
  }
  updateIdentity();
}

async function loadWorkspace() {
  $('retry-btn').disabled = true;
  $('load-error').hidden = true;
  const results = await Promise.allSettled([loadCommands(), loadPeople()]);
  const failures = results.flatMap((result, index) =>
    result.status === 'rejected'
      ? [
          index === 0
            ? 'Commands could not be loaded.'
            : 'The people directory could not be loaded. You can still enter a Discord ID.',
        ]
      : [],
  );
  if (failures.length) {
    $('load-error').querySelector('span').textContent = failures.join(' ');
    $('load-error').hidden = false;
    if (results[0].status === 'rejected')
      commandList.textContent = 'Commands unavailable. Try again above.';
  }
  $('retry-btn').disabled = false;
}

function renderSidebar() {
  const query = $('command-search').value;
  commandList.replaceChildren();
  let count = 0;
  for (const category of categories) {
    const commands = [helper, ...state.commands].filter(
      (cmd) => cmd.category === category.id && matchesSearch(cmd, query),
    );
    if (!commands.length) continue;
    count += commands.length;
    const group = document.createElement('details');
    group.className = 'nav-group';
    group.open = Boolean(query.trim()) || state.openCategories.has(category.id);
    group.addEventListener('toggle', () => {
      if (!query.trim()) {
        if (group.open) state.openCategories.add(category.id);
        else state.openCategories.delete(category.id);
      }
    });
    const heading = document.createElement('summary');
    const symbol = document.createElement('span');
    symbol.className = 'nav-symbol';
    symbol.textContent = category.symbol;
    symbol.setAttribute('aria-hidden', 'true');
    heading.append(symbol, document.createTextNode(category.label));
    group.append(heading);
    for (const command of commands) {
      const button = document.createElement('button');
      button.className = 'nav-button';
      button.textContent = command.title;
      button.title = command.key === helper.key ? command.title : `/${command.displayName}`;
      button.dataset.key = command.key;
      if (state.selectedKey === command.key) {
        button.classList.add('active');
        button.setAttribute('aria-current', 'page');
      }
      button.addEventListener('click', () => selectCommand(command.key));
      group.append(button);
    }
    commandList.append(group);
  }
  if (!count) {
    const empty = document.createElement('p');
    empty.className = 'nav-note';
    empty.textContent = 'No matching commands. Try a name, task, or category.';
    commandList.append(empty);
  }
}

function renderQuickActions() {
  $('quick-actions').replaceChildren();
  const starters = [
    { key: 'whoami', symbol: '◎', detail: 'Your profile and linked accounts' },
    { key: 'my-teams', symbol: '◈', detail: 'See where you belong' },
    { key: 'doc:list', symbol: '▤', detail: 'Explore the document catalog' },
  ];
  for (const starter of starters) {
    const command = state.commands.find((cmd) => cmd.key === starter.key);
    if (!command) continue;
    const button = document.createElement('button');
    button.className = 'quick-card';
    button.innerHTML = `<span class="quick-icon" aria-hidden="true">${starter.symbol}</span><span class="quick-title">${escapeHtml(command.title)}<span aria-hidden="true">↗</span></span><span class="quick-description">${starter.detail}</span>`;
    button.addEventListener('click', () => selectCommand(command.key));
    $('quick-actions').append(button);
  }
}

function updateIdentity() {
  const name = state.peopleMap.get(state.actingAs);
  $('identity-hint').textContent = name
    ? `Using ${name}'s Discord identity.`
    : state.actingAs
      ? 'Using a custom Discord ID. Commands still check this identity’s permissions.'
      : 'Choose an identity to run a command.';
  $('identity-avatar').textContent = name ? initials(name) : state.actingAs ? 'ID' : '?';
  updateSubmitState();
}

function updateSubmitState() {
  for (const button of formStrip.querySelectorAll('button[type="submit"]')) {
    button.dataset.idleLabel ||= button.textContent;
    button.textContent = state.pending ? 'Working…' : button.dataset.idleLabel;
    button.disabled = !state.actingAs || state.pending;
  }
  for (const input of formStrip.querySelectorAll('input, select, textarea')) {
    input.disabled = state.pending;
  }
  actingAsInput.disabled = state.pending;
  $('reset-btn').disabled = state.pending;
  const resetThread = $('reset-thread-btn');
  if (resetThread) resetThread.disabled = state.pending;
  const identityNote = $('form-identity-note');
  if (identityNote)
    identityNote.textContent = state.pending
      ? 'A request is in progress…'
      : !state.actingAs
        ? 'Choose an identity above to continue.'
        : 'Runs as your selected identity.';
}

function setPending(pending, message = '') {
  state.pending = pending;
  $('request-status').textContent = message;
  updateSubmitState();
}

function showHome() {
  state.selectedKey = null;
  formStrip.hidden = true;
  $('welcome').hidden = false;
  $('quick-start').hidden = false;
  $('home-btn').classList.add('active');
  $('home-btn').setAttribute('aria-current', 'page');
  renderSidebar();
}

function selectCommand(key) {
  const command = key === helper.key ? helper : state.commands.find((cmd) => cmd.key === key);
  if (!command) return;
  state.selectedKey = key;
  state.openCategories.add(command.category);
  $('home-btn').classList.remove('active');
  $('home-btn').removeAttribute('aria-current');
  $('welcome').hidden = true;
  $('quick-start').hidden = true;
  formStrip.hidden = false;
  renderSidebar();
  if (key === helper.key) renderHelperReplyForm();
  else renderForm(command);
  updateSubmitState();
  setNavigationOpen(false);
  $('command-title').focus({ preventScroll: true });
  formStrip.scrollIntoView({ block: 'nearest' });
}

function setNavigationOpen(open) {
  document.querySelector('.sidebar').classList.toggle('nav-open', open);
  $('nav-toggle').setAttribute('aria-expanded', String(open));
  if (open) commandList.scrollTop = 0;
}

function renderCommandHeading(command) {
  const authLabels = {
    public: 'Anyone',
    linked: 'Linked account',
    admin: 'Admin only',
    superuser: 'Superuser only',
    dynamic: 'Permission checked',
  };
  const category = categories.find((item) => item.id === command.category)?.label;
  formStrip.innerHTML = `<div class="command-heading"><div><span class="eyebrow">${escapeHtml(category)}${command.key === helper.key ? '' : ` · /${escapeHtml(command.displayName)}`}</span><h1 id="command-title" tabindex="-1">${escapeHtml(command.title)}</h1><p>${escapeHtml(command.description || '')}</p></div><span class="auth-badge">${escapeHtml(authLabels[command.auth] || 'Permission checked')}</span></div>`;
}

function createField({ name, label, description, required, fullWidth = false }, input) {
  const wrapper = document.createElement('div');
  wrapper.className = `form-field${fullWidth ? ' full-width' : ''}`;
  const id = `field-${name}`;
  input.id = id;
  input.name = name;
  input.classList.add('control');
  input.required = Boolean(required);
  const labelElement = document.createElement('label');
  labelElement.className = 'field-label';
  labelElement.htmlFor = id;
  const labelText = document.createElement('span');
  labelText.textContent = label;
  labelElement.append(labelText);
  const requirement = document.createElement('span');
  requirement.className = required ? 'required-mark' : 'field-optional';
  requirement.textContent = required ? 'Required' : 'Optional';
  labelElement.append(requirement);
  wrapper.append(labelElement, input);
  if (description) {
    const hint = document.createElement('span');
    hint.id = `${id}-hint`;
    hint.className = 'field-hint';
    hint.textContent = description;
    input.setAttribute('aria-describedby', hint.id);
    wrapper.append(hint);
  }
  return wrapper;
}

function renderInput(option) {
  let input;
  if (option.choices || option.type === 'boolean') {
    input = document.createElement('select');
    input.append(new Option(option.required ? 'Select an option' : 'Use default', ''));
    const choices = option.choices || [
      { name: 'Yes', value: 'true' },
      { name: 'No', value: 'false' },
    ];
    for (const choice of choices) input.append(new Option(humanize(choice.name), choice.value));
  } else if (option.name === 'description') {
    input = document.createElement('textarea');
    input.rows = 3;
  } else {
    input = document.createElement('input');
    input.type = option.name === 'email' ? 'email' : option.name === 'url' ? 'url' : 'text';
    if (option.type === 'user') {
      input.setAttribute('list', 'people-list');
      input.placeholder = 'Choose a person or enter a Discord ID';
      input.autocomplete = 'off';
    } else if (option.name === 'email') input.placeholder = 'you@example.com';
    else if (option.name === 'url') input.placeholder = 'https://…';
    else if (option.name === 'code') {
      input.inputMode = 'numeric';
      input.autocomplete = 'one-time-code';
      input.placeholder = '6-digit code';
    }
  }
  return input;
}

function createActions(label) {
  const actions = document.createElement('div');
  actions.className = 'form-actions';
  const submit = document.createElement('button');
  submit.type = 'submit';
  submit.className = 'button button-primary';
  submit.textContent = label;
  const hint = document.createElement('span');
  hint.id = 'form-identity-note';
  hint.className = 'field-hint';
  actions.append(submit, hint);
  return actions;
}

function renderForm(command) {
  renderCommandHeading(command);
  const form = document.createElement('form');
  form.className = 'command-form';
  for (const option of command.options)
    form.append(
      createField(
        {
          ...option,
          label: fieldLabel(option),
          fullWidth: option.name === 'description' || option.name === 'url',
        },
        renderInput(option),
      ),
    );
  if (!command.options.length) {
    const note = document.createElement('p');
    note.className = 'form-note';
    note.textContent = 'No extra details needed. This command uses your selected identity.';
    form.append(note);
  }
  form.append(createActions('Run command ↗'));
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submitForm(command, form);
  });
  formStrip.append(form);
}

function renderHelperReplyForm() {
  renderCommandHeading(helper);
  const form = document.createElement('form');
  form.className = 'command-form';
  const input = document.createElement('textarea');
  input.rows = 4;
  input.placeholder = 'What would you like to talk about?';
  form.append(
    createField(
      {
        name: 'content',
        label: 'Your message',
        required: true,
        fullWidth: true,
        description: 'Replies stay in this shared local thread until you reset it.',
      },
      input,
    ),
  );
  const toggle = document.createElement('label');
  toggle.className = 'toggle-field';
  const ping = document.createElement('input');
  ping.type = 'checkbox';
  ping.checked = true;
  ping.id = 'reply-ping';
  const toggleCopy = document.createElement('span');
  toggleCopy.innerHTML =
    '<strong>Ping @Misty</strong><span class="field-hint">Misty answers when this is on. With it off, your message only adds context.</span>';
  toggle.append(ping, toggleCopy);
  form.append(toggle);
  const actions = createActions('Send reply ↗');
  const reset = document.createElement('button');
  reset.type = 'button';
  reset.id = 'reset-thread-btn';
  reset.className = 'button button-secondary';
  reset.textContent = 'Reset thread';
  reset.addEventListener('click', resetHelperThread);
  actions.insertBefore(reset, actions.lastChild);
  form.append(actions);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const content = input.value.trim();
    if (!content || !state.actingAs || state.pending) return;
    const replyPing = ping.checked;
    appendYouText(content, replyPing);
    setPending(true, replyPing ? 'Misty is thinking…' : 'Adding your message…');
    try {
      const payload = await request('/api/helper/reply', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ content, actingAs: state.actingAs, replyPing }),
      });
      input.value = '';
      if (payload.triggered !== false) appendBotMessage(payload);
    } catch (error) {
      appendErrorMessage(error.message);
    } finally {
      setPending(false);
    }
  });
  formStrip.append(form);
}

async function resetHelperThread() {
  if (state.pending) return;
  setPending(true, 'Resetting the thread…');
  try {
    await request('/api/helper/thread', { method: 'DELETE' });
    appendBotMessage({
      content: 'Your local thread has been reset. Your next message starts a new conversation.',
    });
  } catch (error) {
    appendErrorMessage(error.message);
  } finally {
    setPending(false);
  }
}

async function submitForm(command, form) {
  if (!state.actingAs || state.pending) return;
  const options = collectOptions(new FormData(form).entries(), command.options);
  appendYouMessage(command, options);
  setPending(true, `Running /${command.displayName}…`);
  try {
    const payload = await request(`/api/commands/${command.parentName}/run`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ options, subcommand: command.subName, actingAs: state.actingAs }),
    });
    appendBotMessage(payload);
  } catch (error) {
    appendErrorMessage(error.message);
  } finally {
    setPending(false);
  }
}

function appendYouMessage(command, options) {
  // Codes are ephemeral credentials, not useful command-history metadata.
  const summary = Object.entries(options)
    .map(([key, value]) => `${key}:${key === 'code' ? '••••••' : value}`)
    .join(' ');
  const element = userMessage();
  element.querySelector('.body').textContent = `/${command.displayName} ${summary}`.trim();
  appendMessage(element);
}

function appendYouText(content, replyPing) {
  const element = userMessage();
  const context = document.createElement('div');
  context.className = 'reply-context';
  context.textContent = `Reply to Misty · ping ${replyPing ? 'on' : 'off'}`;
  element.querySelector('.body').append(context, document.createTextNode(content));
  appendMessage(element);
}

function userMessage() {
  const name = state.peopleMap.get(state.actingAs) || 'You';
  return messageElement({ author: name, avatar: initials(name), klass: 'you' });
}

function appendBotMessage(payload) {
  const element = messageElement({ author: 'Misty', klass: 'bot' });
  const body = element.querySelector('.body');
  const parts = [];
  if (payload.content !== undefined)
    parts.push(`<div>${hydrateMentions(payload.content, state.peopleMap)}</div>`);
  for (const embed of payload.embeds || []) {
    let html = '<div class="embed">';
    if (embed.title) html += `<h4>${escapeHtml(embed.title)}</h4>`;
    if (embed.description)
      html += `<div>${hydrateMentions(embed.description, state.peopleMap)}</div>`;
    for (const field of embed.fields || [])
      html += `<div class="field"><span class="field-name">${escapeHtml(field.name)}</span><br>${hydrateMentions(field.value, state.peopleMap)}</div>`;
    parts.push(`${html}</div>`);
  }
  body.innerHTML = parts.join('') || '<em>No content returned.</em>';
  if (typeof payload.ephemeral === 'boolean') {
    const visibility = document.createElement('span');
    visibility.className = 'message-visibility';
    visibility.textContent = payload.ephemeral
      ? 'Private reply in Discord'
      : 'Shared reply in Discord';
    element.querySelector('.header').append(visibility);
  }
  appendMessage(element);
}

function appendErrorMessage(message) {
  const element = messageElement({ author: 'Something went wrong', avatar: '!', klass: 'error' });
  element.querySelector('.body').textContent = message;
  appendMessage(element);
}

function messageElement({ author, avatar = '', klass }) {
  const element = document.createElement('div');
  element.className = `message ${klass}`;
  const time = new Date();
  element.innerHTML = `<div class="avatar" aria-hidden="true">${klass === 'bot' ? '<img src="/utmist-logo.png" alt="">' : escapeHtml(avatar)}</div><div class="content"><div class="header"><span class="author">${escapeHtml(author)}</span><time class="time" datetime="${time.toISOString()}">${time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time></div><div class="body"></div></div>`;
  return element;
}

function appendMessage(element) {
  transcript.append(element);
  $('activity-empty').hidden = true;
  $('activity-count').textContent = transcript.childElementCount;
  $('clear-btn').disabled = false;
  transcript.scrollTop = transcript.scrollHeight;
}

function initials(name) {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part.charAt(0))
    .join('')
    .toUpperCase();
}
function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char],
  );
}

actingAsInput.value = state.actingAs;
actingAsInput.addEventListener('input', () => {
  const value = actingAsInput.value.trim();
  const matches = state.people.filter(
    (person) => person.discord_id && (person.discord_id === value || person.display_name === value),
  );
  state.actingAs = matches.length === 1 ? matches[0].discord_id : value;
  localStorage.setItem('actingAs', state.actingAs);
  updateIdentity();
});
$('command-search').addEventListener('input', () => {
  renderSidebar();
  setNavigationOpen(Boolean($('command-search').value.trim()));
});
$('nav-toggle').addEventListener('click', () => {
  setNavigationOpen($('nav-toggle').getAttribute('aria-expanded') !== 'true');
});
$('home-btn').addEventListener('click', showHome);
$('retry-btn').addEventListener('click', loadWorkspace);
$('clear-btn').addEventListener('click', () => {
  transcript.replaceChildren();
  $('activity-empty').hidden = false;
  $('activity-count').textContent = '0';
  $('clear-btn').disabled = true;
});
$('clear-btn').title =
  'Clear the visible activity. Use Reset thread to clear Misty’s conversation context.';
$('reset-btn').addEventListener('click', async () => {
  if (
    state.pending ||
    !confirm(
      'Reset the scratch database from your main dev database? Scratch changes and Misty’s thread will be cleared.',
    )
  )
    return;
  setPending(true, 'Resetting the scratch database…');
  try {
    await request('/api/reset', { method: 'POST' });
    await loadPeople();
    appendBotMessage({ content: 'Scratch database reset. The people picker is up to date.' });
  } catch (error) {
    appendErrorMessage(error.message);
  } finally {
    setPending(false);
  }
});
updateIdentity();
loadWorkspace();
