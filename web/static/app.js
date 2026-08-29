

function oreonParseJwtPayload(token) {
  if (!token || typeof token !== 'string') return null;
  var parts = token.split('.');
  if (parts.length !== 3) return null;
  try {
    var raw = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    while (raw.length % 4 !== 0) raw += '=';
    var json = atob(raw);
    return JSON.parse(json);
  } catch (e) {
    return null;
  }
}

function oreonNormalizeApiErrorDetail(detail, status, statusText) {
  var st = (statusText && String(statusText).trim()) ? String(statusText).trim() : '';
  var code = typeof status === 'number' && status > 0 ? status : 0;
  var fallback = st || (code ? 'HTTP ' + code : 'Request failed');
  if (detail == null || detail === '') return fallback;
  if (typeof detail === 'string') {
    var s = detail.trim();
    return s || fallback;
  }
  if (Array.isArray(detail)) {
    var parts = [];
    for (var i = 0; i < detail.length; i++) {
      var x = detail[i];
      if (x == null) continue;
      if (typeof x === 'string') parts.push(x);
      else if (typeof x.msg === 'string') parts.push(x.msg);
      else {
        try {
          parts.push(JSON.stringify(x));
        } catch (_) {
          parts.push(String(x));
        }
      }
    }
    var joined = parts.join('; ').trim();
    return joined || fallback;
  }
  if (typeof detail === 'object') {
    if (typeof detail.msg === 'string' && detail.msg.trim()) return detail.msg.trim();
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message.trim();
    try {
      return JSON.stringify(detail);
    } catch (_) {
      return fallback;
    }
  }
  var asStr = String(detail).trim();
  return asStr || fallback;
}

window.oreonErrorMessage = function(e) {
  if (!e) return 'Unknown error';
  var m = e.message;
  if (m != null && String(m).trim() !== '') return String(m).trim();
  if (typeof e === 'string' && e.trim()) return e.trim();
  try {
    return JSON.stringify(e);
  } catch (_) {
    return String(e) || 'Unknown error';
  }
};

function oreonLoadStoredAuth(api) {
  api.token = localStorage.getItem('oreon_token') || '';
  api.username = localStorage.getItem('oreon_username') || '';
  api.role = localStorage.getItem('oreon_role') || '';
  if (api.token && !api.role) {
    var p = oreonParseJwtPayload(api.token);
    if (p && p.role) api.role = p.role;
  }
}

