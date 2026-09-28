/* Shared frontend core: API client, live updates, toasts, floor-plan rendering.
   Vanilla JS, no build step. */

const API = {
  getCsrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  },

  async request(url, options = {}) {
    const opts = {
      method: options.method || 'GET',
      headers: {
        'Accept': 'application/json',
        'X-CSRFToken': API.getCsrfToken(),
        'X-Requested-With': 'XMLHttpRequest',
      },
      credentials: 'same-origin',
    };
    if (options.body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(options.body);
    }

    let response;
    try {
      response = await fetch(url, opts);
    } catch (networkError) {
      // Network-level failure: no HTTP status to inspect.
      throw new ApiError('NETWORK_ERROR',
        'Cannot reach the server. Check your connection and try again.', 0);
    }

    let payload = null;
    const text = await response.text();
    if (text) {
      try { payload = JSON.parse(text); }
      catch (e) { payload = null; }
    }

    if (!response.ok) {
      const err = (payload && payload.error) || {};
      throw new ApiError(
        err.code || `HTTP_${response.status}`,
        err.message || `Request failed (${response.status}).`,
        response.status,
        err.field,
        err.request_id);
    }
    return payload;
  },

  login: (username, password) =>
    API.request('/api/auth/login', {method: 'POST', body: {username, password}}),
  logout: () => API.request('/api/auth/logout', {method: 'POST'}),
  session: () => API.request('/api/auth/session'),
  floorplan: () => API.request('/api/floorplan'),
  people: () => API.request('/api/people'),
  move: (personId, deskId, version, swap) =>
    API.request('/api/assignments/move', {
      method: 'POST',
      body: {person_id: personId, desk_id: deskId, version, swap: !!swap},
    }),
  updatePerson: (personId, fields) =>
    API.request('/api/people/update', {
      method: 'POST', body: Object.assign({person_id: personId}, fields),
    }),
};

class ApiError extends Error {
  constructor(code, message, status, field, requestId) {
    super(message);
    this.code = code;
    this.status = status;
    this.field = field;
    this.requestId = requestId;
  }
}

/* ------------------------------------------------------------------ toasts */

const Toast = {
  host: null,
  ensure() {
    if (!this.host) {
      this.host = document.createElement('div');
      this.host.className = 'toasts';
      this.host.setAttribute('role', 'status');
      this.host.setAttribute('aria-live', 'polite');
      document.body.appendChild(this.host);
    }
    return this.host;
  },
  show(message, kind = 'info', code = '', ms = 5000) {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.textContent = message;
    if (code) {
      const c = document.createElement('span');
      c.className = 'code';
      c.textContent = code;
      el.appendChild(c);
    }
    this.ensure().appendChild(el);
    setTimeout(() => el.remove(), ms);
  },
  error(err) {
    if (err instanceof ApiError) {
      this.show(err.message, 'error', err.code +
        (err.requestId ? ` | ref ${err.requestId}` : ''), 8000);
    } else {
      this.show(err && err.message ? err.message : String(err), 'error');
    }
  },
};

/* ---------------------------------------------- live updates (WS + polling) */

class LiveUpdates {
  /*
   * Prefers a WebSocket. If the socket cannot be established or drops, falls
   * back to polling so the public view still refreshes automatically.
   */
  constructor({onSnapshot, onStatus, pollMs = 5000}) {
    this.onSnapshot = onSnapshot;
    this.onStatus = onStatus;
    this.pollMs = pollMs;
    this.ws = null;
    this.pollTimer = null;
    this.retry = 0;
    this.stopped = false;
  }

  start() {
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    if (this.ws) { this.ws.onclose = null; this.ws.close(); this.ws = null; }
    this.stopPolling();
  }

  connect() {
    if (this.stopped) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let url;
    try {
      url = `${proto}//${location.host}/ws/floorplan/`;
    } catch (e) {
      this.startPolling();
      return;
    }

    let ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      this.startPolling();
      return;
    }
    this.ws = ws;
    this.setStatus('connecting');

