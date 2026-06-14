'use strict';

const API = '';
let adminToken = null;
let currentTab = 'pending';
let pollTimer = null;

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

async function doAdminLogin() {
  const email = document.getElementById('adminEmail').value.trim();
  const password = document.getElementById('adminPassword').value;
  const errEl = document.getElementById('adminLoginError');
  const btn = document.getElementById('adminLoginBtn');
  errEl.textContent = '';
  btn.disabled = true;

  try {
    const res = await fetch(`${API}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const json = await res.json();
    if (!res.ok) { errEl.textContent = json.detail || 'Login failed'; return; }

    const { token, role } = json.data;
    if (role !== 'admin') { errEl.textContent = 'Not an admin account.'; return; }

    adminToken = token;
    sessionStorage.setItem('adminToken', token);
    sessionStorage.setItem('adminEmail', email);
    enterAdmin(email);
  } catch (e) {
    errEl.textContent = 'Network error.';
  } finally {
    btn.disabled = false;
  }
}

function doAdminLogout() {
  adminToken = null;
  sessionStorage.removeItem('adminToken');
  sessionStorage.removeItem('adminEmail');
  clearInterval(pollTimer);
  document.getElementById('authOverlay').style.display = 'flex';
  document.getElementById('adminLayout').style.display = 'none';
}

function enterAdmin(email) {
  document.getElementById('authOverlay').style.display = 'none';
  document.getElementById('adminLayout').style.display = 'block';
  document.getElementById('adminUserLabel').textContent = email;
  loadRequests();
  pollTimer = setInterval(loadRequests, 10000);
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------

function switchTab(status) {
  currentTab = status;
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.status === status);
  });
  loadRequests();
}

// ---------------------------------------------------------------------------
// Load and render
// ---------------------------------------------------------------------------

async function loadRequests() {
  if (!adminToken) return;
  try {
    const res = await fetch(`${API}/admin/purchases?status=${currentTab}`, {
      headers: { Authorization: `Bearer ${adminToken}` },
    });
    if (!res.ok) {
      if (res.status === 401 || res.status === 403) doAdminLogout();
      return;
    }
    const json = await res.json();
    renderRequests(json.data.items);
    document.getElementById('refreshStatus').textContent =
      `Refreshed ${new Date().toLocaleTimeString()}`;
  } catch (_) {}
}

function renderRequests(items) {
  const list = document.getElementById('requestsList');
  if (!items || items.length === 0) {
    list.innerHTML = '<div class="empty-state">No ' + currentTab + ' requests.</div>';
    return;
  }

  list.innerHTML = '';
  for (const item of items) {
    list.appendChild(buildCard(item));
  }
}

function buildCard(item) {
  const card = document.createElement('div');
  card.className = 'request-card';
  card.id = `card-${item.purchase_request_id}`;

  const waitText = item.wait_minutes < 2
    ? 'Just now'
    : item.wait_minutes < 60
      ? `Waiting ${item.wait_minutes} min`
      : `Waiting ${Math.round(item.wait_minutes / 60)}h`;

  const statusBadge = `<span class="status-badge status-${item.status}">${item.status}</span>`;
  const isPending = item.status === 'pending';

  // Track rows
  const trackRows = (item.line_items || []).map(t =>
    `<div class="track-row">
      <div>
        <div class="track-name">${esc(t.track_name)}</div>
        <div class="track-meta">${esc(t.artist_name)} — ${esc(t.album_title)}</div>
      </div>
      <div style="color:var(--muted)">$${parseFloat(t.UnitPrice || t.unit_price || 0.99).toFixed(2)}</div>
    </div>`
  ).join('');

  // Actions area
  let actionsHtml = '';
  if (isPending) {
    actionsHtml = `
      <div class="card-actions">
        <button class="btn-green" onclick="decide(${item.purchase_request_id},'approved','')">Approve</button>
        <button class="btn-red"   onclick="toggleDeny(${item.purchase_request_id})">Deny</button>
        <input class="deny-reason-input" id="reason-${item.purchase_request_id}"
               placeholder="Reason for denial (optional)"
               onkeydown="if(event.key==='Enter')decide(${item.purchase_request_id},'denied',this.value)">
        <button class="btn-red" id="confirm-deny-${item.purchase_request_id}"
                style="display:none" onclick="decideDeny(${item.purchase_request_id})">Confirm deny</button>
      </div>`;
  } else if (item.status === 'denied' && item.denial_reason) {
    actionsHtml = `<div style="color:var(--muted);font-size:0.82rem;">Reason: ${esc(item.denial_reason)}</div>`;
  } else if (item.status === 'completed') {
    actionsHtml = `<div style="color:var(--muted);font-size:0.82rem;">Invoice #${item.invoice_id}</div>`;
  }

  card.innerHTML = `
    <div class="card-header" onclick="toggleCard(${item.purchase_request_id})">
      <div class="card-meta">
        <div class="customer-name">${esc(item.customer_name)}</div>
        <div class="customer-email">${esc(item.customer_email)}</div>
        <div class="track-summary">${item.track_count} track${item.track_count !== 1 ? 's' : ''} · ${statusBadge}</div>
      </div>
      <div class="card-right">
        <div class="total">$${parseFloat(item.total_usd).toFixed(2)}</div>
        <div class="wait">${waitText}</div>
      </div>
    </div>
    <div class="card-body" id="body-${item.purchase_request_id}">
      <div class="track-list">${trackRows}</div>
      ${actionsHtml}
    </div>`;

  return card;
}

function toggleCard(id) {
  const body = document.getElementById(`body-${id}`);
  body.classList.toggle('open');
}

function toggleDeny(id) {
  const input = document.getElementById(`reason-${id}`);
  const btn = document.getElementById(`confirm-deny-${id}`);
  const visible = input.classList.contains('visible');
  input.classList.toggle('visible', !visible);
  btn.style.display = visible ? 'none' : '';
  if (!visible) input.focus();
}

function decideDeny(id) {
  const reason = document.getElementById(`reason-${id}`).value.trim();
  decide(id, 'denied', reason);
}

async function decide(id, decision, denial_reason) {
  const card = document.getElementById(`card-${id}`);
  if (card) card.style.opacity = '0.5';

  try {
    const res = await fetch(`${API}/admin/purchases/${id}/decision`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${adminToken}`,
      },
      body: JSON.stringify({ decision, denial_reason }),
    });
    const json = await res.json();
    if (!res.ok) {
      alert(json.detail || 'Error processing decision');
      if (card) card.style.opacity = '1';
      return;
    }
    // Remove card from pending view
    if (card) card.remove();
    // Refresh to get updated counts
    await loadRequests();
  } catch (e) {
    alert('Network error');
    if (card) card.style.opacity = '1';
  }
}

// ---------------------------------------------------------------------------
// Util
// ---------------------------------------------------------------------------

function esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(function init() {
  const saved = sessionStorage.getItem('adminToken');
  const email = sessionStorage.getItem('adminEmail');
  if (saved && email) {
    adminToken = saved;
    enterAdmin(email);
  }
})();
