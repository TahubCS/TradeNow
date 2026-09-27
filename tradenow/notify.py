"""Windows desktop notifications for paper-auto, with no extra dependencies.

The PowerShell script below is a fixed constant. The title and message are
passed in environment variables and inserted as XML text nodes, so no value can
change the script or the notification's markup. PowerShell is started by its
absolute system path, never looked up on PATH. Every notification is also
logged, and a failure to show one never stops a run.
"""

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from .logs import redact


logger = logging.getLogger(__name__)
MAX_TITLE = 64
MAX_MESSAGE = 240
SECRET_PREFIXES = ("TIINGO_", "ALPACA_", "APCA_")
IS_WINDOWS = os.name == "nt"
# Windows PowerShell's registered app ID, so toasts work without installing anything.
TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:TRADENOW_TOAST_TITLE)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:TRADENOW_TOAST_MESSAGE)) | Out-Null
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
"""

Notifier = Callable[[str, str], None]


def _clean(text: str, limit: int) -> str:
    printable = "".join(char if char.isprintable() else " " for char in redact(text))
    return printable[:limit]


def powershell_path() -> Path | None:
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
    if not root:
        return None
    path = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return path if path.is_file() else None


def desktop_notify(title: str, message: str) -> None:
    """Show a Windows toast; elsewhere, only log it."""
    title, message = _clean(title, MAX_TITLE), _clean(message, MAX_MESSAGE)
    logger.info("notification: %s: %s", title, message,
                extra={"fields": {"notification": {"title": title, "message": message}}})
    executable = powershell_path() if IS_WINDOWS else None
    if executable is None:
        return
    # The child never needs credentials, so they are not passed on.
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().startswith(SECRET_PREFIXES)}
    environment.update(TRADENOW_TOAST_TITLE=title, TRADENOW_TOAST_MESSAGE=message)
    try:
        subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command",
                        TOAST_SCRIPT], env=environment, timeout=20, check=True,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("desktop notification failed: %s", type(error).__name__)
