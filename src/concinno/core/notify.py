"""concinno.core.notify — Cross-platform notification system.

@module notify
@responsibility Sound playback, toast notifications, session title
    generation, i18n (Win/macOS/Linux)
@dependencies (none — stdlib only)
@exports play_sound, show_toast, make_session_title,
    notify_waiting_on_user
"""

import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# ── i18n strings ──

_STRINGS = {
    "en": {
        "response_ready": "Response ready",
        "session_done": "Session done",
        "fallback_title": "Claude Code",
    },
    "zh-TW": {
        "response_ready": "已生成回應",
        "session_done": "Session 完成",
        "fallback_title": "Claude Code",
    },
    "zh-CN": {
        "response_ready": "已生成回复",
        "session_done": "Session 完成",
        "fallback_title": "Claude Code",
    },
    "ja": {
        "response_ready": "応答完了",
        "session_done": "セッション完了",
        "fallback_title": "Claude Code",
    },
    "ko": {
        "response_ready": "응답 완료",
        "session_done": "세션 완료",
        "fallback_title": "Claude Code",
    },
}


def _get_locale() -> str:
    """Resolve display locale from layered sources with normalization.

    Order (first non-empty wins): ``CC_UX_LANG`` env → ``concinno.i18n.get_locale``
    (reads ``~/.concinno/locale.json`` + env) → ``get_config().raw("locale")`` →
    ``"en"``. Returns a key that keys into ``_STRINGS`` (``en`` / ``zh-TW`` /
    ``zh-CN`` / ``ja`` / ``ko``) — handles loose forms ``zh_TW``, ``zh-tw``,
    ``zh``, ``zh-hant`` etc transparently.
    """
    raw = ""
    env = os.environ.get("CC_UX_LANG", "").strip()
    if env:
        raw = env
    if not raw:
        try:
            from concinno.i18n import get_locale as _i18n_locale
            raw = (_i18n_locale() or "").strip()
        except Exception:
            pass
    if not raw:
        try:
            from concinno.core.config import get_config
            raw = (get_config().raw("locale", "") or "").strip()
        except Exception:
            pass
    norm = raw.replace("_", "-").lower()
    if norm in ("zh-tw", "zh", "zh-hant", "zh-hant-tw"):
        return "zh-TW"
    if norm in ("zh-cn", "zh-hans", "zh-hans-cn"):
        return "zh-CN"
    if norm.startswith("ja"):
        return "ja"
    if norm.startswith("ko"):
        return "ko"
    return "en"


def _t(key: str, locale: str | None = None) -> str:
    """Get translated string by key."""
    loc = locale or _get_locale()
    strings = _STRINGS.get(loc, _STRINGS["en"])
    return strings.get(key, _STRINGS["en"].get(key, key))


# ── Session title ──


def make_session_title(prefix: str = "cc", tz_offset_hours: int = 8) -> str:
    """Generate session title in unified format: <prefix>_XXXX_HHMM.

    Matches the format used by ``session.generate_session_id()``.

    Args:
        prefix: Project abbreviation prefix (default "cc").
        tz_offset_hours: Timezone offset from UTC (default +8 for Asia/Taipei).

    Returns:
        e.g. "cc_a3f1_1425"
    """
    tz = timezone(timedelta(hours=tz_offset_hours))
    now = datetime.now(tz)
    hex4 = secrets.token_hex(2)  # 4 hex chars
    return f"{prefix}_{hex4}_{now.strftime('%H%M')}"


def extract_first_user_message(transcript_path: str, max_len: int = 40) -> str:
    """Extract first real user message from Claude Code transcript.

    Args:
        transcript_path: Path to JSONL transcript file.
        max_len: Max chars before truncation.

    Returns:
        First user message text, or empty string.
    """
    skip_prefixes = (
        "<ide_",
        "<gitStatus>",
        "<system-reminder>",
        "<available-deferred-tools>",
        "gitStatus:",
    )
    tp = os.path.expanduser(transcript_path)
    if not os.path.isfile(tp):
        return ""
    try:
        with open(tp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("type") != "user":
                    continue
                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", "")
                text = _extract_text(content, skip_prefixes)
                if text:
                    if len(text) > max_len:
                        text = text[: max_len - 3] + "..."
                    return text
    except Exception:
        pass
    return ""


def _extract_text(content, skip_prefixes) -> str:
    """Extract first non-IDE text from content (str or list)."""
    texts = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ]
    for text in texts:
        text = text.strip()
        if not text:
            continue
        if any(text.startswith(pfx) for pfx in skip_prefixes):
            continue
        return text.split("\n")[0].strip()
    return ""