    ws.onopen = () => {
      this.retry = 0;
      this.stopPolling();
      this.setStatus('live');
    };

    ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.type === 'snapshot' || msg.type === 'update') {
        if (msg.payload && msg.payload.snapshot) {
          this.onSnapshot(msg.payload.snapshot, msg.payload.reason || 'update');
        }
      }
    };

    ws.onerror = () => { /* onclose handles the transition */ };

    ws.onclose = () => {
      this.ws = null;
      if (this.stopped) return;
      this.startPolling();
      // Exponential backoff, capped, so a server restart is picked up quickly
      // without hammering it.
      const delay = Math.min(1000 * Math.pow(2, this.retry), 30000);
      this.retry += 1;
      setTimeout(() => this.connect(), delay);
    };
  }

  startPolling() {
    if (this.pollTimer || this.stopped) return;
    this.setStatus('polling');
    this.pollTimer = setInterval(async () => {
      try {
        const snapshot = await API.floorplan();
        this.onSnapshot(snapshot, 'poll');
      } catch (err) {
        this.setStatus('offline');
      }
    }, this.pollMs);
  }

  stopPolling() {
    if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
  }

  setStatus(state) {
    if (this.onStatus) this.onStatus(state);
  }
}

/* ------------------------------------------------------- floor-plan render */

