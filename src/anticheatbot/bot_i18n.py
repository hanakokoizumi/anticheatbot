"""Bot-facing copy (Telegram messages / buttons), separate from LLM-translated group content."""

from __future__ import annotations

from typing import Literal

BotLocale = Literal["zh-Hans", "en", "ja", "ko"]


def resolve_bot_locale(language_code: str | None) -> BotLocale:
    """Map Telegram ``User.language_code`` to one of the supported UI locales."""
    if not language_code or not str(language_code).strip():
        return "zh-Hans"
    raw = str(language_code).strip().replace("_", "-")
    low = raw.lower()
    if low == "zh" or low.startswith("zh-"):
        return "zh-Hans"
    if low.startswith("ja"):
        return "ja"
    if low.startswith("ko"):
        return "ko"
    if low.startswith("en"):
        return "en"
    return "en"


MESSAGES: dict[BotLocale, dict[str, str]] = {
    "zh-Hans": {
        "cmd_start": (
            "我是反垃圾机器人。将机器人拉入群组并授予封禁/删除权限后，新成员需通过小程序验证。\n"
            "群管理员发送 /admin 打开管理后台。运维可在环境变量中配置全局管理员（见 README）。"
        ),
        "cmd_admin_group": "在此群组打开管理后台（需具备群管理员身份）。",
        "cmd_admin_private": "选择要管理的群组（在后台内选择 chat）。",
        "btn_open_admin": "打开管理后台",
        "btn_open_verify": "打开验证",
        "verify_invite": "欢迎！请点击按钮完成入群验证（Telegram 小程序）。",
    },
    "en": {
        "cmd_start": (
            "I'm an anti-spam bot. Add me to a group with ban and delete-message permissions; "
            "new members must pass in-app verification.\n"
            "Group admins: send /admin to open the dashboard. Operators can set global admins via "
            "environment variables (see README)."
        ),
        "cmd_admin_group": "Open the admin dashboard for this group (you must be a group administrator).",
        "cmd_admin_private": "Pick a group to manage (select the chat inside the dashboard).",
        "btn_open_admin": "Open admin",
        "btn_open_verify": "Open verification",
        "verify_invite": "Welcome! Tap the button to complete group verification (Telegram Mini App).",
    },
    "ja": {
        "cmd_start": (
            "スパム対策ボットです。グループに追加し、ユーザーの禁止とメッセージ削除の権限を付与してください。"
            "新しいメンバーはミニアプリで認証が必要です。\n"
            "管理者は /admin で管理画面を開けます。運用者は環境変数でグローバル管理者を設定できます（README 参照）。"
        ),
        "cmd_admin_group": "このグループの管理画面を開きます（グループ管理者である必要があります）。",
        "cmd_admin_private": "管理するグループを選びます（ダッシュボード内でチャットを選択してください）。",
        "btn_open_admin": "管理画面を開く",
        "btn_open_verify": "認証を開く",
        "verify_invite": "ようこそ！ボタンからグループ認証（Telegram ミニアプリ）を完了してください。",
    },
    "ko": {
        "cmd_start": (
            "스팸 방지 봇입니다. 그룹에 추가하고 사용자 차단·메시지 삭제 권한을 부여하세요. "
            "신규 멤버는 미니 앱으로 인증해야 합니다.\n"
            "관리자는 /admin 으로 관리 화면을 엽니다. 운영자는 환경 변수로 전역 관리자를 설정할 수 있습니다(README 참고)."
        ),
        "cmd_admin_group": "이 그룹의 관리 화면을 엽니다(그룹 관리자여야 합니다).",
        "cmd_admin_private": "관리할 그룹을 선택하세요(대시보드에서 채팅을 고릅니다).",
        "btn_open_admin": "관리 열기",
        "btn_open_verify": "인증 열기",
        "verify_invite": "환영합니다! 버튼을 눌러 그룹 인증을 완료하세요(Telegram 미니 앱).",
    },
}


def bot_t(locale: BotLocale, key: str) -> str:
    """Return translated string; falls back to English then Simplified Chinese if a key is missing."""
    row = MESSAGES.get(locale) or {}
    if key in row:
        return row[key]
    if key in MESSAGES["en"]:
        return MESSAGES["en"][key]
    return MESSAGES["zh-Hans"][key]
