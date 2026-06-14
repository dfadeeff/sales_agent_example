'use strict';

const API = '';  // same origin
let token = null;

// ---------------------------------------------------------------------------
// Auth state
// ---------------------------------------------------------------------------

function showLogin() {
  document.getElementById('loginForm').style.display = 'block';
  document.getElementById('registerForm').style.display = 'none';
}
function showRegister() {
  document.getElementById('loginForm').style.display = 'none';
  document.getElementById('registerForm').style.display = 'block';
}

async function doLogin() {
  const email = document.getElementById('loginEmail').value.trim();
  const password = document.getElementById('loginPassword').value;
  const errEl = document.getElementById('loginError');
  errEl.textContent = '';
  const btn = document.getElementById('loginBtn');
  btn.disabled = true;

  try {
    const res = await fetch(`${API}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const json = await res.json();
    if (!res.ok) { errEl.textContent = json.detail || 'Login failed'; return; }

    const { token: tok, role } = json.data;
    if (role !== 'customer') { errEl.textContent = 'This portal is for customers. Admins use /admin.html'; return; }

    token = tok;
    sessionStorage.setItem('token', tok);
    sessionStorage.setItem('email', email);
    enterChat(email);
  } catch (e) {
    errEl.textContent = 'Network error. Is the server running?';
  } finally {
    btn.disabled = false;
  }
}

async function doRegister() {
  const errEl = document.getElementById('registerError');
  errEl.textContent = '';
  const btn = document.getElementById('registerBtn');
  btn.disabled = true;

  const body = {
    email: document.getElementById('regEmail').value.trim(),
    password: document.getElementById('regPassword').value,
    first_name: document.getElementById('regFirst').value.trim(),
    last_name: document.getElementById('regLast').value.trim(),
    city: document.getElementById('regCity').value.trim(),
    country: document.getElementById('regCountry').value.trim(),
    phone: document.getElementById('regPhone').value.trim(),
  };
  if (!body.first_name || !body.last_name || !body.email || !body.password) {
    errEl.textContent = 'Please fill in the required fields.';
    btn.disabled = false;
    return;
  }

  try {
    const res = await fetch(`${API}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let json;
    try { json = await res.json(); } catch (_) { json = {}; }
    if (!res.ok) {
      errEl.textContent = json.detail || `Registration failed (${res.status})`;
      return;
    }
    token = json.data.token;
    sessionStorage.setItem('token', token);
    sessionStorage.setItem('email', body.email);
    enterChat(body.email);
  } catch (e) {
    errEl.textContent = 'Cannot reach server. Is it running on port 8000?';
  } finally {
    btn.disabled = false;
  }
}

function doLogout() {
  token = null;
  sessionStorage.clear();
  clearInterval(notifyTimer);
  notifyTimer = null;
  document.getElementById('authOverlay').style.display = 'flex';
  document.getElementById('chatLayout').style.display = 'none';
  document.getElementById('chatMessages').innerHTML =
    '<div class="message assistant">Hi! I\'m your Marble vinyl store assistant. Ask me about artists, albums, or tracks — or tell me what you\'d like to buy.</div>';
  document.getElementById('loginEmail').value = '';
  document.getElementById('loginPassword').value = '';
  showLogin();
}

function enterChat(email) {
  document.getElementById('authOverlay').style.display = 'none';
  document.getElementById('chatLayout').style.display = 'flex';
  document.getElementById('userLabel').textContent = email;
  loadHistory();
  // Start polling for proactive approval/denial notifications
  clearInterval(notifyTimer);
  notifyTimer = setInterval(pollNotifications, 5000);
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

async function loadHistory() {
  if (!token) return;
  try {
    const res = await fetch(`${API}/chat/history`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return;
    const json = await res.json();
    const msgs = json.data.messages;
    if (!msgs || msgs.length === 0) return;

    const container = document.getElementById('chatMessages');
    // Clear default welcome
    container.innerHTML = '';
    for (const m of msgs) {
      if (m.role === 'user' || m.role === 'assistant') {
        appendMessage(m.role, m.content || '');
      }
    }
    scrollBottom();
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// Sending a message (UI entry point)
// ---------------------------------------------------------------------------

let isSending = false;

async function sendMessage() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text || isSending) return;
  input.value = '';
  input.style.height = '44px';
  await sendMessageText(text);
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function autoResize(el) {
  el.style.height = '44px';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

function appendMessage(role, text) {
  const el = document.createElement('div');
  el.className = `message ${role}`;
  el.textContent = text;
  document.getElementById('chatMessages').appendChild(el);
}

function showTyping(visible) {
  document.getElementById('typingIndicator').style.display = visible ? 'block' : 'none';
}

function scrollBottom() {
  const c = document.getElementById('chatMessages');
  c.scrollTop = c.scrollHeight;
}

// ---------------------------------------------------------------------------
// Proactive notifications — poll every 5 s for approvals / denials
// ---------------------------------------------------------------------------

let notifyTimer = null;

async function pollNotifications() {
  if (!token) return;
  try {
    const res = await fetch(`${API}/chat/notifications`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return;
    const json = await res.json();
    const { approvals, denials } = json.data;

    for (const appr of (approvals || [])) {
      showToast(`Your order ($${appr.total_usd.toFixed(2)}) has been approved! 🎉`, 'success');
      showCheckoutForm(appr.purchase_request_id, appr.total_usd);
    }

    for (const denial of (denials || [])) {
      const reason = denial.denial_reason ? ` Reason: "${denial.denial_reason}"` : '';
      showToast(`Your purchase request was declined.${reason}`, 'error');
      // Let the agent deliver the denial message in-conversation
      await _autoTrigger(
        `My purchase request #${denial.purchase_request_id} was just declined by the admin.` +
        (denial.denial_reason ? ` The reason given was: ${denial.denial_reason}.` : '') +
        ' Please acknowledge this and offer to help me find something else.'
      );
    }
  } catch (_) {}
}

async function _autoTrigger(systemMessage) {
  if (isSending) return;
  await sendMessageText(systemMessage);
}

// ── Checkout form ──────────────────────────────────────────

function showCheckoutForm(purchaseRequestId, totalUsd) {
  const container = document.getElementById('chatMessages');
  const card = document.createElement('div');
  card.className = 'checkout-card';
  card.id = `checkout-${purchaseRequestId}`;
  card.innerHTML = `
    <div class="checkout-card-header">🎉 Order Approved — Enter Shipping Details</div>
    <div class="checkout-card-body">
      <div class="form-group">
        <label>Street Address *</label>
        <input type="text" id="co-addr-${purchaseRequestId}" placeholder="42 Abbey Road">
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>City *</label>
          <input type="text" id="co-city-${purchaseRequestId}" placeholder="London">
        </div>
        <div class="form-group">
          <label>State / Province</label>
          <input type="text" id="co-state-${purchaseRequestId}" placeholder="Optional">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Country *</label>
          <input type="text" id="co-country-${purchaseRequestId}" placeholder="United Kingdom">
        </div>
        <div class="form-group">
          <label>Postal Code</label>
          <input type="text" id="co-postal-${purchaseRequestId}" placeholder="SW1A 1AA">
        </div>
      </div>
      <div class="error-msg" id="co-err-${purchaseRequestId}"></div>
      <button class="btn-primary btn-full" id="co-btn-${purchaseRequestId}"
              onclick="submitCheckout(${purchaseRequestId}, ${totalUsd})">
        Complete Purchase — $${totalUsd.toFixed(2)}
      </button>
    </div>`;
  container.appendChild(card);
  scrollBottom();
  document.getElementById(`co-addr-${purchaseRequestId}`).focus();
}

async function submitCheckout(purchaseRequestId, totalUsd) {
  const btn = document.getElementById(`co-btn-${purchaseRequestId}`);
  const errEl = document.getElementById(`co-err-${purchaseRequestId}`);
  const address  = document.getElementById(`co-addr-${purchaseRequestId}`).value.trim();
  const city     = document.getElementById(`co-city-${purchaseRequestId}`).value.trim();
  const state    = document.getElementById(`co-state-${purchaseRequestId}`).value.trim();
  const country  = document.getElementById(`co-country-${purchaseRequestId}`).value.trim();
  const postal   = document.getElementById(`co-postal-${purchaseRequestId}`).value.trim();

  if (!address || !city || !country) {
    errEl.textContent = 'Please fill in Address, City, and Country.';
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Processing…';
  errEl.textContent = '';

  try {
    const res = await fetch(`${API}/checkout/${purchaseRequestId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        billing_address: address,
        billing_city: city,
        billing_state: state,
        billing_country: country,
        billing_postal_code: postal,
      }),
    });
    const json = await res.json();
    if (!res.ok) {
      errEl.textContent = json.detail || 'Checkout failed.';
      btn.disabled = false;
      btn.textContent = `Complete Purchase — $${totalUsd.toFixed(2)}`;
      return;
    }
    const inv = json.data;
    // Replace the form with a receipt
    const card = document.getElementById(`checkout-${purchaseRequestId}`);
    card.outerHTML = `
      <div class="receipt-card">
        <h3>✅ Purchase Complete!</h3>
        <p>
          Invoice <strong>#${inv.invoice_id}</strong><br>
          ${inv.track_count} track(s) · ${address}, ${city}${state ? ', ' + state : ''}, ${country} ${postal}
        </p>
        <div class="receipt-total">$${inv.total_usd.toFixed(2)}</div>
      </div>`;
    scrollBottom();
    showToast('Invoice created — enjoy the music! 🎸', 'success');
  } catch (e) {
    errEl.textContent = 'Network error. Please try again.';
    btn.disabled = false;
    btn.textContent = `Complete Purchase — $${totalUsd.toFixed(2)}`;
  }
}

function showToast(text, type) {
  const toast = document.createElement('div');
  toast.style.cssText = `
    position: fixed; bottom: 1.5rem; right: 1.5rem; z-index: 999;
    background: ${type === 'success' ? 'var(--green)' : 'var(--red)'};
    color: #fff; padding: 0.75rem 1.1rem; border-radius: 8px;
    font-size: 0.88rem; max-width: 320px; line-height: 1.4;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    animation: slideIn 0.25s ease;
  `;
  toast.textContent = text;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 6000);
}

// Refactored sendMessage to allow programmatic calls
async function sendMessageText(text) {
  if (isSending || !text) return;
  isSending = true;
  document.getElementById('sendBtn').disabled = true;

  appendMessage('user', text);
  showTyping(true);
  scrollBottom();

  const assistantEl = document.createElement('div');
  assistantEl.className = 'message assistant';
  let assistantText = '';

  try {
    const res = await fetch(`${API}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message: text }),
    });
    if (!res.ok) {
      showTyping(false);
      const err = await res.json().catch(() => ({}));
      appendMessage('assistant', `Error: ${err.detail || res.statusText}`);
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let firstChunk = true;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (!raw) continue;
        try {
          const payload = JSON.parse(raw);
          if (payload.text) {
            if (firstChunk) {
              showTyping(false);
              document.getElementById('chatMessages').appendChild(assistantEl);
              firstChunk = false;
            }
            assistantText += payload.text;
            assistantEl.textContent = assistantText;
            scrollBottom();
          }
          if (payload.error) {
            showTyping(false);
            if (firstChunk) appendMessage('assistant', `Something went wrong: ${payload.error}`);
          }
        } catch (_) {}
      }
    }
  } catch (e) {
    showTyping(false);
    appendMessage('assistant', 'Connection error. Please try again.');
  } finally {
    isSending = false;
    document.getElementById('sendBtn').disabled = false;
    showTyping(false);
    scrollBottom();
  }
}

// ---------------------------------------------------------------------------
// Init: restore session
// ---------------------------------------------------------------------------

(function init() {
  const saved = sessionStorage.getItem('token');
  const email = sessionStorage.getItem('email');
  if (saved && email) {
    token = saved;
    enterChat(email);
  }
})();
