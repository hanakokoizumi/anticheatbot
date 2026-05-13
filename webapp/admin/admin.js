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

  let locale = WebappI18n.getLocale(tg);
  const params = new URLSearchParams(location.search);
  let chatId = params.get('chat_id');

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

  function headers() {
    return { 'X-Telegram-Init-Data': tg.initData, 'Content-Type': 'application/json' };
  }

  async function api(path, opts) {
    const o = opts || {};
    const r = await fetch(path, {
      method: o.method || 'GET',
      headers: Object.assign({}, headers(), o.headers || {}),
      body: o.body,
    });
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
            : data.message || text || String(r.status);
      throw new Error(msg);
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

  function setActiveChat(id) {
    chatId = String(id);
    document.getElementById('cid').textContent = chatId;
    document.querySelectorAll('.chat-item').forEach(function (el) {
      el.classList.toggle('is-active', el.dataset.cid === chatId);
    });
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
    setVal('f_rules_markdown', s.rules_markdown || '');
    setVal('f_canonical_locale', s.canonical_locale || 'zh-Hans');
    setCheck('f_llm_translation_enabled', s.llm_translation_enabled);
    var loc = s.translation_allowed_locales;
    setVal(
      'f_translation_allowed_locales',
      loc == null ? '' : typeof loc === 'string' ? loc : JSON.stringify(loc)
    );
    setVal('f_quiz_pass_score_threshold', s.quiz_pass_score_threshold);
    setVal('f_quiz_draw_count', s.quiz_draw_count);
    setCheck('f_llm_enabled', s.llm_enabled);
    setVal('f_llm_max_messages', s.llm_max_messages);
    setVal('f_llm_min_confidence_action', s.llm_min_confidence_action);
    setVal('f_spam_action', s.spam_action || 'delete');
  }

  function collectPatchBody() {
    var locRaw = valStr('f_translation_allowed_locales');
    var locPayload = null;
    if (locRaw) {
      try {
        var parsed = JSON.parse(locRaw);
        locPayload = Array.isArray(parsed) ? JSON.stringify(parsed) : locRaw;
      } catch {
        locPayload = locRaw;
      }
    }
    return {
      verification_enabled: valBool('f_verification_enabled'),
      verify_mode: valStr('f_verify_mode') || 'rules_ack',
      verify_timeout_seconds: valInt('f_verify_timeout_seconds', 180),
      kick_on_verify_timeout: valBool('f_kick_on_verify_timeout'),
      turnstile_enabled: valBool('f_turnstile_enabled'),
      rules_markdown: document.getElementById('f_rules_markdown')
        ? document.getElementById('f_rules_markdown').value
        : '',
      canonical_locale: valStr('f_canonical_locale') || 'zh-Hans',
      llm_translation_enabled: valBool('f_llm_translation_enabled'),
      translation_allowed_locales: locPayload,
      quiz_pass_score_threshold: valInt('f_quiz_pass_score_threshold', 80),
      quiz_draw_count: valInt('f_quiz_draw_count', 10),
      llm_enabled: valBool('f_llm_enabled'),
      llm_max_messages: valInt('f_llm_max_messages', 5),
      llm_min_confidence_action: valFloat('f_llm_min_confidence_action', 0.8),
      spam_action: valStr('f_spam_action') || 'delete',
    };
  }

  async function loadChats() {
    var me = await api('/api/admin/session');
    if (me.global_admin) document.getElementById('gab').classList.remove('hidden');

    var data = await api('/api/admin/chats');
    var box = document.getElementById('chats');
    box.innerHTML = '';
    var list = data.chats || [];
    if (!list.length) {
      box.innerHTML =
        '<p class="sub" style="margin:0">' + escapeHtml(at('noChats')) + '</p>';
      return;
    }
    list.forEach(function (c) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'chat-item';
      b.dataset.cid = String(c.chat_id);
      b.textContent = (c.title || at('unnamed')) + '\n' + c.chat_id;
      b.onclick = function () {
        setActiveChat(c.chat_id);
        loadSettingsAndQuiz();
      };
      box.appendChild(b);
    });
    if (!chatId && list.length === 1) setActiveChat(list[0].chat_id);
    if (chatId) setActiveChat(chatId);
    else document.getElementById('cid').textContent = at('pickChat');
  }

  async function loadSettingsAndQuiz() {
    if (!chatId) return;
    var s = await api('/api/admin/settings?chat_id=' + encodeURIComponent(chatId));
    applySettings(s);
    await loadQuizList();
  }

  async function saveSettings() {
    if (!chatId) {
      toast(at('toastSelectChat'), true);
      return;
    }
    try {
      await api('/api/admin/settings?chat_id=' + encodeURIComponent(chatId), {
        method: 'PATCH',
        headers: headers(),
        body: JSON.stringify(collectPatchBody()),
      });
      toast(at('toastSaved'));
      await loadSettingsAndQuiz();
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function loadQuizList() {
    if (!chatId) return;
    var data = await api('/api/admin/quiz?chat_id=' + encodeURIComponent(chatId));
    var list = document.getElementById('quizList');
    if (!list) return;
    list.innerHTML = '';
    (data.questions || []).forEach(function (q) {
      var row = document.createElement('div');
      row.className = 'quiz-row';
      var meta = document.createElement('div');
      meta.className = 'quiz-row__meta';
      var snippet = (q.prompt || '').replace(/\s+/g, ' ');
      if (snippet.length > 120) snippet = snippet.slice(0, 117) + '…';
      meta.innerHTML =
        '<strong>#' +
        q.id +
        '</strong> · ' +
        q.points +
        ' ' +
        escapeHtml(at('unitPoints')) +
        '<br><span style="color:var(--tg-theme-hint-color,#666);font-size:0.8125rem">' +
        escapeHtml(snippet) +
        '</span>';
      var actions = document.createElement('div');
      actions.className = 'quiz-row__actions';
      var btnEd = document.createElement('button');
      btnEd.type = 'button';
      btnEd.className = 'btn btn--secondary btn--sm';
      btnEd.textContent = at('quizEdit');
      btnEd.onclick = function () {
        fillQuizForm(q);
      };
      var btnDel = document.createElement('button');
      btnDel.type = 'button';
      btnDel.className = 'btn btn--secondary btn--sm';
      btnDel.textContent = at('quizDel');
      btnDel.onclick = function () {
        if (!confirm(af('confirmDeleteQ', { id: q.id }))) return;
        deleteQuiz(q.id);
      };
      actions.appendChild(btnEd);
      actions.appendChild(btnDel);
      row.appendChild(meta);
      row.appendChild(actions);
      list.appendChild(row);
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fillQuizForm(q) {
    document.getElementById('qf_id').value = q.id ? String(q.id) : '';
    document.getElementById('qf_prompt').value = q.prompt || '';
    var lines = (q.choices || []).join('\n');
    document.getElementById('qf_choices').value = lines;
    document.getElementById('qf_correct').value = String(q.correct_index != null ? q.correct_index : 0);
    document.getElementById('qf_points').value = String(q.points != null ? q.points : 10);
  }

  function resetQuizForm() {
    fillQuizForm({ id: '', prompt: '', choices: [], correct_index: 0, points: 10 });
    document.getElementById('qf_id').value = '';
  }

  async function saveQuiz() {
    if (!chatId) {
      toast(at('toastSelectChat'), true);
      return;
    }
    var prompt = document.getElementById('qf_prompt').value.trim();
    var rawChoices = document.getElementById('qf_choices').value.split('\n');
    var choices = rawChoices
      .map(function (x) {
        return x.trim();
      })
      .filter(Boolean);
    if (choices.length < 2) {
      toast(at('toastQuizChoices2'), true);
      return;
    }
    var correctIndex = parseInt(document.getElementById('qf_correct').value, 10);
    if (!Number.isFinite(correctIndex) || correctIndex < 0 || correctIndex >= choices.length) {
      toast(af('toastQuizCorrectRange', { max: choices.length - 1 }), true);
      return;
    }
    var points = parseInt(document.getElementById('qf_points').value, 10);
    if (!Number.isFinite(points) || points < 0) {
      toast(at('toastQuizPoints'), true);
      return;
    }
    var qid = document.getElementById('qf_id').value.trim();
    var body = {
      prompt: prompt,
      choices: choices,
      correct_index: correctIndex,
      points: points,
    };
    if (qid) body.id = parseInt(qid, 10);
    if (!prompt) {
      toast(at('toastQuizPrompt'), true);
      return;
    }
    try {
      await api('/api/admin/quiz?chat_id=' + encodeURIComponent(chatId), {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(body),
      });
      toast(qid ? at('toastQuizUpdated') : at('toastQuizAdded'));
      resetQuizForm();
      await loadQuizList();
    } catch (e) {
      toast(e.message, true);
    }
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

  async function loadStats() {
    if (!chatId) {
      toast(at('toastSelectChat'), true);
      return;
    }
    var days = parseInt(document.getElementById('statsRange').value, 10) || 7;
    var out = document.getElementById('statsOut');
    out.innerHTML = '<p class="sub" style="margin:0">' + escapeHtml(at('statsLoading')) + '</p>';
    try {
      var s = await api(
        '/api/admin/stats?chat_id=' + encodeURIComponent(chatId) + '&range_days=' + encodeURIComponent(String(days))
      );
      var counts = s.counts || {};
      var keys = Object.keys(counts).sort();
      if (!keys.length) {
        out.innerHTML = '<p class="sub" style="margin:0">' + escapeHtml(at('statsEmpty')) + '</p>';
        return;
      }
      var dl = document.createElement('dl');
      dl.className = 'stats-dl';
      keys.forEach(function (k) {
        var dt = document.createElement('dt');
        dt.textContent = k;
        var dd = document.createElement('dd');
        dd.textContent = String(counts[k]);
        dl.appendChild(dt);
        dl.appendChild(dd);
      });
      out.innerHTML = '';
      var since = document.createElement('p');
      since.className = 'sub';
      since.style.margin = '0 0 0.5rem';
      since.textContent = af('statsSince', { since: s.since || '' });
      out.appendChild(since);
      out.appendChild(dl);
    } catch (e) {
      out.innerHTML = '<p class="err" style="margin:0">' + escapeHtml(e.message) + '</p>';
    }
  }

  document.getElementById('reload').onclick = function () {
    loadSettingsAndQuiz().catch(function (e) {
      toast(e.message, true);
    });
  };
  document.getElementById('btnSave').onclick = saveSettings;
  document.getElementById('btnStats').onclick = loadStats;
  document.getElementById('cleartr').onclick = async function () {
    if (!chatId) {
      toast(at('toastSelectChat'), true);
      return;
    }
    try {
      await api('/api/admin/translation-cache/clear?chat_id=' + encodeURIComponent(chatId), { method: 'POST' });
      toast(at('toastTrCleared'));
    } catch (e) {
      toast(e.message, true);
    }
  };
  document.getElementById('qf_save').onclick = saveQuiz;
  document.getElementById('qf_reset').onclick = resetQuizForm;

  document.getElementById('adminUiLocale').addEventListener('change', function () {
    locale = this.value;
    WebappI18n.setLocale(locale);
    refreshAdminI18n();
    if (chatId) {
      loadQuizList().catch(function (e) {
        toast(e.message, true);
      });
    }
  });

  refreshAdminI18n();

  (async function init() {
    var st = document.getElementById('status');
    try {
      await loadChats();
      st.style.display = 'none';
      document.getElementById('panel').classList.remove('hidden');
      if (chatId) await loadSettingsAndQuiz();
    } catch (e) {
      st.innerHTML = '<span class="err">' + escapeHtml(e.message) + '</span>';
    }
  })();
})();
