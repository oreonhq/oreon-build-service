(function() {
  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : s;
    return d.innerHTML;
  }

  function dedupeReleases(items) {
    var seenId = {};
    var seenName = {};
    var out = [];
    for (var i = 0; i < (items || []).length; i++) {
      var r = items[i];
      var idStr = String(r.id);
      var nameKey = ((r.releasename || '') + '').trim().toLowerCase();
      if (!nameKey || seenId[idStr] || seenName[nameKey]) continue;
      seenId[idStr] = true;
      seenName[nameKey] = true;
      out.push(r);
    }
    return out;
  }

  function render(container, releases, selectedIds) {
    if (!container) return;
    var sel = {};
    (selectedIds || []).forEach(function(id) {
      sel[String(id)] = true;
    });
    var list = dedupeReleases(releases);
    if (!list.length) {
      container.innerHTML = '<div class="advisory-release-empty">No releases.</div>';
      return;
    }
    container.innerHTML = list.map(function(r) {
      var id = String(r.id);
      var checked = sel[id] ? ' checked' : '';
      return '<label class="advisory-release-option">'
        + '<input type="checkbox" value="' + esc(id) + '"' + checked + '>'
        + '<span>' + esc(r.releasename || ('#' + r.id)) + '</span>'
        + '</label>';
    }).join('');
  }

  function getSelectedIds(container) {
    if (!container) return [];
    var ids = [];
    container.querySelectorAll('input[type="checkbox"]:checked').forEach(function(cb) {
      var n = parseInt(cb.value, 10);
      if (Number.isFinite(n)) ids.push(n);
    });
    return ids;
  }

  window.oreonReleaseCheckboxes = {
    dedupeReleases: dedupeReleases,
    render: render,
    getSelectedIds: getSelectedIds
  };
})();
