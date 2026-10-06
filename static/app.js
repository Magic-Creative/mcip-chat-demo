/* The demo's browser shell: session, sign-in/register, MCip connection,
   workspace picker, theming and view routing. The chat itself lives in
   chat.js. No inline script (the page's CSP forbids it). */

import { Chat } from './chat.js';

const THEME_KEY = 'mcip-demo-theme';
const THEME_CYCLE = ['auto', 'light', 'dark'];

export const state = {
  csrfToken: '',
  user: null, // {username, is_admin}
  connection: null, // {display_name, email, key_prefix, workspaces, workspace, ...}
  mcipHost: '',
  allowRegister: false,
  configured: false, // an MCip base URL is set (admin Settings)
  settingsOpen: false,
};

export class ApiError extends Error {
  constructor(status, payload) {
    super(payload.message || `HTTP ${status}`);
    this.status = status;
    this.errorCode = payload.errorCode || `HTTP_${status}`;
    this.ui = payload.ui || 'fatal';
    this.advice = payload.advice || '';
    this.retryAfterMs = payload.retry_after_ms;
    this.continueUrl = payload.continue_url;
    this.clientRequestId = payload.client_request_id || null;
  }

  /** "message — advice", what forms and error cards show. */
  get fullText() {
    return this.advice ? `${this.message} ${this.advice}` : this.message;
  }
}

const $ = (id) => document.getElementById(id);

export async function api(path, { method = 'GET', body, csrf = false } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (csrf) headers['X-CSRF-Token'] = state.csrfToken;
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    /* non-JSON error body */
  }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

/* -- banner ---------------------------------------------------------------- */

function showBanner(text, action) {
  $('banner-text').textContent = text;
  const button = $('banner-action');
  if (action) {
    button.textContent = action.label;
    button.hidden = false;
    button.onclick = action.onClick;
  } else {
    button.hidden = true;
    button.onclick = null;
  }
  $('banner').hidden = false;
}

function hideBanner() {
  $('banner').hidden = true;
}

/* -- views ----------------------------------------------------------------- */

const VIEWS = [
  'view-auth',
  'view-unconfigured',
  'view-settings',
  'view-connect',
  'view-workspace',
  'view-chat',
];

function show(viewId) {
  for (const id of VIEWS) $(id).hidden = id !== viewId;
  $('drawer-toggle').hidden = viewId !== 'view-chat' || !isNarrow();
  if (viewId !== 'view-chat') chat.deactivate();
}

function isNarrow() {
  return window.matchMedia('(max-width: 767px)').matches;
}

function route() {
  $('user-chip').hidden = !state.user;
  $('signout-button').hidden = !state.user;
  $('settings-button').hidden = !(state.user && state.user.is_admin);
  if (state.user) {
    $('user-chip').textContent = state.user.is_admin
      ? `${state.user.username} (admin)`
      : state.user.username;
  }
  $('host-chip').hidden = !state.mcipHost;
  $('host-chip').textContent = state.mcipHost;
  $('empty-host').textContent = state.mcipHost || 'MCip';

  if (!state.user) return show('view-auth');
  const isAdmin = Boolean(state.user.is_admin);
  // Admins land on Settings until the MCip address is set (first run).
  if (isAdmin && (state.settingsOpen || !state.configured)) return openSettings();
  if (!state.configured) return show('view-unconfigured');
  if (!state.connection) return show('view-connect');
  if (!state.connection.workspace) return showWorkspacePicker();
  show('view-chat');
  chat.activate();
}

/* -- errors ---------------------------------------------------------------- */

function fillError(element, error) {
  element.textContent =
    error instanceof ApiError ? error.fullText : String(error.message || error);
  element.hidden = false;
}

function clearError(element) {
  element.textContent = '';
  element.hidden = true;
}

/** The demo session is gone (cookie expired or server restarted): re-auth. */
export function sessionExpired(message) {
  state.user = null;
  state.connection = null;
  showBanner(message || 'Your demo session expired. Sign in again.', null);
  route();
}

