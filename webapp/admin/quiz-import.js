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
  var returnQuizId = (params.get('id') || '').trim();
  if (!chatId) {
    document.body.innerHTML =
      '<p class="status-line err">' + WebappI18n.adminT(WebappI18n.getLocale(tg), 'toastSelectChat') + '</p>';
    return;
  }

  var locale = WebappI18n.normalizeLocale(WebappI18n.getLocale(tg));
  var quizImportInFlight = false;
  var QUIZ_IMPORT_JSON_MAX_CHARS = 450000;

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

  function refreshPageI18n() {
    WebappI18n.applyAdminLocale(document, locale);
    document.documentElement.lang = locale === 'zh-Hans' ? 'zh-Hans' : locale;
    var sel = document.getElementById('adminUiLocale');
    if (sel) sel.value = locale;
    var t = document.querySelector('title');
    if (t) t.textContent = at('docTitleQuizImport');
    applyQuizLlmHelp();
  }

  function applyQuizLlmHelp() {
    var pre = document.getElementById('quiz_llm_json_sample');
    if (pre) pre.textContent = at('quizLlmJsonExample');
    var promptEl = document.getElementById('quiz_llm_prompt');
    if (promptEl) promptEl.textContent = at('quizLlmPrompt');
  }

  async function copyQuizHelpText(kind) {
    var el =
      kind === 'json'
        ? document.getElementById('quiz_llm_json_sample')
        : document.getElementById('quiz_llm_prompt');
    if (!el) return;
    var text = el.value != null && el.value !== '' ? el.value : el.textContent;
    if (!String(text || '').trim()) {
      toast(at('toastCopiedEmpty'), true);
      return;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(String(text));
      } else {
        throw new Error('no clipboard');
      }
    } catch {
      try {
        var tmp = document.createElement('textarea');
        tmp.value = String(text);
        tmp.style.position = 'fixed';
        tmp.style.left = '-9999px';
        document.body.appendChild(tmp);
        tmp.select();
        try {
          if (!document.execCommand('copy')) throw new Error('copy failed');
        } finally {
          document.body.removeChild(tmp);
        }
      } catch {
        toast(at('toastCopyFailed'), true);
        return;
      }
    }
    toast(at('toastCopied'));
  }

  function stripJsonImportWrappers(s) {
    var t = String(s).trim();
    if (/^```/.test(t)) {
      t = t.replace(/^```[a-zA-Z0-9]*\s*\r?\n?/, '');
      t = t.replace(/\r?\n?```\s*$/, '').trim();
    }
    return t;
  }

  function parseQuizImportPayload(raw) {
    var t = stripJsonImportWrappers(String(raw || '').trim());
    if (!t) {
      throw new Error(at('toastQuizImportEmpty'));
    }
    if (t.length > QUIZ_IMPORT_JSON_MAX_CHARS) {
      throw new Error(at('toastQuizImportTooBig'));
    }
    var data;
    try {
      data = JSON.parse(t);
    } catch (ignore) {
      throw new Error(at('toastQuizImportParse'));
    }
    if (Array.isArray(data)) {
      return { questions: data };
    }
    if (data && typeof data === 'object' && Array.isArray(data.questions)) {
      return { questions: data.questions };
    }
    throw new Error(at('toastQuizImportShape'));
  }

  function setImportBusy(disabled) {
    ['qf_quiz_import_append', 'qf_quiz_import_replace'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.disabled = disabled;
    });
    var ta = document.getElementById('qf_quiz_json_import');
    if (ta) ta.disabled = disabled;
    ['btn_quiz_copy_json_sample', 'btn_quiz_copy_llm_prompt'].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.disabled = disabled;
    });
  }

  async function importQuizBank(mode) {
    if (mode === 'replace' && !confirm(at('confirmQuizImportReplace'))) {
      return;
    }
    var ta = document.getElementById('qf_quiz_json_import');
    if (!ta) return;
    var payload;
    try {
      payload = parseQuizImportPayload(ta.value);
    } catch (e) {
      toast(e.message || at('toastQuizImportParse'), true);
      return;
    }
    if (!payload.questions.length) {
      toast(at('toastQuizImportShape'), true);
      return;
    }
    if (quizImportInFlight) return;
    try {
      quizImportInFlight = true;
      setImportBusy(true);
      await api('/api/admin/quiz/batch?chat_id=' + encodeURIComponent(chatId), {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ mode: mode, questions: payload.questions }),
      });
      toast(af('toastQuizImported', { n: String(payload.questions.length) }));
      ta.value = '';
    } catch (e) {
      toast(e.message, true);
    } finally {
      quizImportInFlight = false;
      setImportBusy(false);
    }
  }

  function syncBackLink() {
    var a = document.getElementById('linkBackQuizEdit');
    if (!a) return;
    var u = 'quiz-edit.html?chat_id=' + encodeURIComponent(chatId);
    if (returnQuizId) u += '&id=' + encodeURIComponent(returnQuizId);
    a.href = u;
  }

  document.getElementById('qf_quiz_import_append').onclick = function () {
    importQuizBank('append');
  };
  document.getElementById('qf_quiz_import_replace').onclick = function () {
    importQuizBank('replace');
  };
  document.getElementById('btn_quiz_copy_json_sample').onclick = function () {
    copyQuizHelpText('json').catch(function (e) {
      toast(e.message, true);
    });
  };
  document.getElementById('btn_quiz_copy_llm_prompt').onclick = function () {
    copyQuizHelpText('prompt').catch(function (e) {
      toast(e.message, true);
    });
  };

  syncBackLink();
  refreshPageI18n();
})();
