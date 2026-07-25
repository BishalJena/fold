const sessionInput = document.getElementById('session');
const tenantInput = document.getElementById('tenant');
const agentInput = document.getElementById('agent');
const threadInput = document.getElementById('thread');
const threadDatalist = document.getElementById('thread-list');
const loadBtn = document.getElementById('load-btn');
const statePre = document.getElementById('state');
const eventsList = document.getElementById('events');
const messageForm = document.getElementById('message-form');
const contentInput = document.getElementById('content');
const statusEl = document.getElementById('status');
const integrityBadge = document.getElementById('integrity-badge');
const asOfInput = document.getElementById('as-of');
const asOfLabel = document.getElementById('as-of-label');
const resetAsOfBtn = document.getElementById('reset-as-of');
const budgetInput = document.getElementById('budget');
const setBudgetBtn = document.getElementById('set-budget');
const usageSummary = document.getElementById('usage-summary');
const backtestBudgetInput = document.getElementById('backtest-budget');
const runBacktestBtn = document.getElementById('run-backtest');
const backtestResults = document.getElementById('backtest-results');
const cfSelection = document.getElementById('cf-selection');
const cfPayload = document.getElementById('cf-payload');
const cfThread = document.getElementById('cf-thread');
const cfForkBtn = document.getElementById('cf-fork');
const cfResult = document.getElementById('cf-result');
const threadsEl = document.getElementById('threads');

let eventSource = null;
let currentHead = 0;
let asOf = null; // null means live / head
let allEvents = [];
let traceMap = new Map();
let selectedEvent = null;
let knownThreads = new Set(['main']);
let currentScopeKey = '';

function apiUrl(path) {
  return path;
}

function scopeParams() {
  const params = new URLSearchParams();
  params.set('tenant', tenantInput.value.trim() || 'acme');
  params.set('agent', agentInput.value.trim() || 'analyst');
  params.set('thread', threadInput.value.trim() || 'main');
  return params;
}

function scopeBody(extra = {}) {
  return {
    session: sessionInput.value.trim() || 'default',
    tenant: tenantInput.value.trim() || 'acme',
    agent: agentInput.value.trim() || 'analyst',
    thread: threadInput.value.trim() || 'main',
    ...extra,
  };
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.className = isError ? 'status error' : 'status';
}

function setIntegrity(ok, events, brokenAt) {
  integrityBadge.className = ok
    ? 'badge badge-verified'
    : 'badge badge-broken';
  integrityBadge.textContent = ok
    ? `verified (${events} events)`
    : `broken at seq ${brokenAt}`;
}

function updateThreadsUI() {
  threadDatalist.innerHTML = '';
  threadsEl.innerHTML = '';
  const currentThread = threadInput.value.trim() || 'main';
  Array.from(knownThreads).sort().forEach((thread) => {
    const option = document.createElement('option');
    option.value = thread;
    threadDatalist.appendChild(option);

    const chip = document.createElement('span');
    chip.className = 'thread-chip' + (thread === currentThread ? ' active' : '');
    chip.textContent = thread;
    chip.addEventListener('click', () => {
      threadInput.value = thread;
      loadSession();
    });
    threadsEl.appendChild(chip);
  });
}

function addKnownThread(thread) {
  knownThreads.add(thread);
  updateThreadsUI();
}

async function loadIntegrity(session) {
  const params = scopeParams();
  try {
    const resp = await fetch(apiUrl(`/foldos/verify/${session}?${params}`));
    if (!resp.ok) {
      integrityBadge.className = 'badge badge-broken';
      integrityBadge.textContent = 'verify error';
      return;
    }
    const data = await resp.json();
    setIntegrity(data.ok, data.events, data.broken_at);
  } catch {
    integrityBadge.className = 'badge badge-broken';
    integrityBadge.textContent = 'verify error';
  }
}

async function loadState(session) {
  try {
    const params = scopeParams();
    const asOfParam = asOf === null || asOf >= currentHead ? '' : `&as_of=${asOf}`;
    const resp = await fetch(apiUrl(`/foldos/state/${session}?${params}${asOfParam}`));
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      setStatus(`State load failed: ${body.error || resp.statusText}`, true);
      return;
    }
    const data = await resp.json();
    statePre.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    setStatus(`State load error: ${err.message}`, true);
  }
}

