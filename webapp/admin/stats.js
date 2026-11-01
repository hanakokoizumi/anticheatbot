(function () {
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) {
    document.body.innerHTML =
      '<p class="status-line err">' +
      (window.WebappI18n ? window.WebappI18n.adminT('zh-Hans', 'errOutsideTg') : '请在 Telegram 内打开') +
      '</p>';
    return;
  }
  if (!window.WebappI18n) {
    document.body.innerHTML = '<p class="status-line err">Missing i18n</p>';
    return;
  }

  tg.ready();
  tg.expand();

  var params = new URLSearchParams(location.search);
  var chatId = params.get('chat_id');
  if (!chatId) {
    document.body.innerHTML =
      '<p class="status-line err">' + WebappI18n.adminT(WebappI18n.getLocale(tg), 'toastSelectChat') + '</p>';
    return;
  }

  var locale = WebappI18n.normalizeLocale(WebappI18n.getLocale(tg));
  var chartDay = null;
  var chartTypes = null;
  var auditEventsCache = [];

  function pad2(n) {
    return n < 10 ? '0' + n : String(n);
  }

  /** UTC 时间展示为 YYYY:MM:DD HH:MM:SS（与表头「UTC」一致） */
  function formatUtcTsDisplay(iso) {
    if (iso == null || String(iso).trim() === '') return '—';
    var d = new Date(String(iso).trim());
    if (isNaN(d.getTime())) return String(iso);
    return (
      d.getUTCFullYear() +
      ':' +
      pad2(d.getUTCMonth() + 1) +
      ':' +
      pad2(d.getUTCDate()) +
      ' ' +
      pad2(d.getUTCHours()) +
      ':' +
      pad2(d.getUTCMinutes()) +
      ':' +
      pad2(d.getUTCSeconds())
    );
  }

  /**
   * 将审计 payload 压平为 { key, value } 行（键为点号路径），用于表格展示。
   */
  function flattenPayload(val, prefix) {
    var rows = [];
    var pfx = prefix == null ? '' : prefix;
    if (val === null || val === undefined) {
      return rows;
    }
    if (typeof val === 'string') {
      var t = val.trim();
      if (
        (t.charAt(0) === '{' && t.charAt(t.length - 1) === '}') ||
        (t.charAt(0) === '[' && t.charAt(t.length - 1) === ']')
      ) {
        try {
          return flattenPayload(JSON.parse(t), pfx);
        } catch (ignore) {
          rows.push({ key: pfx || '(text)', value: val });
          return rows;
        }
      }
      rows.push({ key: pfx || '(text)', value: val });
      return rows;
    }
    if (typeof val === 'number' || typeof val === 'boolean') {
      rows.push({ key: pfx || '(value)', value: String(val) });
      return rows;
    }
    if (typeof val !== 'object') {
      rows.push({ key: pfx || '(value)', value: String(val) });
      return rows;
    }
    if (Array.isArray(val)) {
      if (!val.length) {
        rows.push({ key: pfx || '[]', value: '—' });
        return rows;
      }
      val.forEach(function (item, i) {
        var path = pfx ? pfx + '[' + i + ']' : '[' + i + ']';
        if (item !== null && typeof item === 'object') {
          rows = rows.concat(flattenPayload(item, path));
        } else {
          rows.push({ key: path, value: item == null ? '—' : String(item) });
        }
      });
      return rows;
    }
    var keys = Object.keys(val).sort();
    if (!keys.length) {
      return rows;
    }
    keys.forEach(function (k) {
      var sub = val[k];
      var path = pfx ? pfx + '.' + k : k;
      if (sub !== null && typeof sub === 'object') {
        rows = rows.concat(flattenPayload(sub, path));
      } else {
        rows.push({ key: path, value: sub == null ? '—' : String(sub) });
      }
    });
    return rows;
  }

  function fillPayloadDetailTable(tbody, payload) {
    tbody.innerHTML = '';
    var flat = flattenPayload(payload, '');
    if (!flat.length) {
      var tr0 = document.createElement('tr');
      var td0 = document.createElement('td');
      td0.colSpan = 2;
      td0.className = 'sub';
      td0.style.padding = '0.65rem 0.55rem';
      td0.textContent = at('auditDetailNoPayload');
      tr0.appendChild(td0);
      tbody.appendChild(tr0);
      return;
    }
    flat.forEach(function (r) {
      var tr = document.createElement('tr');
      var th = document.createElement('th');
      th.scope = 'row';
      th.textContent = r.key;
      var td = document.createElement('td');
      td.textContent = r.value;
      tr.appendChild(th);
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
  }

  function openAuditDetailDialog(ev) {
    var dlg = document.getElementById('auditDetailDialog');
    var title = document.getElementById('auditDetailHeading');
    var tbody = document.getElementById('auditDetailKvTbody');
    if (!dlg || !title || !tbody) return;
    title.textContent = af('auditDetailTitleFmt', {
      type: eventLabel(ev.event_type || ''),
      time: formatUtcTsDisplay(ev.ts),
    });
    fillPayloadDetailTable(tbody, ev.payload);
    if (typeof dlg.showModal === 'function') {
      dlg.showModal();
    }
  }

  function wireAuditDetailDialog() {
    var dlg = document.getElementById('auditDetailDialog');
    var closeBtn = document.getElementById('auditDetailCloseBtn');
    var table = document.getElementById('auditTable');
    if (closeBtn && dlg) {
      closeBtn.addEventListener('click', function () {
        dlg.close();
      });
    }
    if (dlg) {
      dlg.addEventListener('click', function (e) {
        if (e.target === dlg) dlg.close();
      });
    }
    if (table) {
      table.addEventListener('click', function (e) {
        var btn = e.target && e.target.closest ? e.target.closest('button[data-ev-idx]') : null;
        if (!btn) return;
        var idx = parseInt(btn.getAttribute('data-ev-idx'), 10);
        if (!Number.isFinite(idx) || !auditEventsCache[idx]) return;
        openAuditDetailDialog(auditEventsCache[idx]);
      });
    }
  }

  function at(k) {
    return WebappI18n.adminT(locale, k);
  }
  function af(k, vars) {
    return WebappI18n.adminFormat(locale, k, vars);
  }

  function headers() {
    return { 'X-Telegram-Init-Data': tg.initData, 'Content-Type': 'application/json' };
  }

  async function api(path, opts) {
    var o = opts || {};
    var r;
    try {
      r = await fetch(path, {
        method: o.method || 'GET',
        headers: Object.assign({}, headers(), o.headers || {}),
        body: o.body,
      });
    } catch (e) {
      throw new Error(at('networkError'));
    }
    var text = await r.text();
    var data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text };
    }
    if (!r.ok) {
      var d = data.detail;
      var msg =
        typeof d === 'string'
          ? d
          : Array.isArray(d)
            ? d
                .map(function (x) {
                  return x.msg || JSON.stringify(x);
                })
                .join('; ')
            : data.message || data.error || text || String(r.status);
      throw new Error(WebappI18n.adminApiError(locale, msg, r.status));
    }
    return data;
  }

  function toast(msg, isErr) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.style.display = 'block';
    el.className = 'toast' + (isErr ? ' toast--err' : '');
    clearTimeout(el._hide);
    el._hide = setTimeout(function () {
      el.style.display = 'none';
    }, 3200);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function eventLabel(etype) {
    var evKey = 'statsEvt_' + etype;
    var lbl = at(evKey);
    return lbl === evKey ? etype : lbl;
  }

  function destroyCharts() {
    if (chartDay) {
      chartDay.destroy();
      chartDay = null;
    }
    if (chartTypes) {
      chartTypes.destroy();
      chartTypes = null;
    }
  }

  function chartTextColor() {
    var v = getComputedStyle(document.documentElement).getPropertyValue('--tg-theme-text-color').trim();
    return v || '#1a1a1a';
  }

  function chartHintColor() {
    var v = getComputedStyle(document.documentElement).getPropertyValue('--tg-theme-hint-color').trim();
    return v || '#6b7280';
  }

  function chartAccent() {
    var v = getComputedStyle(document.documentElement).getPropertyValue('--tg-theme-button-color').trim();
    return v || '#2481cc';
  }

  function syncNav() {
    var back = document.getElementById('linkBackAdmin');
    if (back) back.href = 'group.html?chat_id=' + encodeURIComponent(chatId);
  }

  function refreshPageI18n() {
    WebappI18n.applyAdminLocale(document, locale);
    document.documentElement.lang = locale === 'zh-Hans' ? 'zh-Hans' : locale;
    var sel = document.getElementById('adminUiLocale');
    if (sel) sel.value = locale;
    var t = document.querySelector('title');
    if (t && !t.hasAttribute('data-skip-admin-doc-title')) {
      t.textContent = at('docTitleStats');
    } else if (t) {
      t.textContent = at('docTitleStats');
    }
    var sub = document.getElementById('statsChatSub');
    if (sub) sub.textContent = af('statsSubtitleChat', { chatId: String(chatId) });
  }

  function renderSummary(data) {
    var dl = document.getElementById('statsSummaryDl');
    if (!dl) return;
    dl.innerHTML = '';
    function row(label, value) {
      var dt = document.createElement('dt');
      dt.textContent = label;
      var dd = document.createElement('dd');
      dd.textContent = value;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    row(at('statsSummaryTotal'), String(data.total_events != null ? data.total_events : 0));
    row(at('statsSummaryPeriod'), formatUtcTsDisplay(data.since));
    row(at('lblStatsRange'), String(data.range_days != null ? data.range_days : '—'));
    row(
      af('statsSummaryLogRows', {
        limit: String(data.log_limit != null ? data.log_limit : (data.events || []).length),
      }),
      String((data.events || []).length)
    );
  }

  function renderCharts(data) {
    destroyCharts();
    var ChartCtor = typeof Chart !== 'undefined' ? Chart : null;
    if (!ChartCtor) {
      return;
    }
    var textC = chartTextColor();
    var hintC = chartHintColor();
    var accent = chartAccent();
    var commonOpts = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          labels: { color: textC },
        },
      },
    };

    var daily = data.daily || [];
    var dayLabels = daily.map(function (d) {
      return d.day;
    });
    var dayTotals = daily.map(function (d) {
      var c = d.counts || {};
      var t = 0;
      Object.keys(c).forEach(function (k) {
        t += Number(c[k]) || 0;
      });
      return t;
    });
    var cDaily = document.getElementById('chartDaily');
    if (cDaily && dayLabels.length) {
      chartDay = new ChartCtor(cDaily, {
        type: 'line',
        data: {
          labels: dayLabels,
          datasets: [
            {
              label: at('chartDailySeries'),
              data: dayTotals,
              borderColor: accent,
              backgroundColor: 'rgba(36, 129, 204, 0.12)',
              tension: 0.25,
              fill: true,
            },
          ],
        },
        options: Object.assign({}, commonOpts, {
          scales: {
            x: { ticks: { color: hintC, maxRotation: 45, minRotation: 0 } },
            y: { beginAtZero: true, ticks: { color: hintC, precision: 0 } },
          },
        }),
      });
    }

    var counts = data.counts || {};
    var pairs = Object.keys(counts).map(function (k) {
      return [k, counts[k]];
    });
    pairs.sort(function (a, b) {
      return (Number(b[1]) || 0) - (Number(a[1]) || 0);
    });
    var typeLabels = pairs.map(function (p) {
      return eventLabel(p[0]);
    });
    var typeData = pairs.map(function (p) {
      return Number(p[1]) || 0;
    });
    var cType = document.getElementById('chartByType');
    if (cType && typeLabels.length) {
      chartTypes = new ChartCtor(cType, {
        type: 'bar',
        data: {
          labels: typeLabels,
          datasets: [
            {
              label: at('chartByTypeTitle'),
              data: typeData,
              backgroundColor: 'rgba(36, 129, 204, 0.55)',
            },
          ],
        },
        options: Object.assign({}, commonOpts, {
          indexAxis: 'y',
          scales: {
            x: { beginAtZero: true, ticks: { color: hintC, precision: 0 } },
            y: { ticks: { color: hintC } },
          },
        }),
      });
    }
  }

  function renderLogTable(data) {
    var tb = document.getElementById('auditTbody');
    var hint = document.getElementById('statsLogHint');
    if (hint) {
      hint.textContent = af('statsSince', { since: formatUtcTsDisplay(data.since) });
    }
    if (!tb) return;
    tb.innerHTML = '';
    var rows = data.events || [];
    auditEventsCache = rows;
    if (!rows.length) {
      var tr = document.createElement('tr');
      var td = document.createElement('td');
      td.colSpan = 4;
      td.className = 'sub';
      td.textContent = at('statsLogEmpty');
      tr.appendChild(td);
      tb.appendChild(tr);
      return;
    }
    rows.forEach(function (ev, idx) {
      var tr = document.createElement('tr');
      var tdT = document.createElement('td');
      tdT.textContent = formatUtcTsDisplay(ev.ts);
      var tdTy = document.createElement('td');
      tdTy.textContent = eventLabel(ev.event_type || '');
      var tdU = document.createElement('td');
      tdU.textContent = ev.user_id != null ? String(ev.user_id) : '—';
      var tdP = document.createElement('td');
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn--secondary btn--sm';
      btn.setAttribute('data-ev-idx', String(idx));
      btn.textContent = at('btnLogViewDetails');
      tdP.appendChild(btn);
      tr.appendChild(tdT);
      tr.appendChild(tdTy);
      tr.appendChild(tdU);
      tr.appendChild(tdP);
      tb.appendChild(tr);
    });
  }

  async function loadStats() {
    var errEl = document.getElementById('statsErr');
    var main = document.getElementById('statsMain');
    var empty = document.getElementById('statsEmpty');
    var btn = document.getElementById('btnReloadStats');
    if (errEl) {
      errEl.style.display = 'none';
      errEl.textContent = '';
    }
    if (btn) btn.disabled = true;
    try {
      var days = parseInt(document.getElementById('statsRange').value, 10) || 7;
      var data = await api(
        '/api/admin/stats?chat_id=' +
          encodeURIComponent(chatId) +
          '&range_days=' +
          encodeURIComponent(String(days)) +
          '&log_limit=400'
      );
      var total = Number(data.total_events != null ? data.total_events : 0);
      var hasRows = (data.events || []).length > 0;
      var hasCounts = data.counts && Object.keys(data.counts).length > 0;
      if (main) main.classList.remove('hidden');
      if (empty) empty.classList.toggle('hidden', total > 0 || hasRows || hasCounts);
      renderSummary(data);
      renderCharts(data);
      renderLogTable(data);
    } catch (e) {
      destroyCharts();
      if (main) main.classList.add('hidden');
      if (empty) empty.classList.add('hidden');
      if (errEl) {
        errEl.style.display = 'block';
        errEl.textContent = e.message || String(e);
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  document.getElementById('btnReloadStats').onclick = function () {
    loadStats().catch(function (e) {
      toast(e.message, true);
    });
  };

  syncNav();
  wireAuditDetailDialog();
  refreshPageI18n();
  loadStats().catch(function (e) {
    var errEl = document.getElementById('statsErr');
    if (errEl) {
      errEl.style.display = 'block';
      errEl.textContent = e.message || String(e);
    }
  });
})();
