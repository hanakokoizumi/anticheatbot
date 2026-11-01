(function () {
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) {
    var errLc = window.WebappI18n ? WebappI18n.resolveLocale(null) : 'zh-Hans';
    var errMsg = window.WebappI18n ? WebappI18n.verifyT(errLc, 'errOutsideTg') : 'Open inside Telegram';
    document.getElementById('status').innerHTML = '<span class="err">' + escapeHtml(errMsg) + '</span>';
    return;
  }

  /** 必须在 tg.ready() 之前读取：Telegram 客户端常在 ready 里 replaceState 清掉 ?tgWebAppStartParam= / ?t=。 */
  function captureVerifyTokenFromUrl() {
    var search = new URLSearchParams(location.search);
    var t = search.get('t');
    if (t) return t;
    t = search.get('tgWebAppStartParam');
    if (t) return t;
    if (location.hash && location.hash.length > 1) {
      try {
        var hp = new URLSearchParams(location.hash.replace(/^#/, ''));
        t = hp.get('tgWebAppStartParam') || hp.get('t');
        if (t) return t;
      } catch (e) {
        /* ignore */
      }
    }
    return null;
  }

  function normalizeVerifyToken(raw) {
    if (raw == null) return null;
    var v = String(raw).trim();
    if (!v || v === 'undefined' || v === 'null') return null;
    if (v.length > 64 || !/^[A-Za-z0-9_-]{16,64}$/.test(v)) return null;
    return v;
  }

  var urlTokenSnap = captureVerifyTokenFromUrl();

  tg.ready();
  tg.expand();

  function readVerifyTokenFromWebApp(tgApp) {
    if (!tgApp) return null;
    if (tgApp.startParam != null && String(tgApp.startParam).length) {
      return String(tgApp.startParam);
    }
    if (tgApp.initDataUnsafe && tgApp.initDataUnsafe.start_param != null) {
      var sp = String(tgApp.initDataUnsafe.start_param).trim();
      if (sp) return sp;
    }
    if (tgApp.initData && typeof tgApp.initData === 'string') {
      try {
        var x = new URLSearchParams(tgApp.initData).get('start_param');
        if (x) return x;
      } catch (e2) {
        /* ignore */
      }
    }
    return null;
  }

  function readVerifyToken(tgApp) {
    var head =
      typeof window.__hnkVerifyLaunchToken === 'string' && window.__hnkVerifyLaunchToken.length
        ? window.__hnkVerifyLaunchToken
        : null;
    if (head) return head;
    if (urlTokenSnap) return urlTokenSnap;
    var launch = captureVerifyTokenFromUrl() || readVerifyTokenFromWebApp(tgApp);
    if (launch) return launch;
    try {
      var persisted = window.__hnkVerifyReload && sessionStorage.getItem('hnk_verify_t');
      if (persisted) return persisted;
    } catch (e4) {
      /* ignore */
    }
    return null;
  }

  function readVerifyTokenNorm(tgApp) {
    return normalizeVerifyToken(readVerifyToken(tgApp));
  }

  function redirectToAdminNoToken() {
    var adminUrl = '/admin/index.html';
    try {
      var wch = tg.initDataUnsafe && tg.initDataUnsafe.chat;
      if (wch && wch.id != null) {
        adminUrl = '/admin/group.html?chat_id=' + encodeURIComponent(String(wch.id));
      }
    } catch (e) {
      /* ignore */
    }
    var stEl = document.getElementById('status');
    if (stEl) {
      stEl.style.display = 'block';
      if (window.WebappI18n) {
        stEl.textContent = WebappI18n.verifyT(WebappI18n.getLocale(tg), 'redirectAdmin');
      } else {
        stEl.textContent = 'Redirecting…';
      }
    }
    try {
      sessionStorage.removeItem('hnk_verify_t');
    } catch (rs) {
      /* ignore */
    }
    location.replace(adminUrl);
  }

  var token = null;

  if (!window.WebappI18n) {
    document.getElementById('status').innerHTML = '<span class="err">Missing /shared/i18n.js</span>';
    return;
  }

  var locale = WebappI18n.getLocale(tg);

  function t(key, vars) {
    return WebappI18n.verifyT(locale, key, vars);
  }

  /** Map plain-text API errors to localized verify strings (aiohttp uses raw text bodies). */
  function userFacingApiError(rawMsg) {
    var s = String(rawMsg == null ? '' : rawMsg).trim();
    var low = s.toLowerCase();
    var map = {
      'bad token': 'errBadToken',
      'token used': 'errTokenUsed',
      'token expired': 'errTokenExpired',
      'wrong verify_mode': 'errWrongVerifyMode',
      'turnstile failed': 'errTurnstileFailed',
      'turnstile_unavailable': 'errTurnstileUnavailable',
      'turnstile_not_configured': 'errTurnstileNotConfigured',
      'unable to restore chat permissions; please retry': 'errRestorePermissions',
      'membership_changed': 'errMembershipChanged',
      'missing x-telegram-init-data': 'errMissingInitData',
      'missing t': 'missingT',
      'missing token': 'errMissingTokenBody',
      'missing token or answers': 'errMissingAnswers',
      'invalid answers': 'errInvalidAnswers',
      'bad hash': 'errInitDataInvalid',
      'empty init_data': 'errInitDataInvalid',
      'missing hash': 'errInitDataInvalid',
      'missing auth_date': 'errInitDataInvalid',
      'bad auth_date': 'errInitDataInvalid',
      'auth_date too old': 'errInitDataExpired',
      'auth_date in future': 'errInitDataInvalid',
      'duplicate init_data field': 'errInitDataInvalid',
      'bad user json': 'errInitDataInvalid',
      'missing user id': 'errInitDataInvalid',
      'bad user id': 'errInitDataInvalid',
      'init_data too large': 'errInitDataInvalid',
    };
    var key = map[low];
    return key ? t(key) : s;
  }

  function headers() {
    return { 'X-Telegram-Init-Data': tg.initData, 'Content-Type': 'application/json' };
  }

  function forgetInvalidVerifyToken(raw) {
    if (['bad token', 'token used', 'token expired'].indexOf(String(raw || '').trim().toLowerCase()) < 0) return;
    try {
      sessionStorage.removeItem('hnk_verify_t');
    } catch (e) {}
  }

  function identityNeedsReopen(raw) {
    return [
      'auth_date too old', 'auth_date in future', 'bad hash', 'empty init_data',
      'missing hash', 'missing auth_date', 'bad auth_date', 'duplicate init_data field',
      'bad user json', 'missing user id', 'bad user id', 'init_data too large',
      'missing x-telegram-init-data',
    ].indexOf(String(raw || '').trim().toLowerCase()) >= 0;
  }

  /** Non-owner opened this link: server returns JSON error; close Mini App without banner. */
  function tryCloseWrongVerifySubject(res, data) {
    if (res.status !== 403 || !data || data.error !== 'wrong_verify_subject') return false;
    try {
      tg.close();
    } catch (cl) {
      /* ignore */
    }
    return true;
  }

  async function api(path, opts) {
    var o = opts || {};
    var r;
    try {
      r = await fetch(path, {
        method: o.method || 'GET',
        headers: Object.assign({}, headers(), o.headers || {}),
        body: o.body,
        cache: 'no-store',
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
      if (tryCloseWrongVerifySubject(r, data)) {
        var silent = new Error('');
        silent.silentClose = true;
        throw silent;
      }
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
      var err = new Error(userFacingApiError(msg));
      forgetInvalidVerifyToken(msg);
      err.verifyRaw = String(msg).trim().toLowerCase();
      throw err;
    }
    return data;
  }

  var turnstileToken = null;
  var turnstileWidgetId = null;
  var submissionInFlight = false;
  var renderGeneration = 0;
  var turnstileGeneration = 0;
  var stopWaitingTurnstile = null;
  var retryTurnstileAction = null;
  var rulesResizeObserver = null;
  var rulesImageTimer = null;
  var currentErrorSource = null;

  function stopObservingRules() {
    if (rulesResizeObserver) rulesResizeObserver.disconnect();
    rulesResizeObserver = null;
    clearTimeout(rulesImageTimer);
    rulesImageTimer = null;
  }

  function hasTurnstileApi() {
    return window.turnstile && typeof window.turnstile.render === 'function';
  }

  function showTurnstileRetry(message) {
    if (message) setErr(message, 'turnstile');
    var retry = document.getElementById('retryTurnstile');
    if (retry) retry.classList.remove('hidden');
  }

  function setSubmissionBusy(on) {
    submissionInFlight = on;
    var main = document.getElementById('main');
    if (main) main.inert = on;
    var lang = document.getElementById('verifyUiLang');
    if (lang) lang.disabled = on;
  }

  function resetTurnstileForRetry() {
    turnstileToken = null;
    if (turnstileWidgetId != null && hasTurnstileApi()) {
      try {
        window.turnstile.reset(turnstileWidgetId);
      } catch (e) {
        showTurnstileRetry(t('turnstileLoadFailed'));
      }
    }
  }

  function clearTurnstileWidget() {
    turnstileGeneration++;
    if (stopWaitingTurnstile) stopWaitingTurnstile();
    turnstileToken = null;
    var retry = document.getElementById('retryTurnstile');
    if (retry) retry.classList.add('hidden');
    var host = document.getElementById('turnstile');
    if (!host) return;
    if (turnstileWidgetId != null && hasTurnstileApi()) {
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
    if (!sitekey || !hasTurnstileApi()) return;
    clearTurnstileWidget();
    var host = document.getElementById('turnstile');
    var generation = turnstileGeneration;
    turnstileWidgetId = window.turnstile.render(host, {
      sitekey: sitekey,
      size: host.clientWidth < 300 ? 'compact' : 'flexible',
      theme: tg.colorScheme === 'dark' ? 'dark' : 'light',
      language: locale === 'zh-Hans' ? 'zh-cn' : locale,
      'refresh-expired': 'auto',
      'refresh-timeout': 'auto',
      callback: function (tok) {
        if (generation !== turnstileGeneration) return;
        turnstileToken = tok;
        if (currentErrorSource === 'turnstile') setErr('');
        document.getElementById('retryTurnstile').classList.add('hidden');
      },
      'expired-callback': function () {
        if (generation !== turnstileGeneration) return;
        turnstileToken = null;
        showTurnstileRetry(t('needTurnstile'));
      },
      'timeout-callback': function () {
        if (generation !== turnstileGeneration) return;
        turnstileToken = null;
        showTurnstileRetry(t('needTurnstile'));
      },
      'error-callback': function () {
        if (generation !== turnstileGeneration) return true;
        turnstileToken = null;
        showTurnstileRetry(t('errTurnstileFailed'));
        return true;
      },
      'unsupported-callback': function () {
        if (generation !== turnstileGeneration) return;
        turnstileToken = null;
        showTurnstileRetry(t('turnstileLoadFailed'));
      },
    });
  }

  function waitTurnstile(cb) {
    if (stopWaitingTurnstile) stopWaitingTurnstile();
    if (hasTurnstileApi()) {
      cb();
      return;
    }
    var script = document.getElementById('turnstileApi');
    var stopped = false;
    function loaded() {
      if (stopped || !hasTurnstileApi()) return;
      cleanup();
      cb();
    }
    function failed() {
      if (!stopped) showTurnstileRetry(t('turnstileLoadFailed'));
    }
    var timer = setTimeout(failed, 15000);
    function cleanup() {
      stopped = true;
      clearTimeout(timer);
      if (script) {
        script.removeEventListener('load', loaded);
        script.removeEventListener('error', failed);
      }
      stopWaitingTurnstile = null;
    }
    stopWaitingTurnstile = cleanup;
    if (script) {
      script.addEventListener('load', loaded);
      script.addEventListener('error', failed);
    } else {
      failed();
    }
  }

  function showVerifySuccessThenClose() {
    stopObservingRules();
    try {
      sessionStorage.removeItem('hnk_verify_t');
    } catch (rs2) {
      /* ignore */
    }
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

  function showMembershipChanged() {
    stopObservingRules();
    clearTurnstileWidget();
    try {
      sessionStorage.removeItem('hnk_verify_t');
    } catch (e) {}
    document.getElementById('app').style.display = 'none';
    var status = document.getElementById('status');
    status.style.display = 'block';
    status.className = 'status-line status-line--err';
    status.textContent = t('errMembershipChanged');
  }

  function showVerificationDisabled() {
    stopObservingRules();
    clearTurnstileWidget();
    setSubmissionBusy(true);
    try {
      sessionStorage.removeItem('hnk_verify_t');
    } catch (e) {}
    document.getElementById('app').style.display = 'none';
    var status = document.getElementById('status');
    status.style.display = 'block';
    status.className = 'status-line';
    status.textContent = t('verificationDisabled');
  }

  /** Quiz not passed: empty UI, show message, auto-close Mini App after 5s. */
  function showVerifyQuizFailThenClose(detailText) {
    stopObservingRules();
    try {
      sessionStorage.removeItem('hnk_verify_t');
    } catch (rsf) {
      /* ignore */
    }
    var app = document.getElementById('app');
    if (app) {
      app.style.display = 'none';
      var main = document.getElementById('main');
      if (main) main.innerHTML = '';
    }
    var turn = document.getElementById('turnstile');
    if (turn) turn.innerHTML = '';
    clearTurnstileWidget();
    var st = document.getElementById('status');
    st.style.display = 'block';
    st.className = 'status-line status-line--err';
    locale = WebappI18n.getLocale(tg);
    var body =
      t('quizCountFail', {
        correct: Number(detailText.correct),
        total: Number(detailText.total),
        need: Number(detailText.need),
      }) +
      '\n' +
      t('quizFailClosing', { seconds: 5 });
    st.textContent = body;
    setTimeout(function () {
      try {
        tg.close();
      } catch (e) {
        /* ignore */
      }
    }, 5000);
  }

  function looksLikeHtmlFragment(s) {
    var t = String(s || '').trim();
    if (!t || t.charCodeAt(0) !== 60) return false;
    return /<\/[a-z][a-z0-9:-]*>/i.test(t);
  }

  function setRulesContent(el, rawMd) {
    var src = rawMd && String(rawMd).trim() ? rawMd : t('noRules');
    if (window.marked && window.DOMPurify) {
      try {
        if (!window.__hnkMarkedInit) {
          if (typeof marked.setOptions === 'function') {
            marked.setOptions({ async: false, breaks: true, gfm: true });
          }
          window.__hnkMarkedInit = true;
        }
        var htmlRaw;
        if (looksLikeHtmlFragment(src)) {
          htmlRaw = src;
        } else {
          htmlRaw = marked.parse(src);
          if (htmlRaw && typeof htmlRaw.then === 'function') {
            throw new Error('marked async output');
          }
        }
        el.innerHTML = DOMPurify.sanitize(String(htmlRaw), {
          ADD_ATTR: ['target', 'id', 'rel'],
          ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel|tg):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i,
        });
        el.querySelectorAll('a[target="_blank"]').forEach(function (link) {
          link.setAttribute('rel', 'noopener noreferrer');
        });
        el.classList.add('rules-markdown');
        return;
      } catch (e) {
        console.warn(e);
      }
    }
    el.textContent = src;
    el.classList.remove('rules-markdown');
  }

  function setErr(msg, source) {
    if (msg && source === 'turnstile' && currentErrorSource === 'api') return;
    currentErrorSource = msg ? source || 'api' : null;
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
        if (e && e.silentClose) return;
        setErr(userFacingApiError(e.message));
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
    if (submissionInFlight) return;
    stopObservingRules();
    var generation = ++renderGeneration;
    locale = WebappI18n.getLocale(tg);
    var st = document.getElementById('status');
    token = readVerifyTokenNorm(tg) || token;
    if (!token) {
      redirectToAdminNoToken();
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
      if (generation !== renderGeneration) return;
      if (e && e.silentClose) return;
      if (e && e.verifyRaw === 'missing t') {
        try {
          sessionStorage.removeItem('hnk_verify_t');
        } catch (rs3) {
          /* ignore */
        }
        redirectToAdminNoToken();
        return;
      }
      st.innerHTML = '<span class="err">' + escapeHtml(userFacingApiError(e.message)) + '</span>';
      return;
    }
    if (generation !== renderGeneration) return;
    if (sess.verification_disabled === true) {
      showVerificationDisabled();
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

    if (sess.translation_fallback === 'timeout') {
      var translationNotice = document.createElement('div');
      translationNotice.className = 'banner';
      translationNotice.textContent = t('translationFallback');
      main.appendChild(translationNotice);
    }

    if (sess.turnstile_site_key) {
      var ban = document.createElement('div');
      ban.className = 'banner';
      ban.textContent = t('turnstileBanner');
      main.appendChild(ban);
      var renderReadyTurnstile = function () {
        if (generation !== renderGeneration) return;
        if (!hasTurnstileApi()) {
          showTurnstileRetry(t('turnstileLoadFailed'));
          return;
        }
        try {
          renderTurnstile(sess.turnstile_site_key);
        } catch (e) {
          console.warn(e);
          showTurnstileRetry(t('turnstileLoadFailed'));
        }
      };
      retryTurnstileAction = function () {
        if (submissionInFlight || generation !== renderGeneration) return;
        if (currentErrorSource === 'turnstile') setErr('');
        document.getElementById('retryTurnstile').classList.add('hidden');
        if (!hasTurnstileApi()) {
          if (stopWaitingTurnstile) stopWaitingTurnstile();
          var oldScript = document.getElementById('turnstileApi');
          if (oldScript) oldScript.remove();
          var script = document.createElement('script');
          script.id = 'turnstileApi';
          script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
          script.async = true;
          document.head.appendChild(script);
        }
        waitTurnstile(renderReadyTurnstile);
      };
      var retry = document.getElementById('retryTurnstile');
      retry.textContent = t('retryTurnstile');
      retry.onclick = function () { retryTurnstileAction(); };
      waitTurnstile(renderReadyTurnstile);
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
    var imageWarning = document.createElement('div');
    imageWarning.id = 'rulesImageError';
    imageWarning.className = 'banner banner--warn hidden';
    imageWarning.textContent = t('rulesImageFailed');
    imageWarning.setAttribute('role', 'status');
    var retryImages = document.createElement('button');
    retryImages.id = 'retryRulesImages';
    retryImages.type = 'button';
    retryImages.className = 'btn btn--secondary hidden';
    retryImages.textContent = t('reloadRules');
    retryImages.addEventListener('click', function () { location.reload(); });
    var hasReadAll = false;
    function imagesReady() {
      return Array.prototype.every.call(rules.querySelectorAll('img'), function (img) {
        return img.complete && img.naturalWidth > 0;
      });
    }
    function showImageFailure() {
      if (!rules.isConnected) return;
      if (imagesReady()) return;
      imageWarning.classList.remove('hidden');
      retryImages.classList.remove('hidden');
    }
    function updateReadState() {
      if (!rules.isConnected) return;
      var ready = imagesReady();
      if (ready) {
        clearTimeout(rulesImageTimer);
        rulesImageTimer = null;
        imageWarning.classList.add('hidden');
        retryImages.classList.add('hidden');
      } else if (Array.prototype.some.call(rules.querySelectorAll('img'), function (img) {
        return img.complete && !img.naturalWidth;
      })) {
        showImageFailure();
      }
      if (!hasReadAll) {
        var near = rules.scrollTop + rules.clientHeight >= rules.scrollHeight - 10;
        hasReadAll = ready && near;
      }
      agree.disabled = submissionInFlight || !hasReadAll;
    }
    rules.addEventListener('scroll', updateReadState);
    rules.addEventListener('load', updateReadState, true);
    rules.addEventListener('error', updateReadState, true);
    agree.addEventListener('click', async function () {
      if (submissionInFlight) return;
      if (sess.turnstile_enabled && sess.turnstile_site_key && !turnstileToken) {
        setErr(t('needTurnstile'), 'turnstile');
        return;
      }
      setErr('');
      setSubmissionBusy(true);
      agree.disabled = true;
      try {
        var result = await api('/api/verify/rules-complete', {
          method: 'POST',
          body: JSON.stringify({ token: token, turnstile_token: turnstileToken }),
        });
        if (result.verification_disabled === true) {
          showVerificationDisabled();
          return;
        }
        showVerifySuccessThenClose();
      } catch (e) {
        setSubmissionBusy(false);
        updateReadState();
        if (e && e.silentClose) return;
        if (e && e.verifyRaw === 'membership_changed') {
          showMembershipChanged();
          return;
        }
        setErr(userFacingApiError(e.message), e.verifyRaw === 'turnstile failed' ? 'turnstile' : 'api');
        resetTurnstileForRetry();
        if (identityNeedsReopen(e.verifyRaw)) {
          setSubmissionBusy(true);
          agree.disabled = true;
        }
      }
    });
    actions.appendChild(imageWarning);
    actions.appendChild(retryImages);
    actions.appendChild(agree);
    card.appendChild(rules);
    card.appendChild(actions);
    main.appendChild(card);
    if (typeof ResizeObserver !== 'undefined') {
      rulesResizeObserver = new ResizeObserver(updateReadState);
      rulesResizeObserver.observe(rules);
    }
    if (!imagesReady()) rulesImageTimer = setTimeout(showImageFailure, 15000);
    setTimeout(updateReadState, 0);
  }

  function renderQuiz(main, sess) {
    var card = document.createElement('div');
    card.className = 'card';
    var hint = document.createElement('p');
    hint.className = 'sub';
    hint.style.marginTop = '0';
    hint.textContent = t('quizHint', {
      n: (sess.quiz || []).length,
      pass: sess.quiz_pass_min_correct,
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
      if (submissionInFlight) return;
      if (sess.turnstile_enabled && sess.turnstile_site_key && !turnstileToken) {
        setErr(t('needTurnstile'), 'turnstile');
        return;
      }
      setErr('');
      setSubmissionBusy(true);
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
          body: JSON.stringify({ token: token, answers: answers, quiz_version: sess.quiz_version, turnstile_token: turnstileToken }),
        });
        var text = await res.text();
        var data;
        try {
          data = JSON.parse(text);
        } catch {
          data = {};
        }
        if (!res.ok) {
          setSubmissionBusy(false);
          btn.disabled = false;
          if (tryCloseWrongVerifySubject(res, data)) return;
          if (res.status === 403 && data.error === 'membership_changed') {
            showMembershipChanged();
            return;
          }
          if (res.status === 409 && data.error === 'quiz_changed') {
            await run();
            setErr(t('errQuizChanged'));
            return;
          }
          if (
            res.status === 403 &&
            data.need != null &&
            data.correct != null &&
            data.total != null
          ) {
            showVerifyQuizFailThenClose({
              correct: Number(data.correct),
              total: Number(data.total),
              need: Number(data.need),
            });
          } else {
            var errTxt =
              (typeof data.message === 'string' && data.message) ||
              (typeof data.detail === 'string' && data.detail) ||
              (typeof data.error === 'string' && data.error) ||
              text ||
              String(res.status);
            setErr(userFacingApiError(errTxt), String(errTxt).trim().toLowerCase() === 'turnstile failed' ? 'turnstile' : 'api');
            forgetInvalidVerifyToken(errTxt);
            resetTurnstileForRetry();
            if (identityNeedsReopen(errTxt)) {
              setSubmissionBusy(true);
              btn.disabled = true;
            }
          }
          return;
        }
        if (data.verification_disabled === true) {
          showVerificationDisabled();
          return;
        }
        showVerifySuccessThenClose();
      } catch (e) {
        setSubmissionBusy(false);
        btn.disabled = false;
        if (e && e.silentClose) return;
        var msg = e && e.message ? String(e.message) : '';
        var isNet =
          (typeof TypeError !== 'undefined' && e instanceof TypeError) ||
          msg === 'Failed to fetch' ||
          /network|fetch/i.test(msg);
        setErr(isNet ? t('networkError') : userFacingApiError(msg));
        resetTurnstileForRetry();
      }
    });
    card.appendChild(form);
    main.appendChild(card);
  }

  function kickoffVerify() {
    function go(tok) {
      token = tok;
      try {
        sessionStorage.setItem('hnk_verify_t', tok);
      } catch (e) {}
      var st0 = document.getElementById('status');
      if (st0) st0.textContent = t('loading');
      run().catch(function (e) {
        if (e && e.silentClose) return;
        if (e && e.verifyRaw === 'missing t') {
          try {
            sessionStorage.removeItem('hnk_verify_t');
          } catch (rs4) {
            /* ignore */
          }
          redirectToAdminNoToken();
          return;
        }
        document.getElementById('status').innerHTML =
          '<span class="err">' + escapeHtml(userFacingApiError(e.message)) + '</span>';
      });
    }
    var tok0 = readVerifyTokenNorm(tg);
    if (tok0) {
      go(tok0);
      return;
    }
    setTimeout(function () {
      var tok1 = readVerifyTokenNorm(tg);
      if (tok1) {
        go(tok1);
        return;
      }
      redirectToAdminNoToken();
    }, 150);
  }

  kickoffVerify();
})();
