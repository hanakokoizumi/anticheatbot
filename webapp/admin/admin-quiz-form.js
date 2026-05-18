/**
 * Shared single-question editor (options list, save, reset).
 * Used by quiz-edit.html. Expects DOM ids: qf_id, qf_prompt, qf_choices_list, qf_choice_add, qf_correct (hidden), qf_save, qf_reset.
 */
(function (global) {
  function initAdminQuizForm(opts) {
    var tg = opts.tg;
    var chatId = String(opts.chatId || '');
    var locale = WebappI18n.normalizeLocale(opts.locale || WebappI18n.getLocale(tg));
    var afterSave = opts.afterSave || function () {};

    function at(k) {
      return WebappI18n.adminT(locale, k);
    }
    function af(k, vars) {
      return WebappI18n.adminFormat(locale, k, vars);
    }
    function headers() {
      return { 'X-Telegram-Init-Data': tg.initData, 'Content-Type': 'application/json' };
    }
    async function api(path, requestOpts) {
      var o = requestOpts || {};
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
              : data.message || text || String(r.status);
        throw new Error(msg);
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

    var quizFormChoices = [];
    var quizFormChoiceEditingIndex = null;
    var quizChoiceEditBackup = '';

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    function getCorrectIndex() {
      var ciEl = document.getElementById('qf_correct');
      if (!ciEl) return 0;
      var ci = parseInt(ciEl.value, 10);
      return Number.isFinite(ci) ? ci : 0;
    }

    function setCorrectIndex(idx) {
      var ciEl = document.getElementById('qf_correct');
      if (!ciEl) return;
      var n = quizFormChoices.length;
      var i = parseInt(idx, 10);
      if (!Number.isFinite(i) || n <= 0) {
        ciEl.value = '0';
        return;
      }
      if (i < 0) i = 0;
      if (i >= n) i = n - 1;
      ciEl.value = String(i);
    }

    function syncCorrectIndexAfterChoiceDelete(deletedIndex) {
      var ciEl = document.getElementById('qf_correct');
      if (!ciEl) return;
      var ci = parseInt(ciEl.value, 10);
      if (!Number.isFinite(ci)) ci = 0;
      if (deletedIndex < ci) ci--;
      else if (deletedIndex === ci) ci = 0;
      var n = quizFormChoices.length;
      if (n <= 0) {
        ciEl.value = '0';
        return;
      }
      if (ci >= n) ci = n - 1;
      if (ci < 0) ci = 0;
      ciEl.value = String(ci);
    }

    function onQuizChoiceEditBlur(idx) {
      if (quizFormChoiceEditingIndex !== idx) return;
      var row = document.querySelector('.quiz-choice-row[data-idx="' + idx + '"]');
      var inp = row && row.querySelector('.quiz-choice-row__input');
      var v = inp ? inp.value.trim() : '';
      if (!v) {
        quizFormChoices[idx] = quizChoiceEditBackup;
      } else {
        quizFormChoices[idx] = v;
      }
      quizFormChoiceEditingIndex = null;
      renderQuizChoicesList();
    }

    function addQuizChoiceFromAddInput() {
      var addInp = document.getElementById('qf_choice_add');
      if (!addInp) return;
      var v = addInp.value.trim();
      if (!v) return;
      if (quizFormChoiceEditingIndex !== null) {
        onQuizChoiceEditBlur(quizFormChoiceEditingIndex);
      }
      addInp.value = '';
      quizFormChoices.push(v);
      renderQuizChoicesList();
    }

    function renderQuizChoicesList() {
      var wrap = document.getElementById('qf_choices_list');
      if (!wrap) return;
      wrap.innerHTML = '';
      var correctIdx = getCorrectIndex();
      quizFormChoices.forEach(function (text, idx) {
        var row = document.createElement('div');
        row.className = 'quiz-choice-row';
        row.dataset.idx = String(idx);
        if (idx === correctIdx) row.classList.add('quiz-choice-row--correct');
        var body = document.createElement('div');
        body.className = 'quiz-choice-row__body';
        var isEditing = quizFormChoiceEditingIndex === idx;
        if (!isEditing) {
          body.classList.add('quiz-choice-row__body--tap');
          body.setAttribute('role', 'button');
          body.setAttribute('tabindex', '0');
          body.setAttribute('aria-pressed', idx === correctIdx ? 'true' : 'false');
          body.setAttribute('aria-label', at('ariaQuizChoicePickCorrect'));
        }
        if (isEditing) {
          var inp = document.createElement('input');
          inp.type = 'text';
          inp.className = 'quiz-choice-row__input';
          inp.value = text;
          inp.setAttribute('aria-label', at('lblQuizChoices'));
          body.appendChild(inp);
          setTimeout(function () {
            inp.focus();
            inp.select();
          }, 0);
          inp.addEventListener('blur', function () {
            onQuizChoiceEditBlur(idx);
          });
          inp.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter') {
              ev.preventDefault();
              inp.blur();
            }
            if (ev.key === 'Escape') {
              ev.preventDefault();
              quizFormChoices[idx] = quizChoiceEditBackup;
              quizFormChoiceEditingIndex = null;
              renderQuizChoicesList();
            }
          });
        } else {
          var span = document.createElement('span');
          span.className = 'quiz-choice-row__text';
          span.textContent = text;
          body.appendChild(span);
          body.addEventListener('click', function () {
            setCorrectIndex(idx);
            renderQuizChoicesList();
          });
          body.addEventListener('keydown', function (ev) {
            if (ev.key !== 'Enter' && ev.key !== ' ') return;
            ev.preventDefault();
            setCorrectIndex(idx);
            renderQuizChoicesList();
          });
        }
        var actions = document.createElement('div');
        actions.className = 'quiz-choice-row__actions';
        var btnEd = document.createElement('button');
        btnEd.type = 'button';
        btnEd.className = 'btn btn--icon btn--secondary btn--sm';
        btnEd.setAttribute('aria-label', at('quizEdit'));
        btnEd.setAttribute('title', at('quizEdit'));
        btnEd.innerHTML =
          '<svg class="btn-ic" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>';
        btnEd.disabled = isEditing;
        btnEd.onclick = function (ev) {
          ev.stopPropagation();
          if (quizFormChoiceEditingIndex !== null && quizFormChoiceEditingIndex !== idx) {
            onQuizChoiceEditBlur(quizFormChoiceEditingIndex);
          }
          quizChoiceEditBackup = quizFormChoices[idx];
          quizFormChoiceEditingIndex = idx;
          renderQuizChoicesList();
        };
        var btnDel = document.createElement('button');
        btnDel.type = 'button';
        btnDel.className = 'btn btn--icon btn--secondary btn--sm';
        btnDel.setAttribute('aria-label', at('quizDel'));
        btnDel.setAttribute('title', at('quizDel'));
        btnDel.innerHTML =
          '<svg class="btn-ic" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
        btnDel.onclick = function (ev) {
          ev.stopPropagation();
          if (!confirm(at('confirmDeleteQuizChoice'))) return;
          if (quizFormChoiceEditingIndex === idx) {
            quizFormChoiceEditingIndex = null;
          } else if (quizFormChoiceEditingIndex !== null && quizFormChoiceEditingIndex > idx) {
            quizFormChoiceEditingIndex--;
          }
          quizFormChoices.splice(idx, 1);
          syncCorrectIndexAfterChoiceDelete(idx);
          renderQuizChoicesList();
        };
        actions.appendChild(btnEd);
        actions.appendChild(btnDel);
        row.appendChild(body);
        row.appendChild(actions);
        wrap.appendChild(row);
      });
    }

    function fillQuizForm(q) {
      var idEl = document.getElementById('qf_id');
      if (idEl) idEl.value = q.id ? String(q.id) : '';
      var pr = document.getElementById('qf_prompt');
      if (pr) pr.value = q.prompt || '';
      quizFormChoices = (q.choices || []).map(function (c) {
        return String(c);
      });
      quizFormChoiceEditingIndex = null;
      var ci = document.getElementById('qf_correct');
      if (ci) {
        var raw = q.correct_index != null ? parseInt(String(q.correct_index), 10) : 0;
        if (!Number.isFinite(raw)) raw = 0;
        var n = quizFormChoices.length;
        if (n <= 0) ci.value = '0';
        else {
          if (raw < 0) raw = 0;
          else if (raw >= n) raw = n - 1;
          ci.value = String(raw);
        }
      }
      renderQuizChoicesList();
    }

    function resetQuizForm() {
      fillQuizForm({ id: '', prompt: '', choices: [], correct_index: 0 });
      var idEl = document.getElementById('qf_id');
      if (idEl) idEl.value = '';
      var addInp = document.getElementById('qf_choice_add');
      if (addInp) addInp.value = '';
    }

    function setQuizFormBusy(disabled) {
      ['qf_save', 'qf_reset'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.disabled = disabled;
      });
      var linkImp = document.getElementById('linkQuizImport');
      if (linkImp) linkImp.setAttribute('aria-disabled', disabled ? 'true' : 'false');
      if (linkImp) linkImp.style.pointerEvents = disabled ? 'none' : '';
      var addInp = document.getElementById('qf_choice_add');
      if (addInp) addInp.disabled = disabled;
      document.querySelectorAll('.quiz-choice-row button').forEach(function (b) {
        b.disabled = disabled;
      });
      document.querySelectorAll('.quiz-choice-row__input').forEach(function (inp) {
        inp.disabled = disabled;
      });
    }

    async function saveQuiz() {
      if (!chatId) {
        toast(at('toastSelectChat'), true);
        return;
      }
      if (quizFormChoiceEditingIndex !== null) {
        onQuizChoiceEditBlur(quizFormChoiceEditingIndex);
      }
      var prompt = (document.getElementById('qf_prompt') && document.getElementById('qf_prompt').value.trim()) || '';
      var choices = quizFormChoices
        .map(function (x) {
          return String(x).trim();
        })
        .filter(Boolean);
      if (choices.length < 2) {
        toast(at('toastQuizChoices2'), true);
        return;
      }
      var correctIndex = getCorrectIndex();
      if (!Number.isFinite(correctIndex) || correctIndex < 0 || correctIndex >= choices.length) {
        toast(af('toastQuizCorrectRange', { max: choices.length - 1 }), true);
        return;
      }
      var qid = (document.getElementById('qf_id') && document.getElementById('qf_id').value.trim()) || '';
      var body = {
        prompt: prompt,
        choices: choices,
        correct_index: correctIndex,
      };
      if (qid) body.id = parseInt(qid, 10);
      if (!prompt) {
        toast(at('toastQuizPrompt'), true);
        return;
      }
      try {
        setQuizFormBusy(true);
        await api('/api/admin/quiz?chat_id=' + encodeURIComponent(chatId), {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify(body),
        });
        toast(qid ? at('toastQuizUpdated') : at('toastQuizAdded'));
        await afterSave();
      } catch (e) {
        toast(e.message, true);
      } finally {
        setQuizFormBusy(false);
      }
    }

    function syncHintQuizCorrectTap() {
      var el = document.getElementById('hintQuizCorrectTap');
      if (!el) return;
      el.textContent = at('hintQuizCorrectTap');
    }

    function applyFormLocale() {
      WebappI18n.applyAdminLocale(document, locale);
      document.documentElement.lang = locale === 'zh-Hans' ? 'zh-Hans' : locale;
      var sel = document.getElementById('adminUiLocale');
      if (sel) sel.value = locale;
      if (opts.onLocaleApplied) opts.onLocaleApplied(locale);
      renderQuizChoicesList();
      syncHintQuizCorrectTap();
    }

    document.getElementById('qf_save').onclick = saveQuiz;
    document.getElementById('qf_reset').onclick = resetQuizForm;
    var qfChoiceAdd = document.getElementById('qf_choice_add');
    if (qfChoiceAdd) {
      qfChoiceAdd.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter') return;
        ev.preventDefault();
        addQuizChoiceFromAddInput();
      });
    }
    var locSel = document.getElementById('adminUiLocale');
    if (locSel) {
      locSel.addEventListener('change', function () {
        locale = WebappI18n.normalizeLocale(locSel.value);
        WebappI18n.setLocale(locale);
        applyFormLocale();
      });
    }

    applyFormLocale();

    return {
      fillQuizForm: fillQuizForm,
      resetQuizForm: resetQuizForm,
      renderQuizChoicesList: renderQuizChoicesList,
      getLocale: function () {
        return locale;
      },
    };
  }

  global.initAdminQuizForm = initAdminQuizForm;
})(typeof window !== 'undefined' ? window : this);