window.oreonApi = {
  base: (typeof API_BASE !== 'undefined' ? API_BASE : '') || '/api',
  token: '',
  username: '',
  role: '',
  setToken(t) {
    this.token = t || '';
    if (t) {
      localStorage.setItem('oreon_token', t);
    } else {
      localStorage.removeItem('oreon_token');
      localStorage.removeItem('oreon_role');
      localStorage.removeItem('oreon_username');
      this.role = '';
      this.username = '';
    }
  },
  afterLogin(data) {
    if (!data || !data.access_token) return;
    this.token = data.access_token;
    localStorage.setItem('oreon_token', data.access_token);
    this.username = (data.username != null && data.username !== '') ? data.username : '';
    this.role = (data.role != null && data.role !== '') ? data.role : '';
    if (this.username) localStorage.setItem('oreon_username', this.username);
    else localStorage.removeItem('oreon_username');
    if (this.role) localStorage.setItem('oreon_role', this.role);
    else {
      var p = oreonParseJwtPayload(this.token);
      if (p && p.role) {
        this.role = p.role;
        localStorage.setItem('oreon_role', this.role);
      } else localStorage.removeItem('oreon_role');
    }
  },
  isAdmin() {
    return this.role === 'admin';
  },
    async request(method, path, body) {
    const opts = { method, headers: {}, cache: 'no-store' };
    if (this.token) opts.headers['Authorization'] = 'Bearer ' + this.token;
    if (method === 'GET') {
      opts.headers['Cache-Control'] = 'no-cache';
      opts.headers['Pragma'] = 'no-cache';
    }
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
      const errBody = await r.json().catch(() => ({}));
      const raw = errBody && errBody.detail !== undefined ? errBody.detail : errBody && errBody.message;
      const msg = oreonNormalizeApiErrorDetail(raw, r.status, r.statusText);
      throw new Error(msg);
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

oreonLoadStoredAuth(window.oreonApi);

window.oreonBindTableSelection = function(tbody, opts) {
  if (!tbody || !opts) return { refresh: function() {}, getSelectedIds: function() { return []; } };
  var rowSel = opts.rowCheckboxSelector || '.row-cb';
  var selectAll = opts.selectAll;
  var bulkBar = opts.bulkBar;
  var onSelectionChange = opts.onSelectionChange;

  function getSelectedIds() {
    var ids = [];
    tbody.querySelectorAll(rowSel + ':checked').forEach(function(cb) {
      var id = cb.getAttribute('data-id');
      if (id != null && id !== '') ids.push(id);
    });
    return ids;
  }

  function refresh() {
    var ids = getSelectedIds();
    if (selectAll) {
      var all = tbody.querySelectorAll(rowSel);
      var n = all.length;
      var c = tbody.querySelectorAll(rowSel + ':checked').length;
      selectAll.checked = n > 0 && c === n;
    }
    if (bulkBar) {
      if (ids.length === 0) {
        bulkBar.classList.remove('is-visible');
      } else {
        bulkBar.classList.add('is-visible');
        var cnt = bulkBar.querySelector('.bulk-selection-count');
        if (cnt) cnt.textContent = ids.length + ' selected';
        var btns = bulkBar.querySelector('.bulk-selection-buttons');
        if (btns) {
          btns.innerHTML = '';
          if (onSelectionChange) onSelectionChange(ids, btns);
        }
      }
    }
  }

  tbody.addEventListener('change', function(e) {
    if (e.target && e.target.matches && e.target.matches(rowSel)) refresh();
  });
  if (selectAll) {
    selectAll.addEventListener('change', function() {
      tbody.querySelectorAll(rowSel).forEach(function(cb) { cb.checked = selectAll.checked; });
      refresh();
    });
  }

  return { refresh: refresh, getSelectedIds: getSelectedIds };
};

window.oreonWithLoading = function(btn, asyncFn) {
  if (!btn) return asyncFn();
  var orig = btn.textContent;
  btn.disabled = true;
  btn.classList.add('loading');
  btn.textContent = orig + ' ';
  return asyncFn().finally(function() {
    btn.disabled = false;
    btn.classList.remove('loading');
    btn.textContent = orig;
  });
};

window.oreonDebounce = function(fn, ms) {
  var t;
  return function() {
    var ctx = this;
    var args = arguments;
    clearTimeout(t);
    t = setTimeout(function() {
      fn.apply(ctx, args);
    }, ms);
  };
};

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
  if (userInfo) {
    if (token) {
      var u = window.oreonApi.username || 'user';
      var r = window.oreonApi.role || '';
      userInfo.textContent = r ? '(' + u + ', ' + r + ')' : '(' + u + ')';
    } else userInfo.textContent = '';
  }
}

var OREON_NAV_ITEMS = [
  { href: '/', label: 'Dashboard', match: function(p) { return p === '/' || p === '/index.html'; } },
  { href: '/builds.html', label: 'Builds', match: function(p) { return p === '/builds.html' || p === '/build.html'; } },
  { href: '/side-tags.html', label: 'Side Tags', match: function(p) { return p === '/side-tags.html' || p === '/side-tag.html'; } },
  { href: '/failures.html', label: 'Failures', match: function(p) { return p === '/failures.html'; } },
  { href: '/packages.html', label: 'Packages', match: function(p) { return p === '/packages.html'; } },
  { href: '/releases.html', label: 'Releases', match: function(p) { return p === '/releases.html'; } },
  { href: '/workers.html', label: 'Workers', match: function(p) { return p === '/workers.html'; } },
  { href: '/mock-environments.html', label: 'Mock Environments', match: function(p) { return p === '/mock-environments.html'; } },
  { href: '/repos.html', label: 'Repositories', match: function(p) { return p === '/repos.html'; } },
  { href: '/promotions.html', label: 'Promotions', match: function(p) { return p === '/promotions.html'; } },
  { href: '/admin.html', label: 'Administration', match: function(p) { return p === '/admin.html'; } },
];

function oreonNavbarShouldSkip(header) {
  if (!header) return true;
  if (header.getAttribute('data-oreon-nav-skip') === '1') return true;
  var logo = header.querySelector('.header-logo');
  var src = logo && logo.getAttribute('src') ? logo.getAttribute('src') : '';
  if (src.indexOf('owdlogo') >= 0) return true;
  return false;
}

function oreonNavbarSearchQuery() {
  var el = document.getElementById('nav-search-q');
  if (el && el.value && String(el.value).trim()) return String(el.value).trim();
  try {
    return new URLSearchParams(window.location.search).get('q') || '';
  } catch (_) {
    return '';
  }
}

function oreonInitNavbar() {
  var header = document.querySelector('header.header');
  if (!header || oreonNavbarShouldSkip(header)) return;
  var nav = header.querySelector('nav');
  if (!nav) return;

  var path = window.location.pathname || '/';
  var searchQ = oreonNavbarSearchQuery();
  var parts = [];
  for (var i = 0; i < OREON_NAV_ITEMS.length; i++) {
    var item = OREON_NAV_ITEMS[i];
    if (item.auth && !(window.oreonApi && window.oreonApi.token)) continue;
    var active = item.match(path);
    parts.push(
      '<a href="' + item.href + '"' +
      (active ? ' class="active" aria-current="page"' : '') +
      '>' + item.label + '</a>'
    );
  }
  var qAttr = searchQ.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  parts.push(
    '<form class="navbar-search" action="/search.html" method="get">' +
    '<input type="search" name="q" id="nav-search-q" placeholder="Search" value="' + qAttr + '" autocomplete="off">' +
    '<button type="submit" class="btn">Go</button></form>'
  );
  parts.push(
    '<span id="auth-section">' +
    '<span id="user-info"></span>' +
    '<button id="btn-login" type="button">Login</button>' +
    '<button id="btn-logout" type="button" style="display:none">Logout</button>' +
    '</span>'
  );
  nav.innerHTML = parts.join('');
}

window.oreonInitNavbar = oreonInitNavbar;

window.oreonPrefillUrlSearch = function(inputId, paramNames) {
  var names = paramNames || ['q', 'search'];
  var val = '';
  try {
    var params = new URLSearchParams(window.location.search);
    for (var i = 0; i < names.length; i++) {
      var v = params.get(names[i]);
      if (v && String(v).trim()) {
        val = String(v).trim();
        break;
      }
    }
  } catch (_) {}
  if (!val) return '';
  var el = document.getElementById(inputId);
  if (el) el.value = val;
  return val;
};

function oreonSigningKeyHtml(signingStatus) {
  var ss = (signingStatus || 'not_started').toLowerCase();
  if (ss === 'uploading') {
    ss = 'running';
  }
  var c = '#94a3b8';
  var label = 'Pending';
  var tip =
    'Signing: waiting for the build to finish (mock still running or worker has not notified yet).';
  if (ss === 'running') {
    c = '#3b82f6';
    label = 'Running';
    tip =
      'Signing in progress (RPM upload, GPG signing, R2, repodata) after mock succeeded.';
  } else if (ss === 'signed') {
    c = '#22c55e';
    label = 'Complete';
    tip = 'Signing finished (GPG, repo publish, and finalization).';
  } else if (ss === 'failed') {
    c = '#ef4444';
    label = 'Failed';
    tip = 'Signing failed during upload, GPG signing, R2, or repodata.';
  }
  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/"/g, '&quot;');
  }
  return (
    '<span class="oreon-controller-key" title="' +
    esc(tip) +
    '" style="display:inline-flex;align-items:center;gap:0.35rem;">' +
    '<i class="fa-solid fa-key" aria-hidden="true" style="color:' +
    c +
    ';"></i>' +
    '<span style="font-size:0.85rem;color:var(--text-muted);">' +
    esc(label) +
    '</span></span>'
  );
}

function oreonJobDisplayStatus(job) {
  if (!job) return '';
  var d = job.display_status;
  if (d != null && String(d).trim() !== '') return String(d).trim().toLowerCase();
  return String(job.status || '').toLowerCase();
}

function oreonControllerKeyHtml(signingStatus) {
  return oreonSigningKeyHtml(signingStatus);
}
window.oreonSigningKeyHtml = oreonSigningKeyHtml;
window.oreonControllerKeyHtml = oreonControllerKeyHtml;
window.oreonJobDisplayStatus = oreonJobDisplayStatus;

document.addEventListener('DOMContentLoaded', function() {
  oreonInitNavbar();
  oreonLoadStoredAuth(window.oreonApi);
  updateAuthUI();
  window.addEventListener('auth-change', function() {
    oreonLoadStoredAuth(window.oreonApi);
    updateAuthUI();
    oreonInitNavbar();
  });
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
            window.oreonApi.afterLogin(data);
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