/** MCip access is gone (key revoked/expired…): offer a fresh connect. */
export function handleReconnect(message) {
  showBanner(`${message} Connect a new key to continue.`, {
    label: 'Reconnect',
    onClick: async () => {
      try {
        await api('/api/connection', { method: 'DELETE', csrf: true });
      } catch {
        /* the key is unusable anyway — proceed */
      }
      state.connection = null;
      hideBanner();
      route();
    },
  });
}

/* -- theme ----------------------------------------------------------------- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  if (theme === 'auto') localStorage.removeItem(THEME_KEY);
  else localStorage.setItem(THEME_KEY, theme);
}

function initTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  const initial = THEME_CYCLE.includes(stored) ? stored : 'auto';
  applyTheme(initial);
  $('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme || 'auto';
    const next = THEME_CYCLE[(THEME_CYCLE.indexOf(current) + 1) % THEME_CYCLE.length];
    applyTheme(next);
  });
}

/* -- workspace picker ------------------------------------------------------ */

function showWorkspacePicker(error) {
  const list = $('workspace-list');
  list.replaceChildren();
  const workspaces = (state.connection && state.connection.workspaces) || [];
  if (workspaces.length === 0) {
    const item = document.createElement('li');
    item.className = 'muted';
    item.textContent =
      'This key can not use any workspace yet. The user must be in the API ' +
      "client's organization, with chat access to one of its workspaces.";
    list.append(item);
  }
  for (const workspace of workspaces) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'workspace-option';
    const name = document.createElement('span');
    name.className = 'workspace-name';
    name.textContent = workspace.name;
    button.append(name);
    button.addEventListener('click', () => chooseWorkspace(workspace.id));
    item.append(button);
    list.append(item);
  }
  if (error) fillError($('workspace-error'), error);
  else clearError($('workspace-error'));
  show('view-workspace');
}

async function chooseWorkspace(workspaceId) {
  try {
    const { connection } = await api('/api/connection/workspace', {
      method: 'PUT',
      body: { workspace_id: workspaceId },
      csrf: true,
    });
    state.connection = connection;
    hideBanner();
    route();
  } catch (error) {
    if (error instanceof ApiError && error.ui === 'auth') return sessionExpired(error.fullText);
    fillError($('workspace-error'), error);
  }
}

/* -- auth ------------------------------------------------------------------ */

function showRegisterForm(showIt) {
  $('form-login').hidden = showIt;
  $('form-register').hidden = !showIt;
  $('toggle-register').textContent = showIt ? 'Back to sign in' : 'Create an account';
}

async function submitAuth(formId, errorId, path) {
  const form = $(formId);
  const errorBox = $(errorId);
  clearError(errorBox);
  const username = form.querySelector('input[name="username"]').value.trim();
  const password = form.querySelector('input[name="password"]').value;
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const payload = await api(path, {
      method: 'POST',
      body: { username, password },
      csrf: true,
    });
    state.csrfToken = payload.csrf_token; // rotates on every login
    state.user = payload.user;
    applySession(await api('/api/session')); // role, configured, connection
    form.reset();
    hideBanner();
    route();
  } catch (error) {
    fillError(errorBox, error);
  } finally {
    submit.disabled = false;
  }
}

/* -- admin settings -------------------------------------------------------- */

function describeSettings(view) {
  const urlHint = $('settings-url-hint');
  if (view.mcip_base_url_source === 'settings') {
    urlHint.textContent = `Saved by ${view.mcip_base_url_updated_by || 'an admin'} on ${formatWhen(view.mcip_base_url_updated_at)}.`;
  } else if (view.mcip_base_url_source === 'env') {
    urlHint.textContent = "Currently the MCIP_BASE_URL default from the server's .env. Save to keep it here.";
  } else {
    urlHint.textContent = "Not set yet: users can't connect until it is.";
  }
  $('settings-key-hint').textContent = view.client_key_prefix
    ? `Stored: ${view.client_key_prefix}… (saved by ${view.client_key_updated_by || 'an admin'} on ${formatWhen(view.client_key_updated_at)}). Leave blank to keep it.`
    : 'No client key stored. Needed when the MCip API client requires one.';
}

