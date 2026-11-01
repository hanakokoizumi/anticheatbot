(function () {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) {
    document.body.innerHTML =
      '<p class="status-line err">' +
      (window.WebappI18n ? window.WebappI18n.adminT('zh-Hans', 'errOutsideTg') : '请在 Telegram 内打开') +
      '</p>';
    return;
  }
  if (!window.WebappI18n) {
    document.body.innerHTML = '<p class="status-line err">Missing /shared/i18n.js</p>';
    return;
  }

  tg.ready();
  tg.expand();

  var params = new URLSearchParams(location.search);
  var chatId = (params.get('chat_id') || '').trim();
  if (!chatId) {
    document.body.innerHTML =
      '<p class="status-line err">' + WebappI18n.adminT(WebappI18n.getLocale(tg), 'toastSelectChat') + '</p>';
    return;
  }

  let locale = WebappI18n.normalizeLocale(WebappI18n.getLocale(tg));
  var chatTitleState = '';
  var settingsBusy = false;
  var settingsBaseline = null;

  var ICON_QUIZ_EDIT_SVG =
    '<svg class="btn-ic" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>';
  var ICON_QUIZ_DEL_SVG =
    '<svg class="btn-ic" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';

  function at(k) {
    return WebappI18n.adminT(locale, k);
  }
  function af(k, vars) {
    return WebappI18n.adminFormat(locale, k, vars);
  }

  function refreshAdminI18n() {
    WebappI18n.applyAdminLocale(document, locale);
    document.documentElement.lang = locale === 'zh-Hans' ? 'zh-Hans' : locale;
    const sel = document.getElementById('adminUiLocale');
    if (sel) sel.value = locale;
  }

  function syncGroupHeader() {
    var displayName = chatTitleState ? chatTitleState : at('unnamed');
    var h = document.getElementById('groupTitleHead');
    if (h) h.textContent = displayName;
    var back = document.getElementById('linkBackHome');
    if (back) back.href = 'index.html';
    var t = document.querySelector('title');
    if (t) t.textContent = displayName;
  }

  function titleFromWebAppChat() {
    try {
      var ch = tg.initDataUnsafe && tg.initDataUnsafe.chat;
      if (!ch || ch.id == null) return '';
      if (String(ch.id) !== String(chatId)) return '';
      var t = ch.title;
      if (t == null || String(t).trim() === '') return '';
      return String(t).trim();
    } catch (e) {
      return '';
    }
  }

  function headers() {
    return { 'X-Telegram-Init-Data': tg.initData, 'Content-Type': 'application/json' };
  }

  async function api(path, opts) {
    const o = opts || {};
    let r;
    try {
      r = await fetch(path, {
        method: o.method || 'GET',
        headers: Object.assign({}, headers(), o.headers || {}),
        body: o.body,
      });
    } catch (err) {
      throw new Error(at('networkError'));
    }
    const text = await r.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text };
    }
    if (!r.ok) {
      const d = data.detail;
      const msg =
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
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.style.display = 'block';
    el.className = 'toast' + (isErr ? ' toast--err' : '');
    clearTimeout(el._hide);
    el._hide = setTimeout(function () {
      el.style.display = 'none';
    }, 3200);
  }

  function valBool(id) {
    const el = document.getElementById(id);
    return el && el.checked;
  }

  function valStr(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }

  function valInt(id, fallback) {
    const el = document.getElementById(id);
    if (!el) return fallback;
    const n = parseInt(el.value, 10);
    return Number.isFinite(n) ? n : fallback;
  }

  function valFloat(id, fallback) {
    const el = document.getElementById(id);
    if (!el) return fallback;
    const n = parseFloat(el.value);
    return Number.isFinite(n) ? n : fallback;
  }

  function applySettings(s) {
    function setCheck(id, v) {
      const el = document.getElementById(id);
      if (el) el.checked = Boolean(v);
    }
    function setVal(id, v) {
      const el = document.getElementById(id);
      if (el) el.value = v == null ? '' : String(v);
    }
    setCheck('f_verification_enabled', s.verification_enabled);
    setVal('f_verify_mode', s.verify_mode || 'rules_ack');
    setVal('f_verify_timeout_seconds', s.verify_timeout_seconds);
    setCheck('f_kick_on_verify_timeout', s.kick_on_verify_timeout);
    setCheck('f_turnstile_enabled', s.turnstile_enabled);
    setCheck('f_delete_join_message', s.delete_join_message);
    setCheck('f_block_guest_bots', s.block_guest_bots);
    setVal('f_post_verify_behavior', s.post_verify_behavior || 'delete_only');
    setVal('f_welcome_message_markdown', s.welcome_message_markdown || '');
    setVal('f_rules_markdown', s.rules_markdown || '');
    applyCanonicalFromServer(s.canonical_locale);
    setCheck('f_llm_translation_enabled', s.llm_translation_enabled);
    applyAllowedLocalesFromServer(s.translation_allowed_locales);
    setVal('f_quiz_pass_min_correct', s.quiz_pass_min_correct);
    setVal('f_quiz_draw_count', s.quiz_draw_count);
    setCheck('f_llm_enabled', s.llm_enabled);
    setVal('f_llm_max_messages', s.llm_max_messages);
    setVal('f_llm_min_confidence_action', s.llm_min_confidence_action);
    setVal('f_spam_action', s.spam_action || 'delete');
    syncVerifyModeBlocks();
    syncPostVerifyWelcomeBlock();
    chatTitleState =
      s.chat_title != null && String(s.chat_title).trim() !== '' ? String(s.chat_title).trim() : '';
    if (!chatTitleState) {
      var tw = titleFromWebAppChat();
      if (tw) chatTitleState = tw;
    }
    syncGroupHeader();
    settingsBaseline = collectPatchBody();
  }

  function syncVerifyModeBlocks() {
    var mode = valStr('f_verify_mode') || 'rules_ack';
    var rulesB = document.getElementById('admin_block_rules_mode');
    var quizB = document.getElementById('admin_block_quiz_mode');
    if (rulesB) rulesB.classList.toggle('hidden', mode !== 'rules_ack');
    if (quizB) quizB.classList.toggle('hidden', mode !== 'quiz');
  }

  function syncPostVerifyWelcomeBlock() {
    var beh = valStr('f_post_verify_behavior') || 'delete_only';
    var w = document.getElementById('admin_block_welcome');
    if (w) w.classList.toggle('hidden', beh !== 'welcome');
  }

  function clearCustomCanonicalOptions() {
    var sel = document.getElementById('f_canonical_locale');
    if (!sel) return;
    sel.querySelectorAll('option[data-custom-locale="1"]').forEach(function (o) {
      o.remove();
    });
  }

  function ensureCanonicalOption(value) {
    var sel = document.getElementById('f_canonical_locale');
    if (!sel || !value) return;
    var v = String(value).trim();
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === v) return;
    }
    var opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v;
    opt.setAttribute('data-custom-locale', '1');
    sel.appendChild(opt);
  }

  function applyCanonicalFromServer(v) {
    var sel = document.getElementById('f_canonical_locale');
    if (!sel) return;
    clearCustomCanonicalOptions();
    var cv = v == null || String(v).trim() === '' ? 'zh-Hans' : String(v).trim();
    ensureCanonicalOption(cv);
    sel.value = cv;
  }

  function applyAllowedLocalesFromServer(raw) {
    document.querySelectorAll('input[name="f_translation_allowed_loc"]').forEach(function (cb) {
      cb.checked = false;
    });
    if (raw == null) return;
    var str = typeof raw === 'string' ? raw.trim() : '';
    if (!str) return;
    try {
      var parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
      if (!Array.isArray(parsed)) return;
      parsed.forEach(function (code) {
        var c = String(code).trim();
        document.querySelectorAll('input[name="f_translation_allowed_loc"]').forEach(function (cb) {
          if (cb.value === c) cb.checked = true;
        });
      });
    } catch (ignore) {}
  }

  function collectAllowedLocalesPayload() {
    var picked = [];
    document.querySelectorAll('input[name="f_translation_allowed_loc"]:checked').forEach(function (cb) {
      picked.push(cb.value);
    });
    if (!picked.length) return null;
    return JSON.stringify(picked);
  }

  function collectPatchBody() {
    var locPayload = collectAllowedLocalesPayload();
    return {
      verification_enabled: valBool('f_verification_enabled'),
      verify_mode: valStr('f_verify_mode') || 'rules_ack',
      verify_timeout_seconds: valInt('f_verify_timeout_seconds', 180),
      kick_on_verify_timeout: valBool('f_kick_on_verify_timeout'),
      turnstile_enabled: valBool('f_turnstile_enabled'),
      delete_join_message: valBool('f_delete_join_message'),
      block_guest_bots: valBool('f_block_guest_bots'),
      post_verify_behavior: valStr('f_post_verify_behavior') || 'delete_only',
      welcome_message_markdown: document.getElementById('f_welcome_message_markdown')
        ? document.getElementById('f_welcome_message_markdown').value
        : '',
      rules_markdown: document.getElementById('f_rules_markdown')
        ? document.getElementById('f_rules_markdown').value
        : '',
      canonical_locale: valStr('f_canonical_locale') || 'zh-Hans',
      llm_translation_enabled: valBool('f_llm_translation_enabled'),
      translation_allowed_locales: locPayload,
      quiz_pass_min_correct: valInt('f_quiz_pass_min_correct', 1),
      quiz_draw_count: valInt('f_quiz_draw_count', 1),
      llm_enabled: valBool('f_llm_enabled'),
      llm_max_messages: valInt('f_llm_max_messages', 5),
      llm_min_confidence_action: valFloat('f_llm_min_confidence_action', 0.8),
      spam_action: valStr('f_spam_action') || 'delete',
    };
  }

  function setAdminGroupLoading(on) {
    var ov = document.getElementById('adminGroupLoading');
    var wrap = document.querySelector('.admin-group-scope-wrap');
    if (ov) {
      ov.classList.toggle('hidden', !on);
      ov.setAttribute('aria-hidden', on ? 'false' : 'true');
    }
    if (wrap) wrap.setAttribute('aria-busy', on ? 'true' : 'false');
    if (wrap) wrap.inert = on;
  }

  async function loadSettingsAndQuiz() {
    if (!chatId) return;
    var rb = document.getElementById('reload');
    if (rb) rb.disabled = true;
    setAdminGroupLoading(true);
    try {
      var s = await api('/api/admin/settings?chat_id=' + encodeURIComponent(chatId));
      applySettings(s);
      await loadQuizList({ skipListPlaceholder: true });
    } finally {
      setAdminGroupLoading(false);
      if (rb) rb.disabled = false;
    }
  }

  async function saveSettings() {
    if (settingsBusy) return;
    if (!chatId) {
      toast(at('toastSelectChat'), true);
      return;
    }
    var btn = document.getElementById('btnSave');
    var controls = document.querySelectorAll('.admin-group-scope-wrap input[type="number"]');
    for (var i = 0; i < controls.length; i++) {
      if (!controls[i].reportValidity()) return;
    }
    var current = collectPatchBody();
    var body = {};
    Object.keys(current).forEach(function (key) {
      if (!settingsBaseline || current[key] !== settingsBaseline[key]) body[key] = current[key];
    });
    if (!Object.keys(body).length) {
      toast(at('toastNoChanges'));
      return;
    }
    settingsBusy = true;
    setAdminGroupLoading(true);
    if (btn) btn.disabled = true;
    try {
      await api('/api/admin/settings?chat_id=' + encodeURIComponent(chatId), {
        method: 'PATCH',
        headers: headers(),
        body: JSON.stringify(body),
      });
      settingsBaseline = current;
      toast(at('toastSaved'));
      try {
        await loadSettingsAndQuiz();
      } catch (reloadError) {
        toast(at('toastSavedReloadFailed'), true);
      }
    } catch (e) {
      toast(e.message, true);
    } finally {
      settingsBusy = false;
      setAdminGroupLoading(false);
      if (btn) btn.disabled = false;
    }
  }

  function updateQuizBankHeading(count) {
    var h = document.getElementById('quizBankHeading');
    if (h) h.textContent = af('sectionQuizBankCount', { n: count });
  }

  async function loadQuizList(opts) {
    opts = opts || {};
    if (!chatId) return;
    var list = document.getElementById('quizList');
    var skipPh = Boolean(opts.skipListPlaceholder);
    if (list) {
      if (!skipPh) {
        list.innerHTML =
          '<div class="quiz-list-loading" role="status">' +
          '<span class="load-spinner load-spinner--sm" aria-hidden="true"></span>' +
          '<span class="sub">' +
          escapeHtml(at('loadingQuizList')) +
          '</span></div>';
      } else {
        list.style.pointerEvents = 'none';
        list.style.opacity = '0.65';
      }
    }
    try {
      var data = await api('/api/admin/quiz?chat_id=' + encodeURIComponent(chatId));
      var questions = data.questions || [];
      updateQuizBankHeading(questions.length);
      if (!list) return;
      list.innerHTML = '';
    if (!questions.length) {
      var empty = document.createElement('p');
      empty.className = 'sub';
      empty.style.margin = '0 0 0.75rem';
      empty.textContent = at('quizBankEmpty');
      list.appendChild(empty);
    }
    questions.forEach(function (q) {
      var row = document.createElement('div');
      row.className = 'quiz-row';
      var meta = document.createElement('div');
      meta.className = 'quiz-row__meta';
      var snippet = (q.prompt || '').replace(/\s+/g, ' ');
      if (snippet.length > 120) snippet = snippet.slice(0, 117) + '…';
      meta.innerHTML =
        '<span style="color:var(--tg-theme-hint-color,#666);font-size:0.8125rem">' +
        escapeHtml(snippet) +
        '</span>';
      var actions = document.createElement('div');
      actions.className = 'quiz-row__actions';
      var btnEd = document.createElement('button');
      btnEd.type = 'button';
      btnEd.className = 'btn btn--icon btn--secondary btn--sm';
      btnEd.setAttribute('aria-label', at('quizEdit'));
      btnEd.title = at('quizEdit');
      btnEd.innerHTML = ICON_QUIZ_EDIT_SVG;
      btnEd.onclick = function () {
        location.href =
          'quiz-edit.html?chat_id=' + encodeURIComponent(chatId) + '&id=' + encodeURIComponent(String(q.id));
      };
      var btnDel = document.createElement('button');
      btnDel.type = 'button';
      btnDel.className = 'btn btn--icon btn--secondary btn--sm';
      btnDel.setAttribute('aria-label', at('quizDel'));
      btnDel.title = at('quizDel');
      btnDel.innerHTML = ICON_QUIZ_DEL_SVG;
      btnDel.onclick = function () {
        if (!confirm(at('confirmDeleteQ'))) return;
        deleteQuiz(q.id);
      };
      actions.appendChild(btnEd);
      actions.appendChild(btnDel);
      row.appendChild(meta);
      row.appendChild(actions);
      list.appendChild(row);
    });
    } finally {
      if (list) {
        list.style.pointerEvents = '';
        list.style.opacity = '';
      }
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function deleteQuiz(id) {
    try {
      await api(
        '/api/admin/quiz?chat_id=' + encodeURIComponent(chatId) + '&id=' + encodeURIComponent(String(id)),
        { method: 'DELETE' }
      );
      toast(at('toastDeleted'));
      await loadQuizList();
    } catch (e) {
      toast(e.message, true);
    }
  }

  var btnOpenStats = document.getElementById('btnOpenStats');
  if (btnOpenStats) {
    btnOpenStats.onclick = function () {
      if (!chatId) {
        toast(at('toastSelectChat'), true);
        return;
      }
      location.href = 'stats.html?chat_id=' + encodeURIComponent(chatId);
    };
  }

  document.getElementById('reload').onclick = function () {
    if (!chatId) {
      toast(at('toastSelectChat'), true);
      return;
    }
    loadSettingsAndQuiz().catch(function (e) {
      toast(e.message, true);
    });
  };
  document.getElementById('btnSave').onclick = saveSettings;
  document.getElementById('cleartr').onclick = async function () {
    if (!chatId) {
      toast(at('toastSelectChat'), true);
      return;
    }
    var clr = document.getElementById('cleartr');
    if (clr) clr.disabled = true;
    try {
      await api('/api/admin/translation-cache/clear?chat_id=' + encodeURIComponent(chatId), { method: 'POST' });
      toast(at('toastTrCleared'));
    } catch (e) {
      toast(e.message, true);
    } finally {
      if (clr) clr.disabled = false;
    }
  };
  var btnQuizNewPage = document.getElementById('btnQuizNewPage');
  if (btnQuizNewPage) {
    btnQuizNewPage.onclick = function () {
      if (!chatId) {
        toast(at('toastSelectChat'), true);
        return;
      }
      location.href = 'quiz-edit.html?chat_id=' + encodeURIComponent(chatId);
    };
  }

  var verifyModeEl = document.getElementById('f_verify_mode');
  if (verifyModeEl) {
    verifyModeEl.addEventListener('change', syncVerifyModeBlocks);
  }
  var postVerifyBehEl = document.getElementById('f_post_verify_behavior');
  if (postVerifyBehEl) {
    postVerifyBehEl.addEventListener('change', syncPostVerifyWelcomeBlock);
  }

  syncVerifyModeBlocks();
  syncPostVerifyWelcomeBlock();

  async function init() {
    var st = document.getElementById('status');
    var retry = document.getElementById('retryLoad');
    retry.classList.add('hidden');
    st.style.display = 'block';
    st.textContent = at('statusLoading');
    try {
      var me = await api('/api/admin/session');
      if (me.global_admin) document.getElementById('gab').classList.remove('hidden');
      refreshAdminI18n();
      await loadSettingsAndQuiz();
      st.style.display = 'none';
      document.getElementById('panel').classList.remove('hidden');
      refreshAdminI18n();
    } catch (e) {
      st.style.display = 'block';
      st.innerHTML = '<span class="err">' + escapeHtml(e.message) + '</span>';
      retry.classList.remove('hidden');
    }
  }
  document.getElementById('retryLoad').onclick = init;
  init();
})();