function renderEventItem(event) {
  const li = document.createElement('li');
  li.className = 'event-item' + (selectedEvent && selectedEvent.seq === event.seq ? ' selected' : '');
  li.dataset.seq = event.seq;

  const meta = document.createElement('span');
  meta.className = 'event-meta';
  meta.textContent = `[${event.seq}] ${event.kind} — ${JSON.stringify(event.payload).slice(0, 120)}`;
  li.appendChild(meta);

  const actions = document.createElement('span');
  actions.className = 'event-actions';

  const trace = traceMap.get(event.seq);
  if (trace) {
    const a = document.createElement('a');
    a.className = 'trace-link';
    a.href = `http://localhost:8080/trace/${trace.trace_id}?spanId=${trace.span_id}`;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = 'Open in SigNoz';
    actions.appendChild(a);
  }

  li.appendChild(actions);

  li.addEventListener('click', () => {
    selectedEvent = event;
    cfSelection.textContent = `Selected event seq ${event.seq} (${event.kind}). Edit payload and fork.`;
    cfPayload.value = JSON.stringify(event.payload, null, 2);
    cfForkBtn.disabled = false;
    renderEvents();
  });

  return li;
}

function renderEvents() {
  eventsList.innerHTML = '';
  const limit = asOf === null ? Infinity : asOf;
  allEvents
    .filter((e) => e.seq <= limit)
    .slice()
    .reverse()
    .forEach((event) => {
      eventsList.appendChild(renderEventItem(event));
    });
}

async function loadEvents(session) {
  const params = scopeParams();
  try {
    const resp = await fetch(apiUrl(`/foldos/events/${session}?after=0&${params}`));
    if (!resp.ok) return;
    const data = await resp.json();
    currentHead = data.head || 0;
    currentScopeKey = data.stream || '';
    allEvents = data.events || [];
    updateAsOfSlider();
    renderEvents();
  } catch {
    // ignore
  }
}

function updateAsOfSlider() {
  asOfInput.max = currentHead;
  if (asOf === null) {
    asOfInput.value = currentHead;
    asOfLabel.textContent = 'live';
  } else {
    asOfInput.value = Math.min(asOf, currentHead);
    asOfLabel.textContent = asOf.toString();
  }
}

async function applyAsOf(value) {
  const num = parseInt(value, 10);
  if (Number.isNaN(num) || num >= currentHead) {
    asOf = null;
  } else {
    asOf = num;
  }
  updateAsOfSlider();
  await loadState(sessionInput.value.trim() || 'default');
  renderEvents();
}

async function loadTraceIndex(session) {
  traceMap = new Map();
  try {
    const resp = await fetch(apiUrl(`/foldos/trace-index?session=${encodeURIComponent(session)}`));
    if (!resp.ok) return;
    const entries = await resp.json();
    if (!Array.isArray(entries)) return;
    entries.forEach((entry) => {
      if (entry.seq && entry.trace_id && entry.span_id) {
        traceMap.set(entry.seq, { trace_id: entry.trace_id, span_id: entry.span_id });
      }
    });
  } catch {
    // ignore
  }
}

async function loadUsage(session) {
  const params = scopeParams();
  try {
    const resp = await fetch(apiUrl(`/foldos/usage/${session}?${params}`));
    if (!resp.ok) return;
    const usage = await resp.json();
    const spent = parseFloat(usage.cost_usd) || 0;

    // Budget lives in state data, so fetch current state to read it.
    const stateResp = await fetch(apiUrl(`/foldos/state/${session}?${params}`));
    let budget = null;
    if (stateResp.ok) {
      const state = await stateResp.json();
      budget = state.data?.budget_usd;
    }

    if (budget === null || budget === undefined) {
      usageSummary.innerHTML = `<div>spent $${spent.toFixed(4)} / no budget set</div>`;
      return;
    }
    budget = parseFloat(budget);
    const pct = budget > 0 ? Math.min((spent / budget) * 100, 100) : 0;
    const over = spent > budget;
    usageSummary.innerHTML = `
      <div>spent $${spent.toFixed(4)} / budget $${budget.toFixed(4)} (${pct.toFixed(1)}%)</div>
      <div class="progress-bar"><div class="progress-fill ${over ? 'over' : ''}" style="width: ${pct}%"></div></div>
    `;
  } catch {
    usageSummary.textContent = 'Unable to load usage';
  }
}

async function connectSSE(session) {
  if (eventSource) {
    eventSource.close();
  }
  const params = scopeParams();
  eventSource = new EventSource(apiUrl(`/foldos/events/${session}/stream?follow=true&${params}`));
  eventSource.addEventListener('foldos.event', (ev) => {
    try {
      const event = JSON.parse(ev.data);
      if (!allEvents.find((e) => e.seq === event.seq)) {
        allEvents.push(event);
        allEvents.sort((a, b) => a.seq - b.seq);
      }
      currentHead = Math.max(currentHead, event.seq);
      updateAsOfSlider();
      if (asOf === null) {
        renderEvents();
      }
      loadIntegrity(session);
      loadUsage(session);
    } catch {
      // ignore malformed event
    }
  });
  eventSource.addEventListener('error', () => {
    setStatus('SSE connection error', true);
  });
}

async function sendMessage(session, content) {
  const resp = await fetch(apiUrl('/foldos/message'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(scopeBody({ role: 'user', content })),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.error || resp.statusText);
  }
  return resp.json();
}

