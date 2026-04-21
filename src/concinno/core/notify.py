"""concinno.core.notify — Cross-platform notification system.

@module notify
@responsibility Sound playback, toast notifications, session title
    generation, i18n (Win/macOS/Linux)
@dependencies (none — stdlib only)
@exports play_sound, show_toast, make_session_title
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
) -> bool:
    """Windows toast via windows-toasts pip (in-process WinRT, zero PowerShell flash).

    Optional dep: ``pip install windows-toasts``. Falls through to xmldoc
    path when the package is not available, keeping the core library free
    of mandatory runtime deps.
    """
    try:
        from windows_toasts import InteractableWindowsToaster, Toast, ToastDuration
    except ImportError:
        return False
    try:
        toaster = InteractableWindowsToaster(title, app_id)
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
    app_id: str = "AIKing.Concinno.ClaudeCode",
    enabled: bool | None = None,
    tag: str = "concinno",
    group: str = "concinno",
) -> bool:
    """Show a system notification / toast.

    Windows: XmlDocument version (duration=long, no auto-dismiss, system sound).
    Tag+Group: same tag+group replaces previous toast (anti-stack).

    Default ``app_id`` is ``Microsoft.VisualStudioCode`` — Claude Code runs
    inside the VS Code (or Cursor) host process, so toasts are sent under the
    host IDE's identity. This is the officially supported pattern for host
    process toast notifications (see MS Learn: Application User Model IDs,
    "Registering an Application as a Host Process"). Users see a single
    notification source "Visual Studio Code" rather than a separate bucket.

    If VSC's per-AUMID reputation counter gets demoted to Action-Center-only
    (Windows 11 suppresses banners when notification:interaction ratio is bad),
    reset it via:
        Remove-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\
        Notifications\\Settings\\Microsoft.VisualStudioCode' -Name PeriodicNotificationCount

    Pass a different ``app_id`` only if you have registered your own
    AppUserModelID in HKCU/HKCR AppUserModelId and want an isolated
    notification source.

    Args:
        title: Notification title.
        message: Notification body text.
        app_id: Windows AppUserModelId for the toast. Defaults to
            ``Microsoft.VisualStudioCode`` (host IDE identity — user sees
            "Visual Studio Code" as sender).
        enabled: Override enabled check. None = respect config toast_enabled.
        tag: Toast tag for replacement (same tag+group replaces previous).
        group: Toast group for replacement.

    Returns:
        True if notification sent successfully.
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
    # Per-call counter reset — addresses user observation "VSCode AUMID
    # was stable before, bad now" (private AUMID alone doesn't solve it).
    # Real root cause = toast frequency: Concinno's stop-hook pipeline
    # fires per-turn (20-50/day) while 2.0.0-era fired 1-2/session.
    # Any AUMID hits Win11 demote threshold given this rate. Fix: reset
    # counter to 0 just before every dispatch so Windows always sees 0
    # when it reads for banner-vs-action-center routing.
    try:
        from concinno.notify_health import reset_aumid_counter
        reset_aumid_counter(app_id)
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            if _win_toast_winrt(title, message, app_id, tag=tag, group=group):
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
