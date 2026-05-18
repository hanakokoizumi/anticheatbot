(function () {
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) {
    document.body.innerHTML =
      '<p class="status-line err">' +
      (window.WebappI18n ? window.WebappI18n.adminT('zh-Hans', 'errOutsideTg') : '请在 Telegram 内打开') +
      '</p>';
    return;
  }
  if (!window.WebappI18n || !window.initAdminQuizForm) {
    document.body.innerHTML = '<p class="status-line err">Missing scripts</p>';
    return;
  }

  tg.ready();
  tg.expand();

  var params = new URLSearchParams(location.search);
  var chatId = params.get('chat_id');
  var qid = (params.get('id') || '').trim();

  if (!chatId) {
    document.body.innerHTML =
      '<p class="status-line err">' + WebappI18n.adminT(WebappI18n.getLocale(tg), 'toastSelectChat') + '</p>';
    return;
  }

  var locale = WebappI18n.normalizeLocale(WebappI18n.getLocale(tg));

  function at(k) {
    return WebappI18n.adminT(locale, k);
  }
  function headers() {
    return { 'X-Telegram-Init-Data': tg.initData, 'Content-Type': 'application/json' };
  }
  async function api(path, opts) {
    var o = opts || {};
    var r = await fetch(path, {
      method: o.method || 'GET',
      headers: Object.assign({}, headers(), o.headers || {}),
      body: o.body,
    });
    var text = await r.text();
    var data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text };
    }
    if (!r.ok) throw new Error(data.message || text || String(r.status));
    return data;
  }

  function syncNavLinks() {
    var back = document.getElementById('linkBackAdmin');
    if (back) back.href = 'group.html?chat_id=' + encodeURIComponent(chatId);
    var imp = document.getElementById('linkQuizImport');
    if (imp) {
      var u = 'quiz-import.html?chat_id=' + encodeURIComponent(chatId);
      if (qid) u += '&id=' + encodeURIComponent(qid);
      imp.href = u;
    }
  }

  function updateHeading() {
    var h = document.getElementById('quizEditHeading');
    if (!h) return;
    h.textContent = qid ? at('headingQuizEdit') : at('headingQuizNew');
  }

  function setDocTitle() {
    var t = document.querySelector('title');
    if (t) t.textContent = qid ? at('docTitleQuizEdit') : at('docTitleQuizNew');
  }

  var formApi = window.initAdminQuizForm({
    tg: tg,
    chatId: chatId,
    locale: locale,
    afterSave: function () {
      location.href = 'group.html?chat_id=' + encodeURIComponent(chatId);
    },
    onLocaleApplied: function (lc) {
      locale = lc;
      updateHeading();
      setDocTitle();
      syncNavLinks();
    },
  });

  syncNavLinks();
  updateHeading();
  setDocTitle();

  function setQuizEditLoading(on) {
    var ov = document.getElementById('quizEditLoading');
    var wrap = document.querySelector('.quiz-edit-main-wrap');
    if (ov) {
      ov.classList.toggle('hidden', !on);
      ov.setAttribute('aria-hidden', on ? 'false' : 'true');
    }
    if (wrap) wrap.setAttribute('aria-busy', on ? 'true' : 'false');
  }

  (async function boot() {
    if (!qid) {
      formApi.resetQuizForm();
      return;
    }
    setQuizEditLoading(true);
    try {
      var data = await api('/api/admin/quiz?chat_id=' + encodeURIComponent(chatId));
      var questions = data.questions || [];
      var found = questions.find(function (q) {
        return String(q.id) === String(qid);
      });
      if (!found) {
        var el = document.getElementById('toast');
        if (el) {
          el.textContent = at('toastQuizQuestionNotFound');
          el.style.display = 'block';
          el.className = 'toast toast--err';
        }
        formApi.resetQuizForm();
        return;
      }
      formApi.fillQuizForm(found);
    } catch (e) {
      var t = document.getElementById('toast');
      if (t) {
        t.textContent = e.message;
        t.style.display = 'block';
        t.className = 'toast toast--err';
      }
    } finally {
      setQuizEditLoading(false);
    }
  })();
})();