const Plan = (function () {
  let root = null;
  let cells = new Map();     // deskId -> element
  let snapshot = null;
  let lastRendered = null;

  /* Desk accent colours cycle through the PNG palette. Department is not yet
     populated, so colour is assigned deterministically from the grid position
     to keep the scattered pods visually distinguishable. */
  const PALETTE = [
    {pale: 'var(--pod-1-pale)', edge: 'var(--pod-1)', sw: 'var(--pod-1)'},
    {pale: 'var(--pod-2-pale)', edge: 'var(--pod-2)', sw: 'var(--pod-2)'},
    {pale: 'var(--pod-3-pale)', edge: 'var(--pod-3)', sw: 'var(--pod-3)'},
    {pale: 'var(--pod-4-pale)', edge: 'var(--pod-4)', sw: 'var(--pod-4)'},
    {pale: 'var(--pod-5-pale)', edge: 'var(--pod-5)', sw: 'var(--pod-5)'},
    {pale: 'var(--pod-6-pale)', edge: 'var(--pod-6)', sw: 'var(--pod-6)'},
  ];

  function podFor(desk) {
    // Stable mapping so a desk keeps its colour between reloads.
    return PALETTE[(desk.grid_col * 3 + desk.grid_row * 5) % PALETTE.length];
  }

  function init(el) {
    root = el;
    root.classList.add('plan');
  }

  /* Merge consecutive fixed-area cells sharing a label so each area renders
     as one visual block rather than a grid of separate boxes. */
  function fixedBlocks(desks) {
    const fixed = desks.filter(d => d.kind === 'fixed');
    const used = new Set();
    const blocks = [];
    const key = d => `${d.grid_col},${d.grid_row}`;

    for (const desk of fixed) {
      if (used.has(key(desk))) continue;
      const label = desk.label;
      let endCol = desk.grid_col;
      let endRow = desk.grid_row;
      const members = new Set([key(desk)]);
      used.add(key(desk));

      // Extend right, then down, while the label matches.
      let c = desk.grid_col + 1;
      let row = fixed.filter(d => d.grid_row === desk.grid_row);
      while (row.some(d => d.grid_col === c && d.label === label)) {
        members.add(`${c},${desk.grid_row}`);
        used.add(`${c},${desk.grid_row}`);
        endCol = c; c += 1;
      }
      let r = desk.grid_row + 1;
      while (true) {
        const next = fixed.filter(d => d.grid_row === r &&
          d.grid_col >= desk.grid_col && d.grid_col <= endCol);
        if (!next.length || next.some(d => d.label !== label)) break;
        for (const d of next) { members.add(key(d)); used.add(key(d)); }
        endRow = r; r += 1;
      }
      blocks.push({label, startCol: desk.grid_col, startRow: desk.grid_row,
                   endCol, endRow, members});
    }
    return blocks;
  }

  function render(data, opts = {}) {
    if (!root || !data || !data.desks) return;
    snapshot = data;

    const blocks = fixedBlocks(data.desks);
    const blockByCell = new Map();
    blocks.forEach(b => b.members.forEach(m => blockByCell.set(m, b)));

    root.innerHTML = '';
    cells.clear();

    for (const desk of data.desks) {
      const key = `${desk.grid_col},${desk.grid_row}`;
      const el = document.createElement('div');
      el.dataset.deskId = desk.id;
      el.dataset.ref = desk.ref;
      el.dataset.kind = desk.kind;
      el.style.gridColumn = desk.grid_col;
      el.style.gridRow = desk.grid_row;

      if (desk.kind === 'fixed') {
        const block = blockByCell.get(key);
        el.className = 'cell fixed';
        // Only the anchor cell carries the label and the span.
        if (block && block.startCol === desk.grid_col &&
            block.startRow === desk.grid_row) {
          el.style.gridColumn = `${block.startCol} / ${block.endCol + 1}`;
          el.style.gridRow = `${block.startRow} / ${block.endRow + 1}`;
          const span = document.createElement('span');
          span.className = 'flabel';
          span.textContent = block.label;
          el.appendChild(span);
        } else {
          el.style.visibility = 'hidden';
        }
        el.title = `${desk.ref} - ${desk.label} (fixed area)`;
        root.appendChild(el);
        continue;
      }

      const ref = document.createElement('span');
      ref.className = 'ref-tag';
      ref.textContent = desk.ref;

      if (desk.occupant) {
        const pod = podFor(desk);
        el.className = 'cell occupied';
        el.style.setProperty('--pale', pod.pale);
        el.style.setProperty('--edge', pod.edge);

        const who = document.createElement('div');
        who.className = 'who' + (desk.occupant.is_placeholder ? ' placeholder' : '');
        who.textContent = desk.occupant.name;

        const meta = document.createElement('div');
        meta.className = 'meta';
        meta.textContent = [desk.occupant.role, desk.occupant.department]
          .filter(Boolean).join(' / ') || 'Details pending';

        el.append(who, meta, ref);
        el.title = `${desk.occupant.name} at ${desk.ref}` +
          (desk.occupant.is_placeholder ? ' (placeholder - awaiting details)' : '') +
          (opts.interactive ? '\nClick or drag to move' : '');
      } else {
        el.className = 'cell empty-desk';
        const s = document.createElement('span');
        s.className = 'spare';
        s.textContent = 'Spare';
        el.append(s, ref);
        el.title = `${desk.ref} - spare desk` +
          (opts.interactive ? '\nClick to seat someone here' : '');
      }

      root.appendChild(el);
      cells.set(desk.id, el);
    }

    lastRendered = data;
    if (opts.onRendered) opts.onRendered(data);
  }

  function update(data, reason) {
    if (!data || !data.desks) return;
    // Preserve interaction state across a live refresh.
    const previous = cells;
    render(data, {interactive: document.body.classList.contains('admin'),
                  onRendered: () => {
                    if (reason && reason !== 'poll' && reason !== 'initial') {
                      pulse();
                    }
                  }});
    return previous;
  }

  function pulse() {
    if (!lastRendered) return;
    for (const desk of lastRendered.desks) {
      if (desk.occupant) {
        const el = cells.get(desk.id);
        if (el) { el.classList.add('pulse'); setTimeout(() => el.classList.remove('pulse'), 900); }
      }
    }
  }

  function deskAt(id) {
    if (!snapshot) return null;
    return snapshot.desks.find(d => d.id === Number(id)) || null;
  }

  function cellAt(id) { return cells.get(Number(id)) || null; }

  return {init, render, update, deskAt, cellAt, get snapshot() { return snapshot; },
          podFor, get cells() { return cells; }};
})();
