(function () {
  var params = new URLSearchParams(location.search);
  var token = params.get('t');
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg && !token) {
    if (tg.initDataUnsafe && tg.initDataUnsafe.start_param) {
      token = tg.initDataUnsafe.start_param;
    } else if (tg.initData && typeof tg.initData === 'string') {
      try {
        var sp = new URLSearchParams(tg.initData).get('start_param');
        if (sp) token = sp;
      } catch (e) {
        /* ignore */
      }
    }
  }
  if (!tg) {
    var errLc = window.WebappI18n ? WebappI18n.resolveLocale(null) : 'zh-Hans';
    var errMsg = window.WebappI18n ? WebappI18n.verifyT(errLc, 'errOutsideTg') : 'Open inside Telegram';
    document.getElementById('status').innerHTML = '<span class="err">' + errMsg + '</span>';
    return;
  }
  if (!window.WebappI18n) {
    document.getElementById('status').innerHTML = '<span class="err">Missing /shared/i18n.js</span>';
    return;
  }

  var locale = WebappI18n.getLocale(tg);

  function t(key, vars) {
    return WebappI18n.verifyT(locale, key, vars);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  var st0 = document.getElementById('status');
  if (st0) st0.textContent = t('loading');

  tg.ready();
  tg.expand();

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
    } catch (err) {
      throw new Error(t('networkError'));
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
            : data.message || text || String(r.status);
      throw new Error(msg);
    }
    return data;
  }

  var turnstileToken = null;
  var turnstileWidgetId = null;

  function clearTurnstileWidget() {
    turnstileToken = null;
    var host = document.getElementById('turnstile');
    if (!host) return;
    if (turnstileWidgetId != null && window.turnstile) {
      try {
        window.turnstile.remove(turnstileWidgetId);
      } catch (e) {
        /* ignore */
      }
    }
    turnstileWidgetId = null;
    host.innerHTML = '';
  }

  function renderTurnstile(sitekey) {
    if (!sitekey || !window.turnstile) return;
    clearTurnstileWidget();
    var host = document.getElementById('turnstile');
    turnstileWidgetId = window.turnstile.render(host, {
      sitekey: sitekey,
      callback: function (tok) {
        turnstileToken = tok;
        setErr('');
      },
      'expired-callback': function () {
        turnstileToken = null;
      },
    });
  }

  function waitTurnstile(cb) {
    if (window.turnstile) {
      cb();
      return;
    }
    var n = 0;
    var id = setInterval(function () {
      n++;
      if (window.turnstile) {
        clearInterval(id);
        cb();
      }
      if (n > 80) {
        clearInterval(id);
        cb();
      }
    }, 100);
  }

  function showVerifySuccessThenClose() {
    var app = document.getElementById('app');
    if (app) app.style.display = 'none';
    var st = document.getElementById('status');
    st.style.display = 'block';
    st.className = 'status-line status-line--ok';
    st.textContent = t('verifySuccess');
    setTimeout(function () {
      try {
        tg.close();
      } catch (e) {
        /* ignore */
      }
    }, 900);
  }

  function setRulesContent(el, rawMd) {
    var src = rawMd && String(rawMd).trim() ? rawMd : t('noRules');
    if (window.marked && window.DOMPurify) {
      try {
        var html = marked.parse(src, { breaks: true, gfm: true });
        el.innerHTML = DOMPurify.sanitize(html);
        el.classList.add('rules-markdown');
        return;
      } catch (e) {
        console.warn(e);
      }
    }
    el.textContent = src;
    el.classList.remove('rules-markdown');
  }

  function setErr(msg) {
    var el = document.getElementById('errBanner');
    if (!el) return;
    if (!msg) {
      el.style.display = 'none';
      el.textContent = '';
      return;
    }
    el.textContent = msg;
    el.style.display = 'block';
    el.className = 'banner banner--warn';
  }

  function buildLangBar() {
    var bar = document.createElement('div');
    bar.className = 'lang-bar';
    var lab = document.createElement('span');
    lab.className = 'sub';
    lab.textContent = t('langLabel');
    var sel = document.createElement('select');
    sel.id = 'verifyUiLang';
    var LANG_KEYS = { 'zh-Hans': 'langOptZh', en: 'langOptEn', ja: 'langOptJa', ko: 'langOptKo' };
    ;['zh-Hans', 'en', 'ja', 'ko'].forEach(function (lc) {
      var opt = document.createElement('option');
      opt.value = lc;
      opt.textContent = t(LANG_KEYS[lc]);
      sel.appendChild(opt);
    });
    sel.value = locale;
    sel.addEventListener('change', function () {
      WebappI18n.setLocale(sel.value);
      locale = WebappI18n.getLocale(tg);
      setErr('');
      run().catch(function (e) {
        setErr(e.message);
      });
    });
    bar.appendChild(lab);
    bar.appendChild(sel);
    return bar;
  }

  function applyTopCopy() {
    var h = document.querySelector('#app .topbar h1');
    var p = document.querySelector('#app .topbar .sub');
    if (h) h.textContent = t('title');
    if (p) p.textContent = t('subtitle');
    if (typeof document !== 'undefined') {
      var titleNode = document.querySelector('title');
      if (titleNode) titleNode.textContent = t('title');
    }
  }

  async function fetchSession() {
    var qs = new URLSearchParams({ t: token });
    qs.set('lang', WebappI18n.getLocale(tg));
    return api('/api/verify/session?' + qs.toString());
  }

  async function run() {
    locale = WebappI18n.getLocale(tg);
    var st = document.getElementById('status');
    if (!token && tg.initDataUnsafe && tg.initDataUnsafe.start_param) {
      token = tg.initDataUnsafe.start_param;
    }
    if (!token && tg.initData && typeof tg.initData === 'string') {
      try {
        var sp2 = new URLSearchParams(tg.initData).get('start_param');
        if (sp2) token = sp2;
      } catch (e2) {
        /* ignore */
      }
    }
    if (!token) {
      st.innerHTML = '<span class="err">' + t('missingT') + '</span>';
      return;
    }
    st.textContent = t('loading');
    st.style.display = 'block';
    document.getElementById('app').style.display = 'none';
    clearTurnstileWidget();

    var sess;
    try {
      sess = await fetchSession();
    } catch (e) {
      st.innerHTML = '<span class="err">' + escapeHtml(e.message) + '</span>';
      return;
    }
    st.style.display = 'none';
    var app = document.getElementById('app');
    app.style.display = 'block';
    applyTopCopy();

    var main = document.getElementById('main');
    main.innerHTML = '';
    var existingBar = document.getElementById('langBarMount');
    if (existingBar) existingBar.remove();
    var oldErr = document.getElementById('errBanner');
    if (oldErr) oldErr.remove();
    var mount = document.createElement('div');
    mount.id = 'langBarMount';
    mount.appendChild(buildLangBar());
    var err = document.createElement('div');
    err.id = 'errBanner';
    err.className = 'banner banner--warn';
    err.style.display = 'none';
    app.insertBefore(mount, app.querySelector('.topbar').nextSibling);
    app.insertBefore(err, main);

    if (sess.turnstile_site_key) {
      var ban = document.createElement('div');
      ban.className = 'banner';
      ban.textContent = t('turnstileBanner');
      main.appendChild(ban);
      waitTurnstile(function () {
        if (!window.turnstile) {
          setErr(t('turnstileLoadFailed'));
          return;
        }
        try {
          renderTurnstile(sess.turnstile_site_key);
        } catch (e) {
          console.warn(e);
          setErr(t('turnstileLoadFailed'));
        }
      });
    }

    if (sess.verify_mode === 'quiz') {
      renderQuiz(main, sess);
    } else {
      renderRules(main, sess);
    }
  }

  function renderRules(main, sess) {
    var card = document.createElement('div');
    card.className = 'card';
    var rules = document.createElement('div');
    rules.className = 'rules-scroll';
    rules.id = 'rules';
    setRulesContent(rules, sess.rules_markdown);
    var actions = document.createElement('div');
    actions.className = 'actions';
    var agree = document.createElement('button');
    agree.type = 'button';
    agree.className = 'btn btn--primary';
    agree.textContent = t('agree');
    agree.disabled = true;
    rules.addEventListener('scroll', function () {
      var near = rules.scrollTop + rules.clientHeight >= rules.scrollHeight - 10;
      agree.disabled = !near;
    });
    agree.addEventListener('click', async function () {
      if (sess.turnstile_enabled && sess.turnstile_site_key && !turnstileToken) {
        setErr(t('needTurnstile'));
        return;
      }
      setErr('');
      agree.disabled = true;
      try {
        await api('/api/verify/rules-complete', {
          method: 'POST',
          body: JSON.stringify({ token: token, turnstile_token: turnstileToken }),
        });
        showVerifySuccessThenClose();
      } catch (e) {
        agree.disabled = false;
        setErr(e.message);
      }
    });
    actions.appendChild(agree);
    card.appendChild(rules);
    card.appendChild(actions);
    main.appendChild(card);
    setTimeout(function () {
      var near = rules.scrollTop + rules.clientHeight >= rules.scrollHeight - 10;
      if (near) agree.disabled = false;
    }, 0);
  }

  function renderQuiz(main, sess) {
    var card = document.createElement('div');
    card.className = 'card';
    var hint = document.createElement('p');
    hint.className = 'sub';
    hint.style.marginTop = '0';
    hint.textContent = t('quizHint', {
      n: (sess.quiz || []).length,
      pass: sess.quiz_pass_score_threshold,
    });
    card.appendChild(hint);

    if (!(sess.quiz || []).length) {
      var empty = document.createElement('p');
      empty.className = 'err';
      empty.style.marginTop = '0.5rem';
      empty.textContent = t('quizEmptyBank');
      card.appendChild(empty);
      main.appendChild(card);
      return;
    }

    var form = document.createElement('form');
    (sess.quiz || []).forEach(function (q, qi) {
      var box = document.createElement('div');
      box.className = 'quiz-q';
      var title = document.createElement('div');
      title.className = 'q-title';
      title.textContent = '#' + (qi + 1);
      box.appendChild(title);
      var stem = document.createElement('div');
      stem.style.marginBottom = '0.5rem';
      stem.style.fontSize = '0.9375rem';
      stem.textContent = q.prompt;
      box.appendChild(stem);
      q.choices.forEach(function (c, idx) {
        var lab = document.createElement('label');
        lab.className = 'choice';
        var inp = document.createElement('input');
        inp.type = 'radio';
        inp.name = 'q' + q.id;
        inp.value = String(idx);
        inp.required = true;
        var span = document.createElement('span');
        span.textContent = c;
        lab.appendChild(inp);
        lab.appendChild(span);
        box.appendChild(lab);
      });
      form.appendChild(box);
    });
    var actions = document.createElement('div');
    actions.className = 'actions';
    var btn = document.createElement('button');
    btn.type = 'submit';
    btn.className = 'btn btn--primary';
    btn.textContent = t('submitQuiz');
    actions.appendChild(btn);
    form.appendChild(actions);
    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      if (sess.turnstile_enabled && sess.turnstile_site_key && !turnstileToken) {
        setErr(t('needTurnstile'));
        return;
      }
      setErr('');
      btn.disabled = true;
      var fd = new FormData(form);
      var answers = {};
      (sess.quiz || []).forEach(function (q) {
        answers[q.id] = parseInt(fd.get('q' + q.id), 10);
      });
      try {
        var res = await fetch('/api/verify/quiz-submit', {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify({ token: token, answers: answers, turnstile_token: turnstileToken }),
        });
        var text = await res.text();
        var data;
        try {
          data = JSON.parse(text);
        } catch {
          data = {};
        }
        if (!res.ok) {
          btn.disabled = false;
          if (res.status === 403 && (data.score != null || data.max_score != null || data.need != null)) {
            var need = data.need != null ? data.need : sess.quiz_pass_score_threshold;
            setErr(
              t('scoreFail', {
                score: data.score != null ? data.score : '?',
                max: data.max_score != null ? data.max_score : '?',
                need: need,
              })
            );
          } else {
            var errTxt =
              (typeof data.message === 'string' && data.message) ||
              (typeof data.detail === 'string' && data.detail) ||
              text ||
              String(res.status);
            setErr(errTxt);
          }
          return;
        }
        showVerifySuccessThenClose();
      } catch (e) {
        btn.disabled = false;
        var msg = e && e.message ? String(e.message) : '';
        var isNet =
          (typeof TypeError !== 'undefined' && e instanceof TypeError) ||
          msg === 'Failed to fetch' ||
          /network|fetch/i.test(msg);
        setErr(isNet ? t('networkError') : msg);
      }
    });
    card.appendChild(form);
    main.appendChild(card);
  }

  run().catch(function (e) {
    document.getElementById('status').innerHTML = '<span class="err">' + escapeHtml(e.message) + '</span>';
  });
})();