# ── Sound ──


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def play_sound(sound_file: str) -> bool:
    """Play a notification sound file.

    Args:
        sound_file: Path to audio file (mp3/wav).

    Returns:
        True if playback initiated successfully.
    """
    if not os.path.isfile(sound_file):
        return False
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f'(New-Object Media.SoundPlayer "{sound_file}").PlaySync()',
                ],
                creationflags=_CREATE_NO_WINDOW,
                startupinfo=_hidden_startupinfo(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["afplay", sound_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["paplay", sound_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True
    except Exception:
        return False


# ── Toast ──


def _xml_escape(s: str) -> str:
    """Escape XML and PowerShell special chars."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "")
    )


def _win_toast_winrt(
    title: str,
    message: str,
    app_id: str,
    tag: str = "concinno",
    group: str = "concinno",
    display_name: str = "Visual Studio Code",
) -> bool:
    """Windows toast via windows-toasts pip (in-process WinRT, zero disk artefact).

    2.14.0 fix: splits ``display_name`` (UI sender label — goes into the
    WinRT ``applicationText`` slot) from ``app_id`` (reputation key — the
    ``notifierAUMID``). Before 2.14.0 this helper passed ``title`` into
    ``applicationText``, which is why the banner sender label randomly
    followed the message title.

    Optional dep: ``pip install windows-toasts``. Returns ``False`` when the
    package is missing so the caller cascades to the legacy xmldoc path.
    """
    try:
        from windows_toasts import InteractableWindowsToaster, Toast, ToastDuration
    except ImportError:
        return False
    try:
        toaster = InteractableWindowsToaster(display_name, app_id)
        toast = Toast(text_fields=[title, message], duration=ToastDuration.Long)
        toast.tag = tag
        toast.group = group
        toaster.show_toast(toast)
        return True
    except Exception:
        return False


def _win_toast_xmldoc(
    title: str,
    message: str,
    app_id: str,
    tag: str = "concinno",
    group: str = "concinno",
) -> bool:
    """Windows toast via XmlDocument (duration=long, no auto-dismiss, system sound).

    Uses wscript.exe + VBS wrapper to avoid PowerShell window flash.
    Tag+Group ensures same-category toasts replace instead of stacking.
    """
    safe_title = _xml_escape(title)
    safe_msg = _xml_escape(message)

    # PowerShell: WinRT types + XmlDocument toast
    winrt_ns = "Windows.UI.Notifications"
    xml_ns = "Windows.Data.Xml.Dom.XmlDocument"
    ps = (
        f"$null = [{winrt_ns}.ToastNotificationManager,"
        f" {winrt_ns}, ContentType = WindowsRuntime]\n"
        f"$null = [{xml_ns}, {xml_ns},"
        " ContentType = WindowsRuntime]\n"
        '$template = @"\n'
        '<toast duration="long" scenario="reminder"><visual>'
        '<binding template="ToastGeneric">'
        f"<text>{safe_title}</text>"
        f"<text>{safe_msg}</text>"
        "</binding></visual></toast>\n"
        '"@\n'
        "$xml = New-Object"
        " Windows.Data.Xml.Dom.XmlDocument\n"
        "$xml.LoadXml($template)\n"
        f"$toast = [{winrt_ns}.ToastNotification]"
        "::new($xml)\n"
        f'$toast.Tag = "{tag}"\n'
        f'$toast.Group = "{group}"\n'
        f"[{winrt_ns}.ToastNotificationManager]"
        f'::CreateToastNotifier("{app_id}").Show($toast)'
    )
    try:
        encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        vbs_content = (
            'CreateObject("WScript.Shell").Run '
            f'"powershell -NoProfile -EncodedCommand {encoded}", 0, False'
        )
        vbs_path = os.path.join(tempfile.gettempdir(), "concinno_toast.vbs")
        with open(vbs_path, "w", encoding="ascii") as f:
            f.write(vbs_content)
        subprocess.Popen(
            ["wscript", vbs_path],
            creationflags=_CREATE_NO_WINDOW,
            startupinfo=_hidden_startupinfo(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def register_aumid(
    app_id: str = "Concinno.ClaudeCode",
    display_name: str = "Claude Code",
    icon_path: str | None = None,
    icon_background_color: str = "0060A0",
) -> bool:
    """Register a custom AUMID in HKCU for stable toast reputation.

    Idempotent. Safe on non-Windows (returns ``False``). Writes
    ``HKCU\\Software\\Classes\\AppUserModelId\\<app_id>`` with
    ``DisplayName`` / ``IconUri`` (optional) / ``IconBackgroundColor``.

    Only needed when the caller overrides ``show_toast(app_id=...)`` with a
    custom AUMID — the VS Code default AUMID already has this registry
    entry registered by the VS Code installer.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key_path = rf"Software\Classes\AppUserModelId\{app_id}"
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        )
        try:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
            if icon_path:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon_path)
            winreg.SetValueEx(
                key, "IconBackgroundColor", 0, winreg.REG_SZ, icon_background_color
            )
            return True
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def disable_smart_optout() -> bool:
    """Disable Win11 Notification Suggestions (SmartOptOut) for the current user.

    Writes ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Notifications\\
    Settings\\Windows.ActionCenter.SmartOptOut\\Enabled = 0`` (DWORD) so
    Windows 11 22H2+ stops auto-demoting banners to Action-Center-only
    based on per-AUMID interaction ratios. This is the single-call answer
    to "why does my toast silently stop appearing".

    Opt-in because it changes user-visible OS behaviour beyond this
    library; ``show_toast`` never invokes it implicitly. Safe on
    non-Windows (returns ``False``).
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key_path = (
            r"Software\Microsoft\Windows\CurrentVersion\Notifications"
            r"\Settings\Windows.ActionCenter.SmartOptOut"
        )
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        )
        try:
            winreg.SetValueEx(key, "Enabled", 0, winreg.REG_DWORD, 0)
            return True
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _win_toast_balloon(title: str, message: str, timeout_ms: int = 5000) -> bool:
    """Fallback: .NET BalloonTip via NotifyIcon. Fire-and-forget (non-blocking)."""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip({timeout_ms}, "
        f"'{_xml_escape(title)}', '{_xml_escape(message)}', "
        "'Warning'); "
        "Start-Sleep -Milliseconds 5000; "
        "$n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps],
            creationflags=_CREATE_NO_WINDOW,
            startupinfo=_hidden_startupinfo(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def show_toast(
    title: str,
    message: str,
    app_id: str = "Microsoft.VisualStudioCode",
    enabled: bool | None = None,
    tag: str = "concinno",
    group: str = "concinno",
    display_name: str = "Visual Studio Code",
) -> bool:
    """Show a system notification / toast.

    2.14.0 fallback chain (Windows, in order):

    1. **WinRT in-process** (``windows-toasts`` pip, optional extra
       ``concinno[toast]``) — zero disk, zero subprocess, zero AV surface.
    2. **XmlDocument via wscript+VBS** (legacy, no pip deps) — CCC 1.12.1
       baseline. Drops a ``.vbs`` to ``%TEMP%`` which Avast-family AV
       scans on sight (Surfshark etc).
    3. **NotifyIcon balloon tip** (last resort) — Win10 legacy style.

    Default ``app_id`` is ``Microsoft.VisualStudioCode`` so Action Center
    attributes banners to the VS Code host (sender label "Visual Studio
    Code" + VSC icon). To prevent Win11 SmartOptOut demotion of the shared
    bucket, call :func:`disable_smart_optout` once at bootstrap.

    Tag + group let the same (tag, group) pair replace an earlier toast
    instead of stacking.

    Args:
        title: Notification title (first text line).
        message: Notification body text (second text line).
        app_id: AUMID used as reputation key. Pass a custom value +
            :func:`register_aumid` to isolate from VS Code's bucket.
        enabled: Override enabled check. None = respect config
            ``toast_enabled``.
        tag: Toast tag for replacement.
        group: Toast group for replacement.
        display_name: UI sender label shown in the banner header.

    Returns:
        True if any tier succeeded, False otherwise.
    """
    if enabled is False:
        return False
    if enabled is None:
        try:
            from concinno.core.config import get_config

            if not get_config().toast_enabled:
                return False
        except Exception:
            pass
    # 2.14.0: Tier 1 winrt (zero .vbs, no AV surface) → Tier 2 xmldoc
    # (legacy fallback when windows-toasts not installed) → Tier 3 balloon
    # (last resort). Diagnosis: Opus 1 archaeology + Opus 2 WinRT research
    # + Opus 3 architecture design (session 5a619784, MEMORY #70).
    try:
        if sys.platform == "win32":
            if _win_toast_winrt(
                title, message, app_id, tag=tag, group=group,
                display_name=display_name,
            ):
                return True
            if _win_toast_xmldoc(title, message, app_id, tag=tag, group=group):
                return True
            return _win_toast_balloon(title, message)
        elif sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                timeout=5,
                capture_output=True,
            )
        else:
            subprocess.run(
                ["notify-send", title, message],
                timeout=5,
                capture_output=True,
            )
        return True
    except Exception:
        return False


# ── Generic "waiting on user" helper (2.21.0) ───────────────────────

_WAITING_TOAST_STRINGS = {
    "en": "Claude is waiting — input needed",
    "zh-TW": "Claude 在等你 — 需要回應",
    "zh-CN": "Claude 在等你 — 需要回应",
    "ja": "Claude が応答待ち — 入力が必要",
    "ko": "Claude가 대기 중 — 입력 필요",
}


def notify_waiting_on_user(
    context: str,
    *,
    title: str | None = None,
    tag: str = "concinno-waiting-on-user",
    group: str = "concinno-waiting-on-user",
    async_fire: bool = True,
) -> bool:
    """Surface a toast when the agent needs the user to respond / decide.

    Distinct from :func:`show_toast` (low-level) and
    ``hooks.ask_user_toast.maybe_show_ask_user_toast`` (Claude Code
    ``AskUserQuestion`` tool only). Call this from any code path that
    is *about to block* on user input — ``release_authorization``
    deny branches, destruction-guard confirmation prompts,
    custom CLI wizards — so the user learns to respond without
    foreground-watching the terminal.

    Args:
        context: Short human-readable reason (<=80 chars preferred);
            e.g. ``"publish concinno 2.21.0 needs 'go publish ...'"``.
        title: Optional override. Default is the locale-aware
            ``"Claude is waiting — input needed"`` style title.
        tag/group: Toast replacement keys — a burst of prompts
            collapses to ONE toast instead of stacking.
        async_fire: Fire on a daemon thread (default) so the caller
            does not block on COM / WinRT cold init. Set False for
            deterministic testing.

    Returns:
        ``True`` when a toast was emitted (or queued for async fire),
        ``False`` on hard error or when toasts are disabled in config.
    """
    try:
        locale = _get_locale()
    except Exception:
        locale = "en"
    resolved_title = (
        title
        if title is not None
        else _WAITING_TOAST_STRINGS.get(locale, _WAITING_TOAST_STRINGS["en"])
    )
    message = (context or "").strip()[:120] or resolved_title

    def _fire() -> bool:
        try:
            return show_toast(
                title=resolved_title,
                message=message,
                tag=tag,
                group=group,
            )
        except Exception:
            return False

    if not async_fire:
        return _fire()

    import threading
    try:
        threading.Thread(
            target=_fire,
            name="concinno-waiting-on-user-toast",
            daemon=True,
        ).start()
        return True
    except Exception:
        return False