async function loadSession() {
  const session = sessionInput.value.trim() || 'default';
  const thread = threadInput.value.trim() || 'main';
  sessionInput.value = session;
  threadInput.value = thread;
  addKnownThread(thread);
  asOf = null;
  selectedEvent = null;
  cfSelection.textContent = 'Select an event from the Events panel to rewrite it.';
  cfPayload.value = '';
  cfForkBtn.disabled = true;
  cfResult.innerHTML = '';
  setStatus('Loading…');
  await loadTraceIndex(session);
  await loadIntegrity(session);
  await loadState(session);
  await loadEvents(session);
  await loadUsage(session);
  connectSSE(session);
  setStatus('Connected');
}

async function setBudget() {
  const value = parseFloat(budgetInput.value);
  if (Number.isNaN(value)) {
    setStatus('Enter a valid budget', true);
    return;
  }
  const session = sessionInput.value.trim() || 'default';
  try {
    const resp = await fetch(apiUrl('/foldos/policy'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(scopeBody({ key: 'budget_usd', value, reason: 'console budget set' })),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || resp.statusText);
    }
    setStatus('Budget set');
    await loadUsage(session);
    await loadState(session);
    await loadEvents(session);
    await loadIntegrity(session);
  } catch (err) {
    setStatus(`Set budget failed: ${err.message}`, true);
  }
}

async function runBacktest() {
  const value = parseFloat(backtestBudgetInput.value);
  if (Number.isNaN(value)) {
    setStatus('Enter a candidate budget', true);
    return;
  }
  const session = sessionInput.value.trim() || 'default';
  try {
    const resp = await fetch(apiUrl('/foldos/backtest'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(scopeBody({ policy: { key: 'budget_usd', value } })),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || resp.statusText);
    }
    const data = await resp.json();
    backtestResults.innerHTML = '';
    if (!data.violations || data.violations.length === 0) {
      backtestResults.innerHTML = '<li>No violations</li>';
      return;
    }
    data.violations.forEach((v) => {
      const li = document.createElement('li');
      li.className = 'violation';
      li.textContent = `seq ${v.seq}: cost $${parseFloat(v.observed).toFixed(4)} > limit $${parseFloat(v.limit).toFixed(4)}`;
      backtestResults.appendChild(li);
    });
    setStatus(`Backtest: ${data.violations.length} violation(s) over ${data.evaluated} events`);
  } catch (err) {
    setStatus(`Backtest failed: ${err.message}`, true);
  }
}

async function forkCounterfactual() {
  if (!selectedEvent) return;
  const thread = cfThread.value.trim();
  if (!thread) {
    setStatus('Enter a new thread name', true);
    return;
  }
  let payloadOverrides;
  try {
    payloadOverrides = JSON.parse(cfPayload.value);
  } catch (err) {
    setStatus(`Invalid payload JSON: ${err.message}`, true);
    return;
  }
  const session = sessionInput.value.trim() || 'default';
  const body = {
    session,
    tenant: tenantInput.value.trim() || 'acme',
    agent: agentInput.value.trim() || 'analyst',
    event_seq: selectedEvent.seq,
    thread,
    payload_overrides: payloadOverrides,
  };
  try {
    const resp = await fetch(apiUrl('/foldos/counterfactual'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}));
      throw new Error(errBody.error || resp.statusText);
    }
    const data = await resp.json();
    addKnownThread(thread);
    cfResult.innerHTML = `
      <div>
        <div class="cf-scope">Original: ${data.original_scope}</div>
        <pre>${JSON.stringify(data.original_state, null, 2)}</pre>
      </div>
      <div>
        <div class="cf-scope">Branch: ${data.branch_scope}</div>
        <pre>${JSON.stringify(data.branch_state, null, 2)}</pre>
      </div>
    `;
    setStatus(`Counterfactual branch created: ${data.branch_scope}`);
  } catch (err) {
    setStatus(`Fork failed: ${err.message}`, true);
  }
}

loadBtn.addEventListener('click', loadSession);

sessionInput.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') loadSession();
});

asOfInput.addEventListener('input', () => applyAsOf(asOfInput.value));

resetAsOfBtn.addEventListener('click', () => applyAsOf(currentHead));

setBudgetBtn.addEventListener('click', setBudget);

runBacktestBtn.addEventListener('click', runBacktest);

cfForkBtn.addEventListener('click', forkCounterfactual);

messageForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const session = sessionInput.value.trim() || 'default';
  const content = contentInput.value.trim();
  if (!content) return;
  try {
    await sendMessage(session, content);
    contentInput.value = '';
    setStatus('Message sent');
    await loadState(session);
    await loadUsage(session);
  } catch (err) {
    setStatus(`Send failed: ${err.message}`, true);
  }
});

loadSession();