function formatWhen(iso) {
  if (!iso) return 'an unknown date';
  const moment = new Date(iso);
  return Number.isNaN(moment.getTime()) ? iso : moment.toLocaleString();
}

async function openSettings() {
  show('view-settings');
  clearError($('settings-error'));
  $('settings-status').textContent = '';
  try {
    const { settings } = await api('/api/admin/settings');
    $('settings-url').value = settings.mcip_base_url || '';
    $('settings-client-key').value = '';
    $('settings-clear-key').checked = false;
    $('settings-clear-key').disabled = !settings.client_key_prefix;
    describeSettings(settings);
    $('settings-close').hidden = !state.configured;
    $('settings-url').focus();
  } catch (error) {
    if (error instanceof ApiError && error.ui === 'auth') return sessionExpired(error.fullText);
    fillError($('settings-error'), error);
  }
}

async function saveSettings() {
  const errorBox = $('settings-error');
  clearError(errorBox);
  const url = $('settings-url').value.trim();
  const key = $('settings-client-key').value.trim();
  const clearKey = $('settings-clear-key').checked;
  const body = {};
  if (url) body.mcip_base_url = url;
  if (key) body.client_key = key;
  if (clearKey) body.clear_client_key = true;
  if (Object.keys(body).length === 0) {
    fillError(errorBox, new Error('Enter the MCip address first.'));
    return;
  }
  if (
    !window.confirm(
      'Saving a new MCip address or client key disconnects every user, including you. Continue?',
    )
  ) {
    return;
  }
  const submit = $('settings-save');
  submit.disabled = true;
  try {
    const result = await api('/api/admin/settings', { method: 'PUT', body, csrf: true });
    $('settings-client-key').value = ''; // never keep the key in the DOM
    $('settings-clear-key').checked = false;
    $('settings-clear-key').disabled = !result.settings.client_key_prefix;
    describeSettings(result.settings);
    const session = await api('/api/session');
    applySession(session);
    $('settings-close').hidden = !state.configured;
    $('settings-status').textContent = result.changed.length
      ? `Saved. ${result.disconnected} user connection(s) were cleared.`
      : 'Saved. Nothing changed.';
  } catch (error) {
    if (error instanceof ApiError && error.ui === 'auth') return sessionExpired(error.fullText);
    fillError(errorBox, error);
  } finally {
    submit.disabled = false;
  }
}

async function testSettings() {
  const status = $('settings-status');
  clearError($('settings-error'));
  status.textContent = 'Testing…';
  try {
    const result = await api('/api/admin/settings/test', { method: 'POST', csrf: true });
    const mark = (check) =>
      ({ ok: '✓', failed: '✗', unsupported: '!', not_set: '–', skipped: '–' })[check.status] || '?';
    const line = (label, check, okText) =>
      `${mark(check)} ${label}: ${check.status === 'ok' ? okText : ''}${
        check.error ? `${check.error} — ` : ''
      }${check.detail || (check.status === 'ok' ? '' : check.status)}`.trim();
    const a = result.address;
    const c = result.client_key;
    const u = result.user_key;
    const parts = [
      line('MCip address', a, `reachable${a.api_version ? ` (API ${a.api_version})` : ''}. `),
      line(
        'API client key',
        c,
        `valid for “${c.api_client ? c.api_client.name : '?'}”${
          c.organization ? ` (${c.organization})` : ''
        }. `,
      ),
      line('Your user key', u, `works with the client key (client: ${u.api_client || '?'}). `),
    ];
    parts.push(result.ok ? 'All checks passed.' : 'Some checks did not pass.');    status.textContent = parts.join('\n');
  } catch (error) {
    status.textContent = '';
    if (error instanceof ApiError && error.ui === 'auth') return sessionExpired(error.fullText);
    fillError($('settings-error'), error);
  }
}

/* -- connect --------------------------------------------------------------- */

