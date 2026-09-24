"""SMTP delivery of account verification codes for the web tool.

The administrator configures the outgoing mailbox from the server's
startup-configuration page.  The settings are validated with a real SMTP login
and only then written to ``.Flow_Web/email_config.json``; while no validated
configuration is present the service refuses to send, so a server can never
silently drop verification codes.
"""

import json
import os
import smtplib
import ssl
import threading
import traceback
from email.message import EmailMessage

WEB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOW_WEB_DIR = os.path.join(WEB_ROOT, ".Flow_Web")
MAIL_CONFIG_FILE = os.path.join(FLOW_WEB_DIR, "email_config.json")

ENCRYPTION_MODES = ("ssl", "starttls", "none")
DEFAULT_ENCRYPTION = "ssl"
SMTP_TIMEOUT = 10
MAX_PORT = 65535

# Human-readable purpose labels used in the mail body.
PURPOSE_LABELS = {
    "register": "account registration",
    "login": "login",
    "reset_password": "password change",
}


def normalize_config(config):
    """Validate untrusted SMTP settings and return them in canonical form.

    Args:
        config (dict | None): Settings with ``host``, ``port``, ``username``,
            ``password``, ``from`` and ``encryption`` keys. ``from`` defaults to
            the username and ``encryption`` to ``DEFAULT_ENCRYPTION``.

    Returns:
        dict: The same settings with a string host, an integer port, a lowercase
            encryption mode and an explicit sender.

    Raises:
        ValueError: If a setting is missing, malformed or out of range.
    """
    config = config or {}
    host = str(config.get("host") or "").strip()
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    sender = str(config.get("from") or "").strip() or username
    encryption = str(config.get("encryption") or DEFAULT_ENCRYPTION).strip().lower()
    if not host:
        raise ValueError("enter the SMTP server address (host)")
    if encryption not in ENCRYPTION_MODES:
        raise ValueError("the encryption must be one of " + ", ".join(ENCRYPTION_MODES))
    if not username:
        raise ValueError("enter the mailbox account (username)")
    if not password:
        raise ValueError("enter the authorization code or password of the mailbox")
    if "@" not in sender:
        raise ValueError("enter the sender address (from)")
    try:
        port = int(config.get("port"))
    except (TypeError, ValueError) as e:
        raise ValueError("enter a valid SMTP port") from e
    if not 1 <= port <= MAX_PORT:
        raise ValueError(f"the SMTP port must be between 1 and {MAX_PORT}")
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from": sender,
        "encryption": encryption,
    }


class MailService:
    """SMTP client delivering verification codes.

    The stored configuration is read on construction, so a validated mailbox is
    available again after a restart.
    """

    def __init__(self, config_path=None):
        """Create the service and load the stored configuration.

        Args:
            config_path (str | None): JSON file holding the SMTP settings;
                defaults to ``.Flow_Web/email_config.json``.
        """
        self.config_path = config_path or MAIL_CONFIG_FILE
        self._lock = threading.Lock()
        self._config = None
        self._load()

    # ------------------------------------------------------------- internals

    def _load(self):
        """Read a previously validated configuration, disabling on any problem."""
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self._config = normalize_config(json.load(f))
        except Exception as e:
            traceback.print_exc()
            self._config = None
            print(f"verification email: ignoring {self.config_path}: {e}")

    def _connect(self, config):
        """Open an authenticated SMTP connection with the configured encryption."""
        context = ssl.create_default_context()
        if config["encryption"] == "ssl":
            smtp = smtplib.SMTP_SSL(
                config["host"], config["port"], timeout=SMTP_TIMEOUT, context=context
            )
        else:
            smtp = smtplib.SMTP(config["host"], config["port"], timeout=SMTP_TIMEOUT)
            if config["encryption"] == "starttls":
                smtp.starttls(context=context)
        try:
            smtp.login(config["username"], config["password"])
        except Exception:
            smtp.close()
            raise
        return smtp

    def _close(self, smtp):
        """Close an SMTP connection, ignoring a peer that already hung up."""
        try:
            smtp.quit()
        except Exception:
            try:
                smtp.close()
            except Exception:
                pass

    # ----------------------------------------------------------------- public

    def is_enabled(self):
        """Report whether a validated configuration lets codes be sent.

        Returns:
            bool: True when the SMTP settings were validated and stored.
        """
        return self._config is not None

    def get_config(self):
        """Return the validated SMTP settings.

        Returns:
            dict | None: Copy of the stored settings, or ``None`` while the
                service is not configured.
        """
        with self._lock:
            return dict(self._config) if self._config else None

    def configure(self, config):
        """Validate SMTP settings, store them and start the sending service.

        Args:
            config (dict): Settings accepted by `normalize_config`.

        Returns:
            dict: The validated settings as stored.

        Raises:
            ValueError: If a setting is missing or malformed, or the server
                refuses the connection or the login.
        """
        normalized = normalize_config(config)
        try:
            smtp = self._connect(normalized)
        except Exception as e:
            raise ValueError(f"the SMTP settings were rejected: {e}") from e
        self._close(smtp)
        directory = os.path.dirname(self.config_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=4, ensure_ascii=False)
        os.replace(tmp, self.config_path)
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass
        with self._lock:
            self._config = normalized
        return dict(normalized)

    def send_code(self, to_address, code, purpose, expires_in):
        """Send one verification code by email.

        Args:
            to_address (str): Recipient address.
            code (str): Verification code to deliver.
            purpose (str): "register", "login" or "reset_password"; decides the
                wording of the message.
            expires_in (int): Code lifetime in seconds, stated in the message.

        Raises:
            ValueError: If the service is not configured, or the message cannot
                be delivered.
        """
        with self._lock:
            config = dict(self._config) if self._config else None
        if config is None:
            raise ValueError(
                "the verification email service is not configured; ask the server "
                "administrator to set it up"
            )
        label = PURPOSE_LABELS.get(purpose, purpose)
        message = EmailMessage()
        message["Subject"] = "PyFlow verification code"
        message["From"] = config["from"]
        message["To"] = to_address
        message.set_content(
            f"Your PyFlow verification code for {label} is:\n\n"
            f"    {code}\n\n"
            f"The code is valid for {max(1, int(expires_in) // 60)} minutes and can be "
            "used once. If you did not request it, ignore this message.\n"
        )
        try:
            smtp = self._connect(config)
            try:
                smtp.send_message(message)
            finally:
                self._close(smtp)
        except Exception as e:
            raise ValueError(f"sending the verification code to {to_address} failed: {e}") from e
