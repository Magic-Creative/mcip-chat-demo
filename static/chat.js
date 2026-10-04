/* The chat view: conversations in the sidebar, streamed turns in the main
   pane. Talks only to the demo backend (which owns the MCip key) and switches
   on the `ui` state the backend puts on every error, never on message text.

   The turn rules it implements (docs/integration-guide.md §7–§8):

   * one client_request_id per turn, created here and echoed back by the
     backend on every start / retry / error event (and on HTTP errors), so a
     Retry re-sends the id the first attempt used — a retry of a turn that
     finished on MCip comes back as the stored answer ("recovered, not
     charged again") with start.replayed = true;
   * errors before any text: Retry button (and one auto-retry after a busy
     countdown); errors after text started: the stream shows error + done and
     the Retry button owns the recovery. */

import { marked } from './vendor/marked.esm.js';
import DOMPurify from './vendor/purify.es.mjs';

const HINT_ENTER = 'Enter sends — Shift+Enter adds a line';

/** Render assistant Markdown, then sanitize: model output is untrusted. */
function markdownToHtml(text) {
  const html = marked.parse(text, { breaks: true, gfm: true, async: false });
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}

function openLinksInNewTab(root) {
  for (const link of root.querySelectorAll('a[href]')) {
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
  }
}

/** The turn's idempotency key: created before anything can fail, reused by
    Retry. randomUUID needs a secure context (https or localhost); the byte
    fallback keeps a plain-http demo working too. */
