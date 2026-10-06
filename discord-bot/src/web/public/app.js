import { hydrateMentions } from '/mentions.js';

// --- State ---
const state = {
  commands: [], // list from /api/commands (with subcommands flattened for the sidebar)
  people: [], // list from /api/people
  peopleMap: new Map(), // discord_id -> display_name for mention hydration
  actingAs: localStorage.getItem('actingAs') || '',
  selectedKey: null, // "link" or "team:list" for subcommands
};

// --- DOM refs ---
const actingAsInput = document.getElementById('actingAs');
const peopleDatalist = document.getElementById('people-list');
const resetBtn = document.getElementById('reset-btn');
const commandList = document.getElementById('command-list');
const helperReplyBtn = document.getElementById('helper-reply-btn');
const transcript = document.getElementById('transcript');
const formStrip = document.getElementById('form-strip');

// --- Init ---
async function main() {
  await Promise.all([loadCommands(), loadPeople()]);
  renderSidebar();
  initTopStrip();
  refreshPeoplePicker();
  wireHelperReplyButton();
  wireResetButton();
}

async function loadCommands() {
  const res = await fetch('/api/commands');
  const cmds = await res.json();
  // Flatten subcommands: each subcommand becomes its own entry keyed by parent:sub.
  const flat = [];
  for (const c of cmds) {
    if (c.subcommands.length) {
      for (const sub of c.subcommands) {
        flat.push({
          key: `${c.name}:${sub.name}`,
          parentName: c.name,
          subName: sub.name,
          displayName: `${c.name} ${sub.name}`,
          description: sub.description,
          options: sub.options,
        });
      }
    } else {
      flat.push({
        key: c.name,
        parentName: c.name,
        subName: null,
        displayName: c.name,
        description: c.description,
        options: c.options,
      });
    }
  }
  state.commands = flat;
}

async function loadPeople() {
  const res = await fetch('/api/people');
  const people = await res.json();
  state.people = people;
  state.peopleMap = new Map(
    people.filter((p) => p.discord_id !== null).map((p) => [p.discord_id, p.display_name]),
  );
}

// --- Rendering ---
function renderSidebar() {
  commandList.innerHTML = '';
  for (const cmd of state.commands) {
    const btn = document.createElement('button');
    btn.textContent = `/${cmd.displayName}`;
    btn.dataset.key = cmd.key;
    btn.addEventListener('click', () => selectCommand(cmd.key));
    commandList.appendChild(btn);
  }
}

function initTopStrip() {
  // One-time initialization: set initial value and bind the input listener.
  actingAsInput.value = state.actingAs;
  actingAsInput.addEventListener('input', () => {
    // If the value matches a display_name in datalist, resolve to the ID.
    const person = state.people.find(
      (p) => p.discord_id === actingAsInput.value || p.display_name === actingAsInput.value,
    );
    state.actingAs = person && person.discord_id ? person.discord_id : actingAsInput.value;
    localStorage.setItem('actingAs', state.actingAs);
    // Refresh any visible Run button.
    const btn = formStrip.querySelector('button[type="submit"]');
    if (btn) btn.disabled = !state.actingAs;
  });
}

function refreshPeoplePicker() {
  // Update input value and rebuild people datalist options.
  actingAsInput.value = state.actingAs;
  peopleDatalist.innerHTML = '';
  for (const p of state.people) {
    if (p.discord_id !== null) {
      const opt = document.createElement('option');
      opt.value = p.discord_id;
      opt.label = p.display_name;
      peopleDatalist.appendChild(opt);
    }
  }
}

function wireHelperReplyButton() {
  helperReplyBtn.addEventListener('click', () => {
    state.selectedKey = 'helper-reply';
    for (const b of commandList.querySelectorAll('button')) b.classList.remove('active');
    helperReplyBtn.classList.add('active');
    renderHelperReplyForm();
  });
}