async function submitConnect() {
  const errorBox = $('connect-error');
  clearError(errorBox);
  const input = $('connect-key');
  const key = input.value.trim();
  if (!key) return;
  const submit = $('connect-submit');
  submit.disabled = true;
  try {
    const { connection } = await api('/api/connection', {
      method: 'POST',
      body: { key },
      csrf: true,
    });
    input.value = ''; // never keep the key in the DOM
    state.connection = connection;
    hideBanner();
    if (connection.workspaces.length === 1) {
      // one option: skip the picker and go straight to chat
      const chosen = await api('/api/connection/workspace', {
        method: 'PUT',
        body: { workspace_id: connection.workspaces[0].id },
        csrf: true,
      });
      state.connection = chosen.connection;
      route();
    } else {
      route();
    }
  } catch (error) {
    if (error instanceof ApiError && error.ui === 'auth') return sessionExpired(error.fullText);
    fillError(errorBox, error);
  } finally {
    submit.disabled = false;
  }
}

/* -- wiring ---------------------------------------------------------------- */

const chat = new Chat({
  api,
  ApiError,
  state,
  sessionExpired,
  handleReconnect,
  openWorkspacePicker: showWorkspacePicker,
});

function init() {
  initTheme();

  $('form-login').addEventListener('submit', (event) => {
    event.preventDefault();
    submitAuth('form-login', 'login-error', '/api/login');
  });
  $('form-register').addEventListener('submit', (event) => {
    event.preventDefault();
    submitAuth('form-register', 'register-error', '/api/register');
  });
  $('toggle-register').addEventListener('click', () => {
    showRegisterForm(!$('form-login').hidden);
    clearError($('login-error'));
    clearError($('register-error'));
  });

  $('form-connect').addEventListener('submit', (event) => {
    event.preventDefault();
    submitConnect();
  });

  $('form-settings').addEventListener('submit', (event) => {
    event.preventDefault();
    saveSettings();
  });
  $('settings-test').addEventListener('click', testSettings);
  $('settings-button').addEventListener('click', () => {
    state.settingsOpen = true;
    route();
  });
  $('settings-close').addEventListener('click', () => {
    state.settingsOpen = false;
    route();
  });

  $('signout-button').addEventListener('click', async () => {
    try {
      const payload = await api('/api/logout', { method: 'POST', csrf: true });
      state.csrfToken = payload.csrf_token;
    } catch {
      /* proceed to the sign-in screen regardless */
    }
    state.user = null;
    state.connection = null;
    state.settingsOpen = false;
    hideBanner();
    route();
  });

  $('disconnect').addEventListener('click', async () => {
    if (!window.confirm('Forget the stored MCip key on this demo server?')) return;
    try {
      await api('/api/connection', { method: 'DELETE', csrf: true });
      state.connection = null;
      hideBanner();
      route();
    } catch (error) {
      showBanner(error.fullText || 'Could not disconnect.', null);
    }
  });

  window.addEventListener('resize', () => {
    $('drawer-toggle').hidden = $('view-chat').hidden || !isNarrow();
  });

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      chat.focusComposer();
    } else if (event.key === 'Escape') {
      chat.closeDrawer();
    }
  });

  boot();
}

function applySession(session) {
  state.csrfToken = session.csrf_token;
  state.user = session.user;
  state.connection = session.connection;
  state.mcipHost = session.mcip_host || '';
  state.configured = Boolean(session.configured);
  state.allowRegister = session.allow_register;
  $('connect-host').textContent = state.mcipHost || 'MCip';
  $('connect-link').href = state.mcipHost
    ? `https://${state.mcipHost}/user-settings/api-key`
    : '#';
  $('toggle-register').hidden = !state.allowRegister;
}

async function boot() {
  try {
    applySession(await api('/api/session'));
    showRegisterForm(false);
    route();
  } catch {
    showBanner('Cannot reach the demo server. Reload the page to try again.', {
      label: 'Reload',
      onClick: () => window.location.reload(),
    });
  }
}

init();
