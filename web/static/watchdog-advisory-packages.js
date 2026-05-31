window.oreonAdvisoryPackagePicker = function(opts) {
  var api = opts.api;
  var nameSearchEl = opts.nameSearchEl;
  var nameSelectEl = opts.nameSelectEl;
  var versionSelectEl = opts.versionSelectEl;
  var hiddenNameEl = opts.hiddenNameEl;
  var hiddenVersionEl = opts.hiddenVersionEl;
  var statusEl = opts.statusEl || null;
  var debounceMs = opts.debounceMs || 250;

  var trackedRows = [];
  var debounceTimer = null;

  function setStatus(msg) {
    if (statusEl) statusEl.textContent = msg || '';
  }

  function syncHidden() {
    if (hiddenNameEl) hiddenNameEl.value = nameSelectEl.value || '';
    if (hiddenVersionEl) hiddenVersionEl.value = versionSelectEl.value || '';
  }

  function fillVersionsForName(name) {
    versionSelectEl.innerHTML = '';
    if (!name) {
      versionSelectEl.disabled = true;
      syncHidden();
      return;
    }
    var vers = [];
    var seen = {};
    trackedRows.forEach(function(r) {
      if (r.package_name !== name || !r.package_version) return;
      if (seen[r.package_version]) return;
      seen[r.package_version] = true;
      vers.push(r.package_version);
    });
    vers.sort();
    if (!vers.length) {
      var o = document.createElement('option');
      o.value = '';
      o.textContent = '(no versions in inventory)';
      versionSelectEl.appendChild(o);
      versionSelectEl.disabled = true;
      syncHidden();
      return;
    }
    vers.forEach(function(v) {
      var opt = document.createElement('option');
      opt.value = v;
      opt.textContent = v;
      versionSelectEl.appendChild(opt);
    });
    versionSelectEl.disabled = false;
    if (vers.length === 1) versionSelectEl.selectedIndex = 0;
    syncHidden();
  }

  function fillNameSelect(names, preselect) {
    nameSelectEl.innerHTML = '';
    if (!names.length) {
      var empty = document.createElement('option');
      empty.value = '';
      empty.textContent = '(no matches)';
      nameSelectEl.appendChild(empty);
      nameSelectEl.disabled = true;
      fillVersionsForName('');
      return;
    }
    names.forEach(function(n) {
      var opt = document.createElement('option');
      opt.value = n;
      opt.textContent = n;
      nameSelectEl.appendChild(opt);
    });
    nameSelectEl.disabled = false;
    if (preselect && names.indexOf(preselect) >= 0) {
      nameSelectEl.value = preselect;
    } else if (names.length === 1) {
      nameSelectEl.selectedIndex = 0;
    }
    fillVersionsForName(nameSelectEl.value || '');
  }

  function loadTracked(search, preselectName, preselectVersion) {
    var q = (search || '').trim() || (preselectName || '').trim();
    var path = '/security/tracked-packages?limit=200&offset=0';
    if (q) path += '&search=' + encodeURIComponent(q);
    setStatus('Loading packages...');
    return api.get(path).then(function(res) {
      trackedRows = (res && res.items) ? res.items : [];
      var names = [];
      var seen = {};
      trackedRows.forEach(function(r) {
        var n = r.package_name;
        if (!n || seen[n]) return;
        seen[n] = true;
        names.push(n);
      });
      names.sort(function(a, b) { return a.localeCompare(b); });
      fillNameSelect(names, preselectName || '');
      if (preselectVersion && versionSelectEl) {
        versionSelectEl.value = preselectVersion;
        syncHidden();
      }
      setStatus(names.length ? (names.length + ' package(s) found') : 'No packages match');
    }).catch(function(e) {
      setStatus('Package search failed: ' + (window.oreonErrorMessage ? window.oreonErrorMessage(e) : e));
      fillNameSelect([], '');
    });
  }

  nameSearchEl.addEventListener('input', function() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function() {
      loadTracked(nameSearchEl.value, nameSelectEl.value || '');
    }, debounceMs);
  });

  nameSelectEl.addEventListener('change', function() {
    fillVersionsForName(nameSelectEl.value || '');
  });

  versionSelectEl.addEventListener('change', syncHidden);

  return {
    load: loadTracked,
    getSelected: function() {
      return {
        package_name: (hiddenNameEl && hiddenNameEl.value) || nameSelectEl.value || '',
        package_version: (hiddenVersionEl && hiddenVersionEl.value) || versionSelectEl.value || ''
      };
    }
  };
};