function wireResetButton() {
  resetBtn.addEventListener('click', async () => {
    if (!confirm('Drop the scratch DB and re-clone from your main dev DB?')) return;
    resetBtn.disabled = true;
    resetBtn.textContent = 'Resetting…';
    try {
      const res = await fetch('/api/reset', { method: 'POST' });
      if (!res.ok) throw new Error(`reset failed: HTTP ${res.status}`);
      // Re-fetch people so the picker/mentions reflect the new scratch state.
      await loadPeople();
      refreshPeoplePicker();
      appendBotMessage({ content: '🔄 Scratch DB reset from main dev DB.', ephemeral: true });
    } catch (e) {
      appendErrorMessage(e.message);
    } finally {
      resetBtn.disabled = false;
      resetBtn.textContent = 'Reset DB';
    }
  });
}

function selectCommand(key) {
  state.selectedKey = key;
  const cmd = state.commands.find((c) => c.key === key);
  for (const b of commandList.querySelectorAll('button')) {
    b.classList.toggle('active', b.dataset.key === key);
  }
  helperReplyBtn.classList.remove('active');
  renderForm(cmd);
}

function renderForm(cmd) {
  formStrip.innerHTML = `<h3>/${cmd.displayName} — <small>${escapeHtml(cmd.description || '')}</small></h3>`;
  const form = document.createElement('form');
  for (const o of cmd.options) {
    const wrapper = document.createElement('div');
    const label = document.createElement('label');
    label.className = 'field-label';
    label.textContent = `${o.name}${o.required ? ' *' : ''} — ${o.description || ''}`;
    wrapper.appendChild(label);
    wrapper.appendChild(renderInput(o));
    form.appendChild(wrapper);
  }
  const button = document.createElement('button');
  button.type = 'submit';
  button.textContent = 'Run';
  button.disabled = !state.actingAs;
  form.appendChild(button);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    submitForm(cmd, form);
  });
  formStrip.appendChild(form);
}

function renderHelperReplyForm() {
  formStrip.innerHTML = `
    <h3>↪ Reply to Misty</h3>
    <p class="form-note">Simulates a Discord reply inside a Misty-created thread. Misty responds only when the reply ping is on; every message remains in the simulated thread context.</p>
  `;
  const form = document.createElement('form');
  const input = document.createElement('textarea');
  input.name = 'content';
  input.required = true;
  input.placeholder = 'Ask a follow-up…';
  input.rows = 3;
  form.appendChild(input);

  const pingLabel = document.createElement('label');
  pingLabel.className = 'reply-ping-toggle';
  const ping = document.createElement('input');
  ping.type = 'checkbox';
  ping.checked = true;
  pingLabel.append(ping, document.createTextNode(' Ping @Misty (reply ping ON)'));
  form.appendChild(pingLabel);

  const send = document.createElement('button');
  send.type = 'submit';
  send.textContent = 'Reply';
  send.disabled = !state.actingAs;
  form.appendChild(send);

  const reset = document.createElement('button');
  reset.type = 'button';
  reset.textContent = 'Reset thread';
  reset.addEventListener('click', resetHelperThread);
  form.appendChild(reset);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const content = input.value.trim();
    if (!content) return;
    const replyPing = ping.checked;
    appendYouText(content, true, replyPing);
    input.value = '';
    send.disabled = true;
    try {
      const res = await fetch('/api/helper/reply', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ content, actingAs: state.actingAs, replyPing }),
      });
      const payload = await res.json();
      if (!res.ok) appendErrorMessage(`HTTP ${res.status}: ${payload.error || payload.content}`);
      else if (payload.triggered !== false) appendBotMessage(payload);
    } catch (err) {
      appendErrorMessage(err.message);
    } finally {
      send.disabled = !state.actingAs;
    }
  });
  formStrip.appendChild(form);
}

async function resetHelperThread() {
  try {
    const res = await fetch('/api/helper/thread', { method: 'DELETE' });
    if (!res.ok) throw new Error(`reset failed: HTTP ${res.status}`);
    appendBotMessage({ content: '🧹 Simulated helper thread reset.', ephemeral: true });
  } catch (e) {
    appendErrorMessage(e.message);
  }
}

