/**
 * WebApp UI i18n (zh-Hans / en / ja / ko). Telegram language_code + optional localStorage override.
 * Exposes window.WebappI18n — load before admin.js / verify.js.
 */
(function (global) {
  var STORAGE = 'anticheat_web_locale';

  function resolveLocale(languageCode) {
    if (!languageCode || !String(languageCode).trim()) return 'zh-Hans';
    var raw = String(languageCode).trim().replace(/_/g, '-');
    var low = raw.toLowerCase();
    if (low === 'zh' || low.indexOf('zh-') === 0) return 'zh-Hans';
    if (low.indexOf('ja') === 0) return 'ja';
    if (low.indexOf('ko') === 0) return 'ko';
    if (low.indexOf('en') === 0) return 'en';
    return 'en';
  }

  function getLocale(tg) {
    var s = localStorage.getItem(STORAGE);
    if (s === 'zh-Hans' || s === 'en' || s === 'ja' || s === 'ko') return s;
    var u = tg && tg.initDataUnsafe && tg.initDataUnsafe.user;
    return resolveLocale(u && u.language_code);
  }

  function setLocale(lc) {
    if (lc === 'zh-Hans' || lc === 'en' || lc === 'ja' || lc === 'ko') {
      localStorage.setItem(STORAGE, lc);
    }
  }

  function pick(row, k) {
    if (row && row[k] != null) return row[k];
    return null;
  }

  var ADMIN = {
    'zh-Hans': {
      docTitle: '管理后台',
      errOutsideTg: '请在 Telegram 内打开',
      statusLoading: '加载中…',
      pageTitle: '群管理',
      badgeGlobal: '全局',
      subtitle: '选择群组后修改设置、维护题库与查看统计。仅群管理员或全局管理员可访问。',
      sectionChat: '当前群组',
      reload: '重新加载',
      clearTranslation: '清除翻译缓存',
      sectionVerify: '验证与开关',
      lblVerification: '启用入群验证',
      lblVerifyMode: '验证模式',
      optRulesAck: '群规确认',
      optQuiz: '问卷',
      lblVerifyTimeout: '验证超时（秒）',
      lblKickTimeout: '超时踢出',
      lblTurnstile: '启用 Cloudflare Turnstile',
      sectionRules: '群规（Markdown）',
      lblRulesBody: '正文',
      phRules: '支持 Markdown 文本',
      sectionLang: '语言与翻译',
      lblCanonical: '规范语言（原文语言）',
      lblLlmTranslation: '启用 LLM 翻译群规/题目',
      lblAllowedLocales: '允许翻译到的语言',
      phAllowedLocales: '留空表示不限制；或 JSON 数组如 ["en","ja"]',
      sectionQuizCfg: '问卷参数',
      lblQuizPass: '及格分数线（总分制下）',
      lblQuizDraw: '每次抽取题目数',
      sectionQuizBank: '题库',
      subQuizForm: '新增 / 编辑题目',
      lblQuizPrompt: '题干',
      lblQuizChoices: '选项（每行一个，至少 2 行）',
      phQuizChoices: '选项 A\n选项 B\n…',
      lblQuizCorrect: '正确答案序号（从 0 开始）',
      lblQuizPoints: '分值',
      btnQuizSave: '保存题目',
      btnQuizReset: '清空表单',
      sectionLlm: 'LLM 反垃圾',
      lblLlmEnabled: '启用 LLM 审查新成员消息',
      lblLlmMax: '每名新成员最多审查条数',
      lblLlmConf: '采取行动最低置信度（0–1）',
      lblSpamAction: '判定为垃圾时',
      optSpamDelete: '仅删除',
      optSpamRestrict: '删除并限制',
      sectionStats: '统计',
      lblStatsRange: '统计天数',
      statsD1: '近 1 日',
      statsD7: '近 7 日',
      statsD30: '近 30 日',
      btnLoadStats: '加载统计',
      btnSaveGroup: '保存群设置',
      hintSaveBar: '题库单独使用「保存题目」；上方表单为群级配置。',
      langLabel: '界面语言',
      langOptZh: '简体中文',
      langOptEn: 'English',
      langOptJa: '日本語',
      langOptKo: '한국어',
      noChats: '暂无已知群组。请先在群内使用机器人，或由全局管理员从链接带 chat_id 打开。',
      pickChat: '请选择一个群',
      unnamed: '未命名群',
      statsLoading: '加载中…',
      statsEmpty: '该时段内暂无事件。',
      statsSince: '自 {since} 起',
      toastSelectChat: '请先选择群组',
      toastSaved: '已保存',
      toastTrCleared: '已清除该群翻译缓存',
      toastDeleted: '已删除',
      toastQuizChoices2: '请至少填写 2 个选项（每行一个）',
      toastQuizCorrectRange: '正确答案序号需在 0 到 {max} 之间',
      toastQuizPoints: '分值无效',
      toastQuizPrompt: '请填写题干',
      toastQuizUpdated: '题目已更新',
      toastQuizAdded: '题目已添加',
      confirmDeleteQ: '确定删除题目 {id}？',
      unitPoints: '分',
      quizEdit: '编辑',
      quizDel: '删',
    },
    en: {
      docTitle: 'Admin',
      errOutsideTg: 'Open inside Telegram',
      statusLoading: 'Loading…',
      pageTitle: 'Group admin',
      badgeGlobal: 'Global',
      subtitle:
        'Select a group to edit settings, manage the quiz bank, and view stats. Group admins or global admins only.',
      sectionChat: 'Current group',
      reload: 'Reload',
      clearTranslation: 'Clear translation cache',
      sectionVerify: 'Verification',
      lblVerification: 'Enable join verification',
      lblVerifyMode: 'Verification mode',
      optRulesAck: 'Rules acknowledgement',
      optQuiz: 'Quiz',
      lblVerifyTimeout: 'Verification timeout (seconds)',
      lblKickTimeout: 'Kick on timeout',
      lblTurnstile: 'Enable Cloudflare Turnstile',
      sectionRules: 'Rules (Markdown)',
      lblRulesBody: 'Body',
      phRules: 'Markdown supported',
      sectionLang: 'Language & translation',
      lblCanonical: 'Canonical locale (source language)',
      lblLlmTranslation: 'Enable LLM translation for rules/quiz',
      lblAllowedLocales: 'Allowed target locales',
      phAllowedLocales: 'Leave empty for no limit, or JSON array e.g. ["en","ja"]',
      sectionQuizCfg: 'Quiz settings',
      lblQuizPass: 'Pass score threshold',
      lblQuizDraw: 'Questions drawn per session',
      sectionQuizBank: 'Question bank',
      subQuizForm: 'Add / edit question',
      lblQuizPrompt: 'Prompt',
      lblQuizChoices: 'Choices (one per line, at least 2)',
      phQuizChoices: 'Choice A\nChoice B\n…',
      lblQuizCorrect: 'Correct choice index (from 0)',
      lblQuizPoints: 'Points',
      btnQuizSave: 'Save question',
      btnQuizReset: 'Clear form',
      sectionLlm: 'LLM anti-spam',
      lblLlmEnabled: 'Enable LLM moderation for new members',
      lblLlmMax: 'Max messages to moderate per new member',
      lblLlmConf: 'Min confidence to take action (0–1)',
      lblSpamAction: 'When spam is detected',
      optSpamDelete: 'Delete only',
      optSpamRestrict: 'Delete and restrict',
      sectionStats: 'Statistics',
      lblStatsRange: 'Range',
      statsD1: 'Last 1 day',
      statsD7: 'Last 7 days',
      statsD30: 'Last 30 days',
      btnLoadStats: 'Load stats',
      btnSaveGroup: 'Save group settings',
      hintSaveBar: 'Use “Save question” for the bank; the form above is group-level settings.',
      langLabel: 'UI language',
      langOptZh: '简体中文',
      langOptEn: 'English',
      langOptJa: '日本語',
      langOptKo: '한국어',
      noChats: 'No known groups yet. Use the bot in a group first, or open with chat_id (global admin).',
      pickChat: 'Select a group',
      unnamed: 'Untitled group',
      statsLoading: 'Loading…',
      statsEmpty: 'No events in this period.',
      statsSince: 'Since {since}',
      toastSelectChat: 'Select a group first',
      toastSaved: 'Saved',
      toastTrCleared: 'Translation cache cleared for this group',
      toastDeleted: 'Deleted',
      toastQuizChoices2: 'Enter at least 2 choices (one per line)',
      toastQuizCorrectRange: 'Correct index must be between 0 and {max}',
      toastQuizPoints: 'Invalid points',
      toastQuizPrompt: 'Enter the prompt',
      toastQuizUpdated: 'Question updated',
      toastQuizAdded: 'Question added',
      confirmDeleteQ: 'Delete question {id}?',
      unitPoints: 'pts',
      quizEdit: 'Edit',
      quizDel: 'Del',
    },
    ja: {
      docTitle: '管理画面',
      errOutsideTg: 'Telegram 内で開いてください',
      statusLoading: '読み込み中…',
      pageTitle: 'グループ管理',
      badgeGlobal: '全体',
      subtitle:
        'グループを選び、設定・問題バンク・統計を管理します。グループ管理者または全体管理者のみ。',
      sectionChat: '対象グループ',
      reload: '再読み込み',
      clearTranslation: '翻訳キャッシュを消去',
      sectionVerify: '認証とスイッチ',
      lblVerification: '参加時の認証を有効にする',
      lblVerifyMode: '認証モード',
      optRulesAck: 'ルール確認',
      optQuiz: 'クイズ',
      lblVerifyTimeout: '認証タイムアウト（秒）',
      lblKickTimeout: 'タイムアウトでキック',
      lblTurnstile: 'Cloudflare Turnstile を有効化',
      sectionRules: 'ルール（Markdown）',
      lblRulesBody: '本文',
      phRules: 'Markdown 対応',
      sectionLang: '言語と翻訳',
      lblCanonical: '正規ロケール（原文の言語）',
      lblLlmTranslation: 'ルール/問題の LLM 翻訳を有効化',
      lblAllowedLocales: '翻訳先として許可するロケール',
      phAllowedLocales: '空欄は制限なし、または JSON 配列例 ["en","ja"]',
      sectionQuizCfg: 'クイズ設定',
      lblQuizPass: '合格点しきい値',
      lblQuizDraw: '1 セッションあたり出題数',
      sectionQuizBank: '問題バンク',
      subQuizForm: '追加 / 編集',
      lblQuizPrompt: '問題文',
      lblQuizChoices: '選択肢（1 行 1 つ、2 つ以上）',
      phQuizChoices: '選択肢 A\n選択肢 B\n…',
      lblQuizCorrect: '正解のインデックス（0 から）',
      lblQuizPoints: '配点',
      btnQuizSave: '問題を保存',
      btnQuizReset: 'フォームをクリア',
      sectionLlm: 'LLM スパム対策',
      lblLlmEnabled: '新メンバーの LLM モデレーションを有効化',
      lblLlmMax: '新メンバーあたりのモデレーション上限',
      lblLlmConf: 'アクション実行の最低信頼度（0–1）',
      lblSpamAction: 'スパム判定時',
      optSpamDelete: '削除のみ',
      optSpamRestrict: '削除と制限',
      sectionStats: '統計',
      lblStatsRange: '集計期間',
      statsD1: '過去 1 日',
      statsD7: '過去 7 日',
      statsD30: '過去 30 日',
      btnLoadStats: '統計を読み込む',
      btnSaveGroup: 'グループ設定を保存',
      hintSaveBar: '問題バンクは「問題を保存」から。上のフォームはグループ共通設定です。',
      langLabel: '表示言語',
      langOptZh: '简体中文',
      langOptEn: 'English',
      langOptJa: '日本語',
      langOptKo: '한국어',
      noChats: '既知のグループがありません。先にグループでボットを利用するか、全体管理者は chat_id 付きで開いてください。',
      pickChat: 'グループを選択してください',
      unnamed: '名称未設定',
      statsLoading: '読み込み中…',
      statsEmpty: 'この期間にイベントはありません。',
      statsSince: '{since} 以降',
      toastSelectChat: '先にグループを選択してください',
      toastSaved: '保存しました',
      toastTrCleared: 'このグループの翻訳キャッシュを消去しました',
      toastDeleted: '削除しました',
      toastQuizChoices2: '選択肢を 2 つ以上、1 行に 1 つ入力してください',
      toastQuizCorrectRange: '正解インデックスは 0 ～ {max} の間にしてください',
      toastQuizPoints: '配点が無効です',
      toastQuizPrompt: '問題文を入力してください',
      toastQuizUpdated: '問題を更新しました',
      toastQuizAdded: '問題を追加しました',
      confirmDeleteQ: '問題 {id} を削除しますか？',
      unitPoints: '点',
      quizEdit: '編集',
      quizDel: '削除',
    },
    ko: {
      docTitle: '관리',
      errOutsideTg: 'Telegram 안에서 열어 주세요',
      statusLoading: '불러오는 중…',
      pageTitle: '그룹 관리',
      badgeGlobal: '전역',
      subtitle:
        '그룹을 선택해 설정·문제 은행·통계를 관리합니다. 그룹 관리자 또는 전역 관리자만 사용할 수 있습니다.',
      sectionChat: '현재 그룹',
      reload: '다시 불러오기',
      clearTranslation: '번역 캐시 지우기',
      sectionVerify: '인증 및 스위치',
      lblVerification: '입장 인증 사용',
      lblVerifyMode: '인증 방식',
      optRulesAck: '규칙 동의',
      optQuiz: '퀴즈',
      lblVerifyTimeout: '인증 제한 시간(초)',
      lblKickTimeout: '시간 초과 시 추방',
      lblTurnstile: 'Cloudflare Turnstile 사용',
      sectionRules: '규칙(Markdown)',
      lblRulesBody: '본문',
      phRules: 'Markdown 지원',
      sectionLang: '언어 및 번역',
      lblCanonical: '기준 로케일(원문 언어)',
      lblLlmTranslation: '규칙/문제 LLM 번역 사용',
      lblAllowedLocales: '허용 번역 대상 로케일',
      phAllowedLocales: '비우면 제한 없음, 또는 JSON 배열 예: ["en","ja"]',
      sectionQuizCfg: '퀴즈 설정',
      lblQuizPass: '합격 점수 기준',
      lblQuizDraw: '세션당 출제 문항 수',
      sectionQuizBank: '문제 은행',
      subQuizForm: '추가 / 편집',
      lblQuizPrompt: '발문',
      lblQuizChoices: '선택지(한 줄에 하나, 2개 이상)',
      phQuizChoices: '선택지 A\n선택지 B\n…',
      lblQuizCorrect: '정답 인덱스(0부터)',
      lblQuizPoints: '배점',
      btnQuizSave: '문항 저장',
      btnQuizReset: '양식 비우기',
      sectionLlm: 'LLM 스팸 방지',
      lblLlmEnabled: '신규 멤버 LLM 검열 사용',
      lblLlmMax: '신규 멤버당 검열 최대 메시지 수',
      lblLlmConf: '조치 최소 신뢰도(0–1)',
      lblSpamAction: '스팸으로 판정 시',
      optSpamDelete: '삭제만',
      optSpamRestrict: '삭제 및 제한',
      sectionStats: '통계',
      lblStatsRange: '기간',
      statsD1: '최근 1일',
      statsD7: '최근 7일',
      statsD30: '최근 30일',
      btnLoadStats: '통계 불러오기',
      btnSaveGroup: '그룹 설정 저장',
      hintSaveBar: '문제 은행은「문항 저장」을 사용하세요. 위 양식은 그룹 단위 설정입니다.',
      langLabel: '표시 언어',
      langOptZh: '简体中文',
      langOptEn: 'English',
      langOptJa: '日本語',
      langOptKo: '한국어',
      noChats: '알려진 그룹이 없습니다. 먼저 그룹에서 봇을 사용하거나, 전역 관리자는 chat_id로 여세요.',
      pickChat: '그룹을 선택하세요',
      unnamed: '이름 없음',
      statsLoading: '불러오는 중…',
      statsEmpty: '이 기간에 이벤트가 없습니다.',
      statsSince: '{since} 이후',
      toastSelectChat: '먼저 그룹을 선택하세요',
      toastSaved: '저장됨',
      toastTrCleared: '이 그룹의 번역 캐시를 지웠습니다',
      toastDeleted: '삭제됨',
      toastQuizChoices2: '선택지를 두 개 이상, 한 줄에 하나씩 입력하세요',
      toastQuizCorrectRange: '정답 인덱스는 0~{max} 사이여야 합니다',
      toastQuizPoints: '배점이 올바르지 않습니다',
      toastQuizPrompt: '발문을 입력하세요',
      toastQuizUpdated: '문항이 수정되었습니다',
      toastQuizAdded: '문항이 추가되었습니다',
      confirmDeleteQ: '문항 {id}을(를) 삭제할까요?',
      unitPoints: '점',
      quizEdit: '편집',
      quizDel: '삭제',
    },
  };

  var VERIFY = {
    'zh-Hans': {
      title: '入群验证',
      subtitle: '请完成以下步骤，通过后即可在群内发言。',
      langLabel: '界面语言',
      turnstileBanner: '本群已启用人机验证：请先在页面底部完成验证框，再提交。',
      needTurnstile: '请先完成下方的人机验证，再点击提交。',
      agree: '我已阅读并同意',
      submitQuiz: '提交答卷',
      quizHint: '共 {n} 题，及格线为 {pass} 分。',
      scoreFail: '未通过：得分 {score} / {max}（需达到 {need}）',
      noRules: '（暂无群规，请联系管理员。）',
      loading: '加载中…',
      missingT: '缺少参数 t',
      errOutsideTg: '请在 Telegram 内打开',
      langOptZh: '简体中文',
      langOptEn: 'English',
      langOptJa: '日本語',
      langOptKo: '한국어',
    },
    en: {
      title: 'Group verification',
      subtitle: 'Complete the steps below. After you pass, you can speak in the group.',
      langLabel: 'UI language',
      turnstileBanner: 'Turnstile is enabled: complete the widget at the bottom before submitting.',
      needTurnstile: 'Please complete the Turnstile challenge below before submitting.',
      agree: 'I have read and agree',
      submitQuiz: 'Submit answers',
      quizHint: '{n} questions. Pass score: {pass}.',
      scoreFail: 'Not passed: score {score} / {max} (need {need})',
      noRules: '(No rules yet. Contact an admin.)',
      loading: 'Loading…',
      missingT: 'Missing parameter t',
      errOutsideTg: 'Open inside Telegram',
      langOptZh: '简体中文',
      langOptEn: 'English',
      langOptJa: '日本語',
      langOptKo: '한국어',
    },
    ja: {
      title: 'グループ認証',
      subtitle: '手順を完了すると、グループで発言できます。',
      langLabel: '表示言語',
      turnstileBanner: 'Turnstile が有効です。送信前にページ下部のウィジェットを完了してください。',
      needTurnstile: '先に下部の認証を完了してから送信してください。',
      agree: '読みました。同意します',
      submitQuiz: '回答を送信',
      quizHint: '全 {n} 問。合格点 {pass} 点。',
      scoreFail: '不合格：得点 {score} / {max}（{need} 点以上が必要）',
      noRules: '（ルールが未設定です。管理者に連絡してください。）',
      loading: '読み込み中…',
      missingT: 'パラメータ t がありません',
      errOutsideTg: 'Telegram 内で開いてください',
      langOptZh: '简体中文',
      langOptEn: 'English',
      langOptJa: '日本語',
      langOptKo: '한국어',
    },
    ko: {
      title: '그룹 인증',
      subtitle: '아래 단계를 완료하면 그룹에서 말할 수 있습니다.',
      langLabel: '표시 언어',
      turnstileBanner: 'Turnstile이 켜져 있습니다. 하단 위젯을 완료한 뒤 제출하세요.',
      needTurnstile: '아래 인증을 먼저 완료한 뒤 제출하세요.',
      agree: '읽었으며 동의합니다',
      submitQuiz: '답안 제출',
      quizHint: '총 {n}문항, 합격 점수 {pass}점.',
      scoreFail: '불합격: {score} / {max}점({need}점 이상 필요)',
      noRules: '(아직 규칙이 없습니다. 관리자에게 문의하세요.)',
      loading: '불러오는 중…',
      missingT: '매개변수 t가 없습니다',
      errOutsideTg: 'Telegram 안에서 열어 주세요',
      langOptZh: '简体中文',
      langOptEn: 'English',
      langOptJa: '日本語',
      langOptKo: '한국어',
    },
  };

  function adminT(lc, k) {
    return (
      pick(ADMIN[lc], k) ||
      pick(ADMIN.en, k) ||
      pick(ADMIN['zh-Hans'], k) ||
      k
    );
  }

  function formatStr(s, vars) {
    if (!vars) return s;
    return String(s).replace(/\{(\w+)\}/g, function (_, name) {
      return vars[name] != null ? String(vars[name]) : '';
    });
  }

  function verifyT(lc, k, vars) {
    var s =
      pick(VERIFY[lc], k) ||
      pick(VERIFY.en, k) ||
      pick(VERIFY['zh-Hans'], k) ||
      k;
    return formatStr(s, vars);
  }

  function applyAdminLocale(root, lc) {
    var el = root || document;
    el.querySelectorAll('[data-i18n]').forEach(function (node) {
      var k = node.getAttribute('data-i18n');
      if (!k) return;
      node.textContent = adminT(lc, k);
    });
    el.querySelectorAll('[data-i18n-placeholder]').forEach(function (node) {
      var k = node.getAttribute('data-i18n-placeholder');
      if (!k) return;
      node.placeholder = adminT(lc, k);
    });
    if (typeof document !== 'undefined') {
      var titleNode = document.querySelector('title');
      if (titleNode) titleNode.textContent = adminT(lc, 'docTitle');
    }
  }

  function adminFormat(lc, k, vars) {
    return formatStr(adminT(lc, k), vars);
  }

  global.WebappI18n = {
    resolveLocale: resolveLocale,
    getLocale: getLocale,
    setLocale: setLocale,
    adminT: adminT,
    adminFormat: adminFormat,
    verifyT: verifyT,
    applyAdminLocale: applyAdminLocale,
  };
})(typeof window !== 'undefined' ? window : this);
