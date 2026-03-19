/* Oreon Build Service - frontend helpers */
window.oreonApi = {
  base: (typeof API_BASE !== 'undefined' ? API_BASE : '') || '/api',
  token: localStorage.getItem('oreon_token') || '',
  setToken(t) {
    this.token = t || '';
    if (t) localStorage.setItem('oreon_token', t);
    else localStorage.removeItem('oreon_token');
  },
  async request(method, path, body) {
    const opts = { method, headers: {} };
    if (this.token) opts.headers['Authorization'] = 'Bearer ' + this.token;
    if (body && typeof body === 'object' && !(body instanceof FormData)) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    } else if (body) opts.body = body;
    const r = await fetch(this.base + path, opts);
    if (r.status === 401) {
      this.setToken(null);
      window.dispatchEvent(new Event('auth-change'));
    }
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || r.statusText);
    }
    const text = await r.text();
    if (r.status === 204 || !text || !text.trim()) return null;
    const ct = r.headers.get('Content-Type');
    if (ct && ct.includes('application/json')) {
      try { return JSON.parse(text); } catch (_) { return null; }
    }
    return text || null;
  },
  get(path) { return this.request('GET', path); },
  post(path, body) { return this.request('POST', path, body); },
  patch(path, body) { return this.request('PATCH', path, body); },
  delete(path) { return this.request('DELETE', path); },
};

// Lightweight UI helpers for modals / confirmations
window.oreonUI = (function() {
  let confirmResolve = null;
  function ensureShell() {
    if (document.getElementById('global-confirm-modal')) return;
    const wrapper = document.createElement('div');
    wrapper.innerHTML = ''
      + '<div id="global-confirm-modal" class="modal">'
      + '  <div class="modal-content">'
      + '    <h3 id="confirm-title">Confirm</h3>'
      + '    <p id="confirm-message" style="margin:0 0 1rem;"></p>'
      + '    <div class="modal-actions" style="justify-content:flex-end;">'
      + '      <button type="button" class="btn" id="confirm-cancel-btn">Cancel</button>'
      + '      <button type="button" class="btn primary" id="confirm-ok-btn">OK</button>'
      + '    </div>'
      + '  </div>'
      + '</div>'
      + '<div id="login-modal" class="modal">'
      + '  <div class="modal-content">'
      + '    <h3>Login</h3>'
      + '    <div class="form-group"><label>Username</label><input type="text" id="login-username"></div>'
      + '    <div class="form-group"><label>Password</label><input type="password" id="login-password"></div>'
      + '    <div class="modal-actions" style="justify-content:flex-end;">'
      + '      <button type="button" class="btn" id="login-cancel-btn">Cancel</button>'
      + '      <button type="button" class="btn primary" id="login-ok-btn">Login</button>'
      + '    </div>'
      + '  </div>'
      + '</div>';
    document.body.appendChild(wrapper);
    const cm = document.getElementById('global-confirm-modal');
    const msg = document.getElementById('confirm-message');
    const btnOk = document.getElementById('confirm-ok-btn');
    const btnCancel = document.getElementById('confirm-cancel-btn');
    function close(val) {
      cm.classList.remove('open');
      const r = confirmResolve;
      confirmResolve = null;
      if (r) r(val);
    }
    btnOk.addEventListener('click', function() { close(true); });
    btnCancel.addEventListener('click', function() { close(false); });
    cm.addEventListener('click', function(e) { if (e.target === cm) close(false); });
  }
  function confirm(message, title) {
    ensureShell();
    return new Promise(function(resolve) {
      confirmResolve = resolve;
      const cm = document.getElementById('global-confirm-modal');
      document.getElementById('confirm-title').textContent = title || 'Confirm';
      document.getElementById('confirm-message').textContent = message || '';
      cm.classList.add('open');
    });
  }
  function openLogin() {
    ensureShell();
    const lm = document.getElementById('login-modal');
    const u = document.getElementById('login-username');
    const p = document.getElementById('login-password');
    lm.classList.add('open');
    u.value = '';
    p.value = '';
    u.focus();
  }
  function closeLogin() {
    const lm = document.getElementById('login-modal');
    if (lm) lm.classList.remove('open');
  }
  return { confirm, openLogin, closeLogin };
})();

function updateAuthUI() {
  const token = window.oreonApi.token;
  const btnLogin = document.getElementById('btn-login');
  const btnLogout = document.getElementById('btn-logout');
  const userInfo = document.getElementById('user-info');
  if (btnLogin) btnLogin.style.display = token ? 'none' : 'inline-block';
  if (btnLogout) btnLogout.style.display = token ? 'inline-block' : 'none';
  if (userInfo) userInfo.textContent = token ? '(logged in)' : '';
}

document.addEventListener('DOMContentLoaded', function() {
  updateAuthUI();
  window.addEventListener('auth-change', updateAuthUI);
  const btnLogin = document.getElementById('btn-login');
  const btnLogout = document.getElementById('btn-logout');
  if (btnLogin) {
    btnLogin.addEventListener('click', function() {
      window.oreonUI.openLogin();
      const btnOk = document.getElementById('login-ok-btn');
      const btnCancel = document.getElementById('login-cancel-btn');
      const u = document.getElementById('login-username');
      const p = document.getElementById('login-password');
      function cleanup() {
        btnOk.removeEventListener('click', onOk);
        btnCancel.removeEventListener('click', onCancel);
      }
      function onOk() {
        const user = (u.value || '').trim();
        const pass = (p.value || '').trim();
        if (!user || !pass) return;
        window.oreonApi.post('/auth/login', { username: user, password: pass })
          .then(function(data) {
            window.oreonApi.setToken(data.access_token);
            cleanup();
            window.oreonUI.closeLogin();
            updateAuthUI();
            window.dispatchEvent(new Event('auth-change'));
          })
          .catch(function(e) {
            alert('Login failed: ' + e.message);
          });
      }
      function onCancel() {
        cleanup();
        window.oreonUI.closeLogin();
      }
      btnOk.addEventListener('click', onOk);
      btnCancel.addEventListener('click', onCancel);
    });
  }
  if (btnLogout) {
    btnLogout.addEventListener('click', function() {
      window.oreonApi.setToken(null);
      updateAuthUI();
      window.dispatchEvent(new Event('auth-change'));
    });
  }
});