function renderInput(o) {
  let el;
  if (o.choices) {
    const sel = document.createElement('select');
    sel.name = o.name;
    if (!o.required) sel.appendChild(new Option('', ''));
    for (const c of o.choices) sel.appendChild(new Option(c.name, c.value));
    el = sel;
  } else if (o.type === 'boolean') {
    const sel = document.createElement('select');
    sel.name = o.name;
    sel.appendChild(new Option('(unset)', ''));
    sel.appendChild(new Option('true', 'true'));
    sel.appendChild(new Option('false', 'false'));
    el = sel;
  } else if (o.type === 'user') {
    const input = document.createElement('input');
    input.type = 'text';
    input.name = o.name;
    input.setAttribute('list', 'people-list');
    input.placeholder = 'Type or pick a Discord ID';
    input.autocomplete = 'off';
    el = input;
  } else {
    // string, or unknown → text input
    const input = document.createElement('input');
    input.type = 'text';
    input.name = o.name;
    el = input;
  }
  if (o.required) el.required = true;
  return el;
}

async function submitForm(cmd, form) {
  const options = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.value === '' && !el.required) continue;
    options[el.name] = el.value;
  }
  const body = {
    options,
    subcommand: cmd.subName,
    actingAs: state.actingAs,
  };
  // Append "you" message
  appendYouMessage(cmd, options);
  try {
    const res = await fetch(`/api/commands/${cmd.parentName}/run`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    const payload = await res.json();
    if (!res.ok) {
      appendErrorMessage(
        `HTTP ${res.status}: ${payload.error || payload.content || res.statusText}`,
      );
    } else {
      appendBotMessage(payload);
    }
  } catch (e) {
    appendErrorMessage(e.message);
  }
}

// --- Transcript rendering ---
function appendYouMessage(cmd, options) {
  const optsStr = Object.entries(options)
    .map(([k, v]) => `${k}:${v}`)
    .join(' ');
  const el = messageElement({ author: 'You', avatar: 'Y', klass: 'you' });
  el.querySelector('.body').textContent = `/${cmd.displayName} ${optsStr}`.trim();
  transcript.appendChild(el);
  scrollToBottom();
}

function appendYouText(content, isReply = false, replyPing = true) {
  const el = messageElement({ author: 'You', avatar: 'Y', klass: 'you' });
  const body = el.querySelector('.body');
  if (isReply) {
    const context = document.createElement('div');
    context.className = 'reply-context';
    context.textContent = `↪ Replying to Misty · reply ping ${replyPing ? 'ON' : 'OFF'}`;
    body.appendChild(context);
  }
  body.appendChild(document.createTextNode(content));
  transcript.appendChild(el);
  scrollToBottom();
}

function appendBotMessage(payload) {
  const el = messageElement({ author: 'bot', avatar: '🤖', klass: 'bot' });
  const body = el.querySelector('.body');
  const parts = [];
  if (payload.content !== undefined) {
    parts.push(`<div>${hydrateMentions(payload.content, state.peopleMap)}</div>`);
  }
  for (const embed of payload.embeds || []) {
    let html = '<div class="embed">';
    if (embed.title) html += `<h4>${escapeHtml(embed.title)}</h4>`;
    for (const f of embed.fields || []) {
      html += `<div class="field"><span class="field-name">${escapeHtml(f.name)}</span><br>${hydrateMentions(f.value, state.peopleMap)}</div>`;
    }
    html += '</div>';
    parts.push(html);
  }
  body.innerHTML = parts.join('') || '<em>(empty)</em>';
  transcript.appendChild(el);
  scrollToBottom();
}

function appendErrorMessage(msg) {
  const el = messageElement({ author: 'error', avatar: '⚠', klass: 'error' });
  const body = el.querySelector('.body');
  body.classList.add('error');
  body.textContent = msg;
  transcript.appendChild(el);
  scrollToBottom();
}

function messageElement({ author, avatar, klass }) {
  const now = new Date().toTimeString().slice(0, 5);
  const el = document.createElement('div');
  el.className = `message ${klass}`;
  el.innerHTML = `
    <div class="avatar">${escapeHtml(avatar)}</div>
    <div class="content">
      <div class="header"><span class="author">${escapeHtml(author)}</span><span class="time">${escapeHtml(now)}</span></div>
      <div class="body"></div>
    </div>
  `;
  return el;
}

function scrollToBottom() {
  transcript.scrollTop = transcript.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      })[c],
  );
}

main();
