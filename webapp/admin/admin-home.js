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
    document.body.innerHTML = '<p class="status-line err">Missing /shared/i18n.js</p>';
    return;
  }

  tg.ready();
  tg.expand();

  var params = new URLSearchParams(location.search);
  var legacyChat = (params.get('chat_id') || '').trim();
  if (legacyChat) {
    location.replace('group.html?chat_id=' + encodeURIComponent(legacyChat));
    return;
  }

  var locale = WebappI18n.normalizeLocale(WebappI18n.getLocale(tg));
  var mineList = [];
  var globalList = [];
  var activeHomeTab = 'mine';

  function at(k) {
    return WebappI18n.adminT(locale, k);
  }

  function refreshAdminI18n() {
    WebappI18n.applyAdminLocale(document, locale);
    document.documentElement.lang = locale === 'zh-Hans' ? 'zh-Hans' : locale;
    var sel = document.getElementById('adminUiLocale');
    if (sel) sel.value = locale;
    var elTitle = document.getElementById('homePageTitle');
    if (elTitle) elTitle.textContent = at('pageTitleHome');
    var elSub = document.getElementById('homePageSubtitle');
    if (elSub) elSub.textContent = at('homeSubtitle');
    var elSec = document.getElementById('homeSectionChats');
    if (elSec) elSec.textContent = at('sectionHomeChats');
    var t = document.querySelector('title');
    if (t && t.hasAttribute('data-skip-admin-doc-title')) t.textContent = at('pageTitleHome');
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
    } catch (err) {
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

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function renderChatList(list) {
    var box = document.getElementById('chats');
    if (!box) return;
    box.innerHTML = '';
    if (!list.length) {
      box.innerHTML = '<p class="sub" style="margin:0">' + escapeHtml(at('noChats')) + '</p>';
      return;
    }
    list.forEach(function (c) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'chat-item';
      b.dataset.cid = String(c.chat_id);
      b.textContent = (c.title || at('unnamed')) + '\n' + c.chat_id;
      b.onclick = function () {
        location.href = 'group.html?chat_id=' + encodeURIComponent(String(c.chat_id));
      };
      box.appendChild(b);
    });
  }

  function syncHomeTabStyles() {
    var mineBtn = document.getElementById('homeTabMine');
    var globBtn = document.getElementById('homeTabGlobal');
    if (mineBtn) {
      mineBtn.classList.toggle('admin-tab--active', activeHomeTab === 'mine');
      mineBtn.setAttribute('aria-selected', activeHomeTab === 'mine' ? 'true' : 'false');
    }
    if (globBtn) {
      globBtn.classList.toggle('admin-tab--active', activeHomeTab === 'global');
      globBtn.setAttribute('aria-selected', activeHomeTab === 'global' ? 'true' : 'false');
    }
  }

  function applyHomeTab() {
    syncHomeTabStyles();
    renderChatList(activeHomeTab === 'mine' ? mineList : globalList);
  }

  async function loadChats() {
    var me = await api('/api/admin/session');
    var gab = document.getElementById('gab');
    if (gab && me.global_admin) gab.classList.remove('hidden');

    var data = await api('/api/admin/chats');
    var tabsWrap = document.getElementById('homeChatTabsWrap');

    if (me.global_admin && data.chats_mine != null && data.chats_global != null) {
      mineList = data.chats_mine || [];
      globalList = data.chats_global || [];
      if (tabsWrap) tabsWrap.classList.remove('hidden');
      activeHomeTab = 'mine';
      applyHomeTab();
      return;
    }

    mineList = data.chats || [];
    globalList = [];
    if (tabsWrap) tabsWrap.classList.add('hidden');
    renderChatList(mineList);
  }

  document.getElementById('adminUiLocale').addEventListener('change', function () {
    locale = WebappI18n.normalizeLocale(this.value);
    WebappI18n.setLocale(locale);
    refreshAdminI18n();
    applyHomeTab();
  });

  var homeTabMine = document.getElementById('homeTabMine');
  var homeTabGlobal = document.getElementById('homeTabGlobal');
  if (homeTabMine) {
    homeTabMine.addEventListener('click', function () {
      activeHomeTab = 'mine';
      applyHomeTab();
    });
  }
  if (homeTabGlobal) {
    homeTabGlobal.addEventListener('click', function () {
      activeHomeTab = 'global';
      applyHomeTab();
    });
  }

  refreshAdminI18n();

  async function init() {
    var st = document.getElementById('status');
    var retry = document.getElementById('retryLoad');
    retry.classList.add('hidden');
    st.textContent = at('statusLoading');
    try {
      await loadChats();
      st.style.display = 'none';
      document.getElementById('panel').classList.remove('hidden');
      refreshAdminI18n();
      applyHomeTab();
    } catch (e) {
      st.innerHTML = '<span class="err">' + escapeHtml(e.message) + '</span>';
      retry.classList.remove('hidden');
    }
  }
  document.getElementById('retryLoad').onclick = init;
  init();
})();