function newClientRequestId() {
  if (globalThis.crypto && crypto.randomUUID) return `demo-${crypto.randomUUID()}`;
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return `demo-${[...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

/** Untrusted URLs (model citations, error links) may go in an href only as
    http(s); anything else renders as plain text instead. */
function safeHttpUrl(value) {
  if (typeof value !== 'string' || !value) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

function creditsText(micros) {
  const credits = (Number(micros) || 0) / 1_000_000;
  return `${credits.toLocaleString(undefined, { maximumFractionDigits: 4 })} credits`;
}

function usageText(usage) {
  if (!usage || (!usage.model && !usage.total_tokens && !usage.credits_micros)) return '';
  const parts = [];
  if (usage.model) parts.push(usage.model);
  if (usage.total_tokens) parts.push(`${Number(usage.total_tokens).toLocaleString()} tokens`);
  parts.push(creditsText(usage.credits_micros));
  return parts.join(' · ');
}

/** Parse the backend's SSE response into event objects. */
async function* sseEvents(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const data = frame
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
        .join('\n');
      if (!data) continue;
      try {
        yield JSON.parse(data);
      } catch {
        /* ignore a frame we can not parse */
      }
    }
  }
}

const $ = (id) => document.getElementById(id);

export class Chat {
  constructor(deps) {
    this.api = deps.api;
    this.ApiError = deps.ApiError;
    this.state = deps.state;
    this.sessionExpired = deps.sessionExpired;
    this.handleReconnect = deps.handleReconnect;
    this.openWorkspacePicker = deps.openWorkspacePicker;

    this.conversations = [];
    this.currentId = null;
    this.turn = null; // the in-flight turn, if any
    this.pendingRetry = null; // timer id of a scheduled auto-retry

    this.els = {
      messages: $('messages'),
      emptyState: $('empty-state'),
      list: $('conversation-list'),
      listError: $('conversation-error'),
      composer: $('composer'),
      input: $('composer-input'),
      send: $('send-button'),
      stop: $('stop-button'),
      status: $('status-line'),
      summary: $('connection-summary'),
      newChat: $('new-chat'),
      scrim: $('scrim'),
      chatLayout: $('view-chat'),
      drawerToggle: $('drawer-toggle'),
    };

    this.els.composer.addEventListener('submit', (event) => {
      event.preventDefault();
      this.submitComposer();
    });
    this.els.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        this.submitComposer();
      }
    });
    this.els.input.addEventListener('input', () => this.autosize());
    this.els.stop.addEventListener('click', () => this.stop());
    this.els.newChat.addEventListener('click', () => this.newChat());
    this.els.drawerToggle.addEventListener('click', () => this.toggleDrawer());
    this.els.scrim.addEventListener('click', () => this.closeDrawer());
  }

  /* -- lifecycle ----------------------------------------------------------- */

  async activate() {
    this.renderConnectionSummary();
    try {
      await this.refreshConversations();
    } catch (error) {
      this.showListError(error);
    }
  }

  deactivate() {
    this.closeDrawer();
    this.cancelPendingRetry();
    if (this.turn) this.stop(); // leaving the view abandons the stream
  }

  focusComposer() {
    this.els.input.focus();
  }

  toggleDrawer() {
    this.els.chatLayout.classList.toggle('drawer-open');
    this.els.drawerToggle.setAttribute(
      'aria-expanded',
      this.els.chatLayout.classList.contains('drawer-open') ? 'true' : 'false',
    );
  }

  closeDrawer() {
    this.els.chatLayout.classList.remove('drawer-open');
    this.els.drawerToggle.setAttribute('aria-expanded', 'false');
  }

  autosize() {
    const input = this.els.input;
    input.style.height = 'auto';
    input.style.height = `${input.scrollHeight}px`;
  }

  setStatus(text) {
    this.els.status.textContent = text;
  }

  setBusy(busy) {
    this.els.send.disabled = busy;
    this.els.stop.hidden = !busy;
    this.els.input.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  /* -- conversation list --------------------------------------------------- */

  renderConnectionSummary() {
    const connection = this.state.connection;
    if (!connection) return;
    const who = connection.email || connection.display_name || 'connected';
    const workspace = connection.workspace ? connection.workspace.name : 'no workspace';
    this.els.summary.textContent = `${who} · ${workspace} · key ${connection.key_prefix}…`;
  }

  showListError(error) {
    this.els.listError.textContent = error.fullText || String(error);
    this.els.listError.hidden = false;
  }

  async refreshConversations() {
    const { conversations } = await this.api('/api/conversations');
    this.conversations = conversations;
    this.renderConversations();
  }

  renderConversations() {
    this.els.listError.hidden = true;
    this.els.list.replaceChildren();
    for (const conversation of this.conversations) {
      const item = document.createElement('li');
      item.className = 'conversation-item';
      if (conversation.id === this.currentId) item.classList.add('active');

      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'conversation-open';
      open.title = conversation.title;
      const title = document.createElement('span');
      title.className = 'conversation-title';
      title.textContent = conversation.title;
      open.append(title);
      open.addEventListener('click', () => {
        this.closeDrawer();
        this.openConversation(conversation.id);
      });

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'button ghost conversation-delete';
      remove.textContent = 'Delete';
      remove.setAttribute('aria-label', `Delete conversation “${conversation.title}”`);
      remove.addEventListener('click', () => this.deleteConversation(conversation.id));

      item.append(open, remove);
      this.els.list.append(item);
    }
  }

  markActive() {
    for (const item of this.els.list.querySelectorAll('.conversation-item')) {
      item.classList.remove('active');
    }
    this.renderConversations();
  }

  /* -- transcript ---------------------------------------------------------- */

  clearMessages() {
    for (const child of [...this.els.messages.children]) {
      if (child !== this.els.emptyState) child.remove();
    }
    this.els.emptyState.hidden = false;
  }

  setEmptyHidden(hidden) {
    this.els.emptyState.hidden = hidden;
  }

  scrollToBottom() {
    this.els.messages.scrollTop = this.els.messages.scrollHeight;
  }

  nearBottom() {
    const el = this.els.messages;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  newChat() {
    if (this.turn) return; // do not silently drop a running turn
    this.cancelPendingRetry();
    this.currentId = null;
    this.clearMessages();
    this.renderConversations();
    this.setStatus('');
    this.closeDrawer();
    this.focusComposer();
  }

  async openConversation(id) {
    if (this.turn) return;
    this.cancelPendingRetry();
    this.currentId = id;
    this.renderConversations();
    this.clearMessages();
    this.setStatus('Loading…');
    try {
      const page = await this.api(`/api/conversations/${id}/messages`);
      this.setEmptyHidden(true);
      this.renderTranscript(page.messages);
      if (page.has_more) this.prependOlderButton(page.next_before);
      this.setStatus('');
      this.scrollToBottom();
    } catch (error) {
      if (error instanceof this.ApiError && error.ui === 'auth') {
        return this.sessionExpired(error.fullText);
      }
      this.setStatus('');
      this.appendErrorCard({ message: error.fullText, ui: error.ui, errorCode: error.errorCode });
    }
  }

  renderTranscript(messages) {
    for (const message of messages) {
      if (message.role === 'user') {
        this.appendBubble('user', message.text);
      } else {
        const bubble = this.appendBubble('assistant', '');
        bubble.body.innerHTML = markdownToHtml(message.text);
        openLinksInNewTab(bubble.body);
        this.renderCitations(bubble, message.citations || []);
        bubble.meta.textContent = new Date(message.created_at).toLocaleString();
      }
    }
  }

  prependOlderButton(before) {
    const wrap = document.createElement('div');
    wrap.className = 'older-wrap';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'button ghost';
    button.textContent = 'Load older messages';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const page = await this.api(`/api/conversations/${this.currentId}/messages?before=${before}`);
        const height = this.els.messages.scrollHeight;
        const fragment = document.createDocumentFragment();
        for (const message of page.messages) {
          const node = this.messageNode(message);
          fragment.append(node);
        }
        wrap.replaceWith(fragment);
        if (page.has_more) this.prependOlderButton(page.next_before);
        this.els.messages.scrollTop += this.els.messages.scrollHeight - height;
      } catch (error) {
        button.disabled = false;
        this.showListError(error);
      }
    });
    wrap.append(button);
    this.els.messages.prepend(wrap);
  }

  messageNode(message) {
    if (message.role === 'user') {
      const el = document.createElement('div');
      el.className = 'msg msg-user';
      el.textContent = message.text;
      return el;
    }
    const el = document.createElement('div');
    el.className = 'msg msg-assistant';
    const body = document.createElement('div');
    body.className = 'msg-body';
    body.innerHTML = markdownToHtml(message.text);
    openLinksInNewTab(body);
    const citations = document.createElement('ol');
    citations.className = 'citations';
    el.append(body, citations);
    this.fillCitations(citations, message.citations || []);
    return el;
  }

  async deleteConversation(id) {
    if (this.turn) return;
    const row = this.conversations.find((c) => c.id === id);
    if (!window.confirm(`Delete “${row ? row.title : id}”? The messages are removed in MCip too.`)) {
      return;
    }
    try {
      await this.api(`/api/conversations/${id}`, { method: 'DELETE', csrf: true });
      if (this.currentId === id) this.newChat();
      await this.refreshConversations();
    } catch (error) {
      this.showListError(error);
    }
  }

  /* -- messages ------------------------------------------------------------ */

  appendBubble(kind, text) {
    this.setEmptyHidden(true);
    const el = document.createElement('div');
    el.className = `msg msg-${kind}`;
    let body = null;
    let meta = null;
    if (kind === 'assistant') {
      body = document.createElement('div');
      body.className = 'msg-body';
      meta = document.createElement('div');
      meta.className = 'msg-meta';
      el.append(body, meta);
    } else {
      el.textContent = text;
    }
    this.els.messages.append(el);
    this.scrollToBottom();
    return { el, body, meta };
  }

  appendNotice(text, after) {
    const el = document.createElement('div');
    el.className = 'msg-notice';
    el.textContent = text;
    if (after && after.el) after.el.after(el);
    else this.els.messages.append(el);
    this.scrollToBottom();
    return el;
  }

  appendErrorCard(error, { snapshot = null, bubble = null } = {}) {
    const card = document.createElement('div');
    card.className = 'msg-error';
    card.setAttribute('role', 'alert');

    const title = document.createElement('p');
    title.className = 'msg-error-title';
    title.textContent = `Error: ${error.errorCode}`;
    const message = document.createElement('p');
    message.textContent = error.message || 'The turn failed.';
    card.append(title, message);

    const actions = document.createElement('div');
    actions.className = 'actions';

    const continueUrl = safeHttpUrl(error.continueUrl);
    if (continueUrl) {
      const link = document.createElement('a');
      link.className = 'button primary';
      link.href = continueUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Open MCip to approve';
      actions.append(link);
    }
    if (snapshot) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'button';
      retry.textContent = 'Retry';
      retry.addEventListener('click', () => {
        card.remove();
        this.retryTurn(snapshot, bubble);
      });
      actions.append(retry);
    }
    if (actions.childElementCount) card.append(actions);
    this.els.messages.append(card);
    this.scrollToBottom();
    return card;
  }

  renderCitations(bubble, citations) {
    let list = bubble.el.querySelector('.citations');
    if (!list) {
      list = document.createElement('ol');
      list.className = 'citations';
      bubble.body.after(list);
    }
    this.fillCitations(list, citations);
  }

  fillCitations(list, citations) {
    list.replaceChildren();
    for (const citation of citations) {
      const item = document.createElement('li');
      const label = citation.title || (citation.document_id ? `Document ${citation.document_id}` : 'Source');
      const url = safeHttpUrl(citation.url);
      if (url) {
        const link = document.createElement('a');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = `[${citation.index}] ${label}`;
        item.append(link);
      } else {
        item.textContent = `[${citation.index}] ${label}`;
      }
      if (citation.snippet) {
        const snippet = document.createElement('div');
        snippet.className = 'muted small';
        snippet.textContent = citation.snippet;
        item.append(snippet);
      }
      list.append(item);
    }
    list.hidden = list.childElementCount === 0;
  }

  submitComposer() {
    const text = this.els.input.value.trim();
    if (!text || this.turn) return;
    this.els.input.value = '';
    this.autosize();
    this.send(text);
  }

  /* -- the turn ------------------------------------------------------------ */

  async send(text) {
    this.cancelPendingRetry();
    this.retireRetryCards();
    const userBubble = this.appendBubble('user', text);
    const assistant = this.appendBubble('assistant', '');
    assistant.el.classList.add('streaming');
    const turn = {
      message: text,
      conversationId: this.currentId,
      clientRequestId: newClientRequestId(), // ours from the start: Retry reuses it
      retry: false,
      userBubble,
      assistant,
      text: '',
      citations: [],
      error: null,
      status: 'running',
      renderQueued: false,
      autoRetried: false,
      controller: new AbortController(),
    };
    this.turn = turn;
    await this.runTurn(turn);
  }

  async retryTurn(snapshot, bubble) {
    if (this.turn) return;
    this.cancelPendingRetry();
    const turn = {
      message: snapshot.message,
      conversationId: snapshot.conversationId,
      clientRequestId: snapshot.clientRequestId, // the same id: server replays
      retry: true,
      userBubble: null,
      assistant: bubble,
      text: '',
      citations: [],
      error: null,
      status: 'running',
      renderQueued: false,
      autoRetried: true, // a manual retry must not schedule another auto one
      controller: new AbortController(),
    };
    bubble.el.classList.add('streaming');
    bubble.body.replaceChildren();
    const list = bubble.el.querySelector('.citations');
    if (list) list.replaceChildren();
    bubble.meta.textContent = '';
    this.turn = turn;
    await this.runTurn(turn);
  }

  snapshotTurn(turn) {
    return {
      message: turn.message,
      conversationId: turn.conversationId,
      clientRequestId: turn.clientRequestId,
    };
  }

  async runTurn(turn) {
    this.setBusy(true);
    this.setStatus(turn.retry ? 'Retrying the same turn…' : 'Sending to MCip…');
    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this.state.csrfToken },
        credentials: 'same-origin',
        signal: turn.controller.signal,
        body: JSON.stringify({
          conversation_id: turn.conversationId,
          message: turn.message,
          stream: true,
          client_request_id: turn.clientRequestId,
        }),
      });
      if (!response.ok) {
        let payload = {};
        try {
          payload = await response.json();
        } catch {
          /* not JSON */
        }
        throw new this.ApiError(response.status, payload);
      }
      for await (const event of sseEvents(response)) {
        this.handleEvent(turn, event);
        if (event.event === 'done') break;
      }
    } catch (error) {
      if (error.name === 'AbortError') {
        turn.status = 'stopped';
        this.onTurnStopped(turn);
      } else if (error instanceof this.ApiError) {
        this.onHttpError(turn, error);
      } else {
        this.onHttpError(
          turn,
          new this.ApiError(0, {
            errorCode: 'NETWORK_ERROR',
            message: 'Lost the connection to the demo server.',
            ui: 'retryable',
            advice: 'Check your network and retry.',
          }),
        );
      }
    } finally {
      this.flushRender(turn);
      turn.assistant.el.classList.remove('streaming');
      if (this.turn === turn) this.turn = null;
      this.setBusy(false);
    }
  }

  /** Retry buttons from earlier failed turns stop working once a new turn
      starts in this chat: a superseded id would re-run an old turn out of
      order, and its answer could land in a different MCip conversation. */
  retireRetryCards() {
    for (const button of this.els.messages.querySelectorAll('.msg-error button')) {
      button.disabled = true;
      button.title = 'Superseded by a newer message — ask again instead.';
    }
  }

  /** A failure before the stream started: the same handling as events. */
  onHttpError(turn, error) {
    if (error.clientRequestId) turn.clientRequestId = error.clientRequestId;
    if (error.ui === 'auth') return this.sessionExpired(error.fullText);
    if (error.ui === 'reconnect') return this.handleReconnect(error.fullText);
    if (error.ui === 'workspace') {
      this.appendErrorCard(error);
      return this.openWorkspacePicker(error);
    }
    turn.error = {
      errorCode: error.errorCode,
      message: error.message,
      ui: error.ui,
      retryable: true,
      continueUrl: error.continueUrl,
      retryAfterMs: error.retryAfterMs,
    };
    this.setStatus('');
    turn.errorCard = this.appendErrorCard(turn.error, {
      snapshot: this.snapshotTurn(turn),
      bubble: turn.assistant,
    });
    this.maybeAutoRetry(turn);
  }

  onTurnStopped(turn) {
    this.setStatus('Stopped.');
    this.appendNotice('Stopped. MCip stopped this answer too; Retry asks again (it runs as a new turn).');
    this.appendErrorCard(
      {
        errorCode: 'STOPPED',
        message: 'You stopped this answer.',
        ui: 'retryable',
      },
      { snapshot: this.snapshotTurn(turn), bubble: turn.assistant },
    );
  }

  handleEvent(turn, event) {
    switch (event.event) {
      case 'start': {
        if (event.client_request_id) turn.clientRequestId = event.client_request_id;
        if (event.local_conversation_id) {
          const wasNew = this.currentId === null;
          this.currentId = event.local_conversation_id;
          turn.conversationId = event.local_conversation_id;
          if (wasNew) {
            // give the new conversation a row in the sidebar right away
            this.refreshConversations().catch(() => {});
          }
          this.markActive();
        }
        if (event.replayed) {
          this.appendNotice(
            'Recovered the finished turn from MCip — it was not charged again.',
            turn.assistant,
          );
          this.setStatus('Recovered the finished turn — not charged again.');
        } else {
          this.setStatus('MCip is answering…');
        }
        break;
      }
      case 'delta': {
        turn.text += event.text || '';
        this.scheduleRender(turn);
        break;
      }
      case 'status': {
        this.setStatus(event.text || '');
        break;
      }
      case 'citation': {
        turn.citations.push(event);
        this.renderCitations(turn.assistant, turn.citations);
        break;
      }
      case 'action_rejected': {
        this.appendActionRejected(event, turn.assistant);
        break;
      }
      case 'retry': {
        if (event.client_request_id) turn.clientRequestId = event.client_request_id;
        this.setStatus(
          `MCip is busy (${event.errorCode}) — retrying in ${event.delay_s}s ` +
            `(attempt ${event.attempt} of ${event.max_attempts})…`,
        );
        break;
      }
      case 'error': {
        if (event.client_request_id) turn.clientRequestId = event.client_request_id;
        turn.error = event;
        if (event.ui === 'auth') return this.sessionExpired(event.message);
        if (event.ui === 'reconnect') return this.handleReconnect(event.message);
        if (event.ui === 'workspace') {
          this.appendErrorCard(event);
          return this.openWorkspacePicker(event);
        }
        turn.error = {
          errorCode: event.errorCode,
          message: event.message,
          ui: event.ui,
          retryable: Boolean(event.retryable),
          continueUrl: event.continue_url || null,
          retryAfterMs: event.retry_after_ms,
        };
        turn.errorCard = this.appendErrorCard(turn.error, {
          snapshot: event.retryable ? this.snapshotTurn(turn) : null,
          bubble: turn.assistant,
        });
        this.maybeAutoRetry(turn);
        break;
      }
      case 'done': {
        turn.status = event.status || 'completed';
        this.setStatus(event.status === 'error' ? 'The turn failed.' : 'Done.');
        const text = usageText(event.usage);
        if (text) turn.assistant.meta.textContent = text;
        if (event.status === 'error' && !turn.error) {
          this.appendErrorCard({
            errorCode: 'TURN_FAILED',
            message: 'The turn ended without an answer.',
            ui: 'retryable',
          }, { snapshot: this.snapshotTurn(turn), bubble: turn.assistant });
        }
        break;
      }
      default:
        break; // unknown events are ignored on purpose (Guide §5)
    }
  }

  scheduleRender(turn) {
    if (turn.renderQueued) return;
    turn.renderQueued = true;
    turn.renderFrame = requestAnimationFrame(() => {
      turn.renderQueued = false;
      this.renderTurnText(turn);
    });
  }

  renderTurnText(turn) {
    const stick = this.nearBottom();
    turn.assistant.body.innerHTML = markdownToHtml(turn.text);
    openLinksInNewTab(turn.assistant.body);
    if (stick) this.scrollToBottom();
  }

  /** The last deltas may still be waiting for a frame: paint them now. */
  flushRender(turn) {
    if (!turn.renderQueued) return;
    cancelAnimationFrame(turn.renderFrame);
    turn.renderQueued = false;
    this.renderTurnText(turn);
  }

  appendActionRejected(event, bubble) {
    const card = document.createElement('div');
    card.className = 'action-rejected';
    const line = document.createElement('p');
    line.textContent = `Action not allowed: ${event.tool} (${event.reason})`;
    const detail = document.createElement('p');
    detail.className = 'muted small';
    detail.textContent = event.message || '';
    card.append(line, detail);
    bubble.el.after(card);
    this.scrollToBottom();
  }

  /** One auto-retry after a busy countdown, and only before any text. */
  maybeAutoRetry(turn) {
    const error = turn.error;
    if (!error || turn.autoRetried) return;
    if (error.ui !== 'busy' || turn.text !== '') return;
    turn.autoRetried = true;
    const waitMs = Math.max(1000, Math.min(Number(error.retryAfterMs) || 5000, 60_000));
    const snapshot = this.snapshotTurn(turn);
    const bubble = turn.assistant;
    const card = turn.errorCard;
    let remaining = Math.ceil(waitMs / 1000);
    const tick = () => {
      this.pendingRetry = null;
      if (remaining > 0) {
        this.setStatus(`MCip is busy — retrying in ${remaining}s…`);
        remaining -= 1;
        this.pendingRetry = setTimeout(tick, 1000);
        return;
      }
      if (this.turn !== null) return; // another turn started meanwhile
      if (card) card.remove();
      this.setStatus('');
      this.retryTurn(snapshot, bubble);
    };
    tick();
  }

  cancelPendingRetry() {
    if (this.pendingRetry) {
      clearTimeout(this.pendingRetry);
      this.pendingRetry = null;
    }
  }

  stop() {
    const turn = this.turn;
    if (!turn) return;
    this.cancelPendingRetry();
    turn.controller.abort();
  }
}
