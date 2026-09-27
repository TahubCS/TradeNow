"""Windows desktop notifications for paper-auto, with no extra dependencies.

The PowerShell script below is a fixed constant, passed with -EncodedCommand so
no command-line quoting can alter it. The title and message travel only in
environment variables and are inserted as XML text nodes or plain properties,
so no value can change the script or the notification's markup. PowerShell is
started by its absolute system path, never looked up on PATH, and never
receives credentials. Every notification is logged, and a failure to show one
never stops a run.
"""

import base64
import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from .logs import redact


logger = logging.getLogger(__name__)
MAX_TITLE = 64
MAX_MESSAGE = 240
MAX_DETAIL = 300
SECRET_PREFIXES = ("TIINGO_", "ALPACA_", "APCA_")
IS_WINDOWS = os.name == "nt"
# Try a modern toast under Windows PowerShell's registered app ID. If Windows
# refuses it, fall back to a classic tray balloon. The script prints which one
# was used, so notify-test can report it.
TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$title = $env:TRADENOW_TOAST_TITLE
$message = $env:TRADENOW_TOAST_MESSAGE
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
    $texts = $xml.GetElementsByTagName('text')
    $texts.Item(0).AppendChild($xml.CreateTextNode($title)) | Out-Null
    $texts.Item(1).AppendChild($xml.CreateTextNode($message)) | Out-Null
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
        [Windows.UI.Notifications.ToastNotification]::new($xml))
    Write-Output 'shown: toast'
} catch {
    $toastError = $_.Exception.Message
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.BalloonTipTitle = $title
    $icon.BalloonTipText = $message
    $icon.Visible = $true
    $icon.ShowBalloonTip(10000)
    Start-Sleep -Seconds 8
    $icon.Dispose()
    Write-Output "shown: balloon (toast failed: $toastError)"
}
"""
ENCODED_SCRIPT = base64.b64encode(TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")

Notifier = Callable[[str, str], object]


def _clean(text: str, limit: int) -> str:
    printable = "".join(char if char.isprintable() else " " for char in redact(text))
    return printable[:limit]


def powershell_path() -> Path | None:
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
    if not root:
        return None
    path = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return path if path.is_file() else None


def _output(data: bytes | str | None) -> str:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return _clean(" ".join((data or "").split()), MAX_DETAIL)


def desktop_notify(title: str, message: str) -> dict:
    """Show a Windows notification; elsewhere, only log it. Returns what happened."""
    title, message = _clean(title, MAX_TITLE), _clean(message, MAX_MESSAGE)
    logger.info("notification: %s: %s", title, message,
                extra={"fields": {"notification": {"title": title, "message": message}}})
    if not IS_WINDOWS:
        return {"shown": False, "detail": "not Windows; logged only"}
    executable = powershell_path()
    if executable is None:
        logger.warning("desktop notification failed: Windows PowerShell not found")
        return {"shown": False, "detail": "Windows PowerShell not found under SystemRoot"}
    # The child never needs credentials, so they are not passed on.
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().startswith(SECRET_PREFIXES)}
    environment.update(TRADENOW_TOAST_TITLE=title, TRADENOW_TOAST_MESSAGE=message)
    try:
        completed = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand",
             ENCODED_SCRIPT],
            env=environment, timeout=30, capture_output=True)
    except (OSError, subprocess.SubprocessError) as error:
        detail = _clean(f"{type(error).__name__}: {error}", MAX_DETAIL)
        logger.warning("desktop notification failed: %s", detail)
        return {"shown": False, "detail": detail}
    stdout, stderr = _output(completed.stdout), _output(completed.stderr)
    if completed.returncode != 0 or not stdout.startswith("shown"):
        detail = f"PowerShell exit {completed.returncode}: {stderr or stdout or 'no output'}"
        logger.warning("desktop notification failed: %s", detail)
        return {"shown": False, "detail": detail}
    return {"shown": True, "detail": stdout, "powershell": str(executable)}
