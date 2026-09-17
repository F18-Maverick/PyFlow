"""SQLite account, verification-code, contact and client-session store.

A PyFlow web server keeps every account in ``.Flow_Web/flow_web.db``: the
administrator console logins and the client accounts registered from the
server's landing page.  Each account carries a public user id next to its
username and email, and is looked up by any of the three.

A missing database file is created and seeded with the default administrator
(``DEFAULT_ADMIN_USERNAME`` / ``DEFAULT_ADMIN_PASSWORD``); a legacy
``.Flow_Web/users.json`` is imported once and renamed.  A database file that
exists but cannot be read is never re-seeded, so a damaged store cannot silently
restore the default account.
"""

import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import traceback

WEB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOW_WEB_DIR = os.path.join(WEB_ROOT, ".Flow_Web")
USER_DB_FILE = os.path.join(FLOW_WEB_DIR, "flow_web.db")
LEGACY_USERS_FILE = os.path.join(FLOW_WEB_DIR, "users.json")

# The first run seeds this administrator, and the console warns while the seeded
# pair (and only that pair) is still in use.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"

MIN_PASSWORD_LENGTH = 8
PBKDF2_ITERATIONS = 200000
USER_ID_BYTES = 4  # 8 hexadecimal characters
SESSION_TOKEN_BYTES = 32

# Verification codes: one lifetime for every purpose, one per minute and target.
CODE_PURPOSES = ("register", "login", "reset_password")
CODE_DIGITS = 6
CODE_TTL_SECONDS = 300
CODE_RESEND_SECONDS = 60
CODE_MAX_ATTEMPTS = 5

SEARCH_RESULT_LIMIT = 20

_USERNAME_RE = re.compile(r"\S{1,64}")
_EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY COLLATE NOCASE,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        email TEXT UNIQUE COLLATE NOCASE,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verification_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purpose TEXT NOT NULL,
        target TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contacts (
        owner_id TEXT NOT NULL COLLATE NOCASE,
        contact_id TEXT NOT NULL COLLATE NOCASE,
        created_at REAL NOT NULL,
        PRIMARY KEY (owner_id, contact_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contact_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_id TEXT NOT NULL COLLATE NOCASE,
        to_id TEXT NOT NULL COLLATE NOCASE,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL,
        UNIQUE (from_id, to_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_tokens (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL COLLATE NOCASE,
        created_at REAL NOT NULL
    )
    """,
)

SEARCH_RESULT_LIMIT = 20
SEARCH_QUERY_MAX_LENGTH = 64


def hash_password(password, salt, iterations=PBKDF2_ITERATIONS):
    """Build a PBKDF2-SHA256 password record.

    Args:
        password (str): Plain password to hash.
        salt (str): Per-account salt, stored in the record.
        iterations (int): PBKDF2 iteration count. Defaults to ``PBKDF2_ITERATIONS``.

    Returns:
        str: Record shaped ``pbkdf2_sha256$<iterations>$<salt>$<hex digest>``.
    """
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def new_password_record(password):
    """Build a password record with a fresh random salt.

    Args:
        password (str): Plain password to hash.

    Returns:
        str: Record produced by `hash_password` with a new 16-byte hex salt.
    """
    return hash_password(password, secrets.token_hex(16))


@functools.lru_cache(maxsize=1)
def dummy_password_record():
    """Return a throwaway record, so an unknown account costs a real verification."""
    return new_password_record(secrets.token_hex(32))


def verify_password(password, record):
    """Check a plain password against a stored record.

    Args:
        password (str): Plain password to check.
        record (str): Record produced by `hash_password`.

    Returns:
        bool: True when the password matches the record; False for a malformed
            or mismatching record.
    """
    try:
        algorithm, iterations, salt, digest = record.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        )
    except Exception:
        return False
    return hmac.compare_digest(expected.hex(), digest)


def validate_username(username):
    """Validate an account username.

    Args:
        username (str): Login name to validate.

    Returns:
        str: The username with surrounding whitespace removed.

    Raises:
        ValueError: If the username is not 1-64 characters without whitespace.
    """
    username = (username or "").strip()
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("the username must be 1-64 characters without spaces")
    return username


def validate_email(email):
    """Validate an email address.

    Args:
        email (str): Address to validate.

    Returns:
        str: The address with surrounding whitespace removed.

    Raises:
        ValueError: If the address is not ``local@domain.tld`` shaped.
    """
    email = (email or "").strip()
    if not _EMAIL_RE.fullmatch(email):
        raise ValueError("enter a valid email address")
    return email


def validate_password(password):
    """Validate a plain password.

    Args:
        password (str): Password to validate.

    Returns:
        str: The password unchanged.

    Raises:
        ValueError: If the password is shorter than ``MIN_PASSWORD_LENGTH``.
    """
    password = password or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"the password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def mask_email(email):
    """Hide the local part of an address for display.

    Args:
        email (str | None): Address to mask.

    Returns:
        str: ``a***@example.com`` shaped text, or an empty string when ``email``
            is empty.
    """
    email = (email or "").strip()
    if "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _public(entry):
    """Convert a stored row into the account dictionary handed to callers."""
    return {
        "user_id": entry["user_id"],
        "username": entry["username"],
        "email": entry["email"],
        "role": entry["role"],
    }


def _code_hash(purpose, target, code):
    """Hash a verification code together with its purpose and target."""
    payload = f"{purpose}\n{target}\n{code}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _like_pattern(query):
    """Escape a search query for a case-insensitive LIKE substring match."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class UserDatabase:
    """SQLite account store with verification codes, contacts and client sessions.

    Every method is safe to call from several threads; writes are serialized on
    one connection.
    """

    def __init__(self, path=None):
        """Open or create the account database.

        Args:
            path (str | None): Database file to use; defaults to
                ``.Flow_Web/flow_web.db``. The parent directory is created on
                demand, and a missing file is seeded with the default
                administrator.
        """
        self.path = path or USER_DB_FILE
        directory = os.path.dirname(self.path)
        self.legacy_users_file = (
            os.path.join(directory, "users.json") if directory else LEGACY_USERS_FILE
        )
        self._lock = threading.RLock()
        self._conn = None
        self._error = None
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                for statement in _SCHEMA:
                    self._conn.execute(statement)
                self._conn.commit()
            self._import_legacy_users()
            if self._count("users") == 0:
                self._seed_default_admin()
        except sqlite3.Error as e:
            traceback.print_exc()
            self._close_connection()
            self._error = f"the account database {self.path} cannot be read: {e}"
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------- internals

    def _require_db(self):
        """Raise when the database file could not be opened."""
        if self._conn is None:
            raise ValueError(self._error or f"the account database {self.path} is unavailable")

    def _execute(self, sql, params=()):
        """Run one writing statement and commit it."""
        self._require_db()
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _fetch(self, sql, params=()):
        """Run one reading statement and return the first row or ``None``."""
        self._require_db()
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql, params=()):
        """Run one reading statement and return every row."""
        self._require_db()
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _count(self, table):
        """Return the number of rows in a table."""
        row = self._fetch(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - table names are internal
        return row["n"] if row else 0

    def _insert_user(self, username, email, password_record, role):
        """Insert one account row and return its public dictionary."""
        validate_username(username)
        if email is not None:
            validate_email(email)
        if self._fetch("SELECT 1 FROM users WHERE username = ?", (username,)) is not None:
            raise ValueError(f"user {username} already exists")
        if email is not None and (
            self._fetch("SELECT 1 FROM users WHERE email = ?", (email,)) is not None
        ):
            raise ValueError(f"the email {email} is already registered")
        user_id = self._new_user_id()
        try:
            self._execute(
                "INSERT INTO users (user_id, username, email, password, role, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    username,
                    email,
                    password_record,
                    role if role in ("admin", "user") else "user",
                    time.time(),
                ),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"the account cannot be created: {e}") from e
        return self.find(user_id)

    def _new_user_id(self):
        """Return a public user id no account uses yet."""
        while True:
            user_id = secrets.token_hex(USER_ID_BYTES).upper()
            if self._fetch("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) is None:
                return user_id

    def _find_row(self, identify):
        """Return the stored row matching a user id, username or email."""
        identify = (identify or "").strip()
        if not identify:
            return None
        return self._fetch(
            "SELECT * FROM users WHERE user_id = ? OR username = ? OR email = ? LIMIT 1",
            (identify, identify, identify),
        )

    def _import_legacy_users(self):
        """Import a legacy ``users.json`` next to the database and archive it."""
        legacy = self.legacy_users_file
        if self._count("users") > 0 or not os.path.exists(legacy):
            return
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            traceback.print_exc()
            return
        entries = data.get("users") if isinstance(data, dict) else None
        imported = 0
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            username, password = entry.get("username"), entry.get("password")
            if not username or not password:
                continue
            try:
                self._insert_user(username, None, password, entry.get("role"))
                imported += 1
            except ValueError:
                continue
        if imported:
            archived = legacy + ".migrated"
            try:
                if not os.path.exists(archived):
                    os.replace(legacy, archived)
            except OSError:
                traceback.print_exc()
            print(f"account store: imported {imported} account(s) from users.json")

    def _seed_default_admin(self):
        """Create the default administrator in an empty store."""
        self._insert_user(
            DEFAULT_ADMIN_USERNAME,
            None,
            new_password_record(DEFAULT_ADMIN_PASSWORD),
            "admin",
        )

    # -------------------------------------------------------------- accounts

    def register(self, username, email, password):
        """Create an account from the registration form.

        Args:
            username (str): Login name, 1-64 characters without spaces and
                unique in the store.
            email (str): Address the verification code was sent to; unique in
                the store.
            password (str): Plain password, at least ``MIN_PASSWORD_LENGTH``
                characters.

        Returns:
            dict: New account without its password (``user_id``, ``username``,
                ``email``, ``role``).

        Raises:
            ValueError: If a field is malformed or the username or email is
                already registered.
        """
        validate_password(password)
        return self._insert_user(
            validate_username(username),
            validate_email(email),
            new_password_record(password),
            "user",
        )

    def email_registered(self, email):
        """Report whether an email address already belongs to an account.

        Args:
            email (str): Address to look up.

        Returns:
            bool: True when one account uses the address.

        Raises:
            ValueError: If the address is malformed.
        """
        email = validate_email(email)
        return self._fetch("SELECT 1 FROM users WHERE email = ?", (email,)) is not None

    def register_with_code(self, username, email, password, code):
        """Register an account after consuming the code sent to its email.

        The code is only spent once every field is valid, so a rejected
        registration does not cost the user a new code.

        Args:
            username (str): Login name, 1-64 characters without spaces and
                unique in the store.
            email (str): Address the verification code was sent to; unique in
                the store.
            password (str): Plain password, at least ``MIN_PASSWORD_LENGTH``
                characters.
            code (str): Code delivered for the ``register`` purpose.

        Returns:
            dict: New account without its password.

        Raises:
            ValueError: If a field is malformed, the username or email is taken,
                or the code is wrong, expired or missing.
        """
        username = validate_username(username)
        email = validate_email(email)
        validate_password(password)
        with self._lock:
            self.verify_code("register", email, code)
            return self.register(username, email, password)

    def reset_password_with_code(self, identify, code, new_password):
        """Set a new password after consuming the code sent to the account email.

        Args:
            identify (str): User id, username or email address of the account.
            code (str): Code delivered for the ``reset_password`` purpose.
            new_password (str): Replacement plain password, at least
                ``MIN_PASSWORD_LENGTH`` characters.

        Returns:
            dict: Updated account without its password.

        Raises:
            ValueError: If no account matches ``identify``, it has no email
                address, the new password is malformed, or the code is wrong,
                expired or missing.
        """
        account = self._find_row(identify)
        if account is None:
            raise ValueError("no account matches that user name or email")
        if not account["email"]:
            raise ValueError("this account has no email address; ask an administrator")
        validate_password(new_password)
        with self._lock:
            self.verify_code("reset_password", account["email"], code)
            return self.update_credentials(account["user_id"], new_password=new_password)

    def add_user(self, username, email, password, role="user"):
        """Create an account on behalf of an administrator.

        Args:
            username (str): Login name, 1-64 characters without spaces and
                unique in the store.
            email (str): Unique address of the account.
            password (str): Plain password, at least ``MIN_PASSWORD_LENGTH``
                characters.
            role (str): "admin" or "user"; anything else is stored as "user".

        Returns:
            dict: New account without its password.

        Raises:
            ValueError: If a field is malformed or the username or email is
                already registered.
        """
        validate_password(password)
        return self._insert_user(
            validate_username(username), validate_email(email), new_password_record(password), role
        )

    def find(self, identify):
        """Look an account up by user id, username or email.

        Args:
            identify (str): User id, username or email address; matched
                case-insensitively.

        Returns:
            dict | None: Account without its password, or ``None`` when nothing
                matches.
        """
        entry = self._find_row(identify)
        return _public(entry) if entry is not None else None

    def authenticate(self, identify, password):
        """Check a password against one account.

        Args:
            identify (str): User id, username or email address.
            password (str): Plain password to check.

        Returns:
            dict | None: Account without its password when the password is
                correct, otherwise ``None``.
        """
        entry = self._find_row(identify)
        if entry is None:
            verify_password(password, dummy_password_record())
            return None
        if not verify_password(password, entry["password"]):
            return None
        return _public(entry)

    def list_users(self):
        """List every account sorted by username.

        Returns:
            list[dict]: Accounts without their passwords.
        """
        rows = self._fetchall("SELECT * FROM users ORDER BY username")
        return [_public(row) for row in rows]

    def update_credentials(self, identify, new_username=None, new_password=None, email=None):
        """Update the username, password or email of one account.

        Args:
            identify (str): User id, username or email address of the account.
            new_username (str | None): Replacement login name; ``None`` keeps
                the current one.
            new_password (str | None): Replacement plain password; ``None``
                keeps the current one.
            email (str | None): Replacement address; ``None`` keeps the current
                one.

        Returns:
            dict: Updated account without its password.

        Raises:
            ValueError: If the account is unknown, a replacement is malformed,
                or the new username or email is already in use.
        """
        entry = self._find_row(identify)
        if entry is None:
            raise ValueError(f"unknown user {identify}")
        fields = {}
        if new_username is not None:
            new_username = validate_username(new_username)
            taken = self._fetch(
                "SELECT 1 FROM users WHERE username = ? AND user_id <> ?",
                (new_username, entry["user_id"]),
            )
            if taken is not None:
                raise ValueError(f"user {new_username} already exists")
            fields["username"] = new_username
        if email is not None:
            email = validate_email(email)
            taken = self._fetch(
                "SELECT 1 FROM users WHERE email = ? AND user_id <> ?", (email, entry["user_id"])
            )
            if taken is not None:
                raise ValueError(f"the email {email} is already registered")
            fields["email"] = email
        if new_password is not None:
            fields["password"] = new_password_record(validate_password(new_password))
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            self._execute(
                f"UPDATE users SET {assignments} WHERE user_id = ?",  # noqa: S608 - column names are internal
                (*fields.values(), entry["user_id"]),
            )
        return self.find(entry["user_id"])

    def remove_user(self, identify):
        """Delete one account, keeping at least one administrator.

        Args:
            identify (str): User id, username or email address.

        Raises:
            ValueError: If the account is unknown or it is the last
                administrator.
        """
        entry = self._find_row(identify)
        if entry is None:
            raise ValueError(f"unknown user {identify}")
        if entry["role"] == "admin":
            admins = self._fetch("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'")["n"]
            if admins <= 1:
                raise ValueError("the last administrator cannot be removed")
        self._execute("DELETE FROM users WHERE user_id = ?", (entry["user_id"],))
        self._execute(
            "DELETE FROM contacts WHERE owner_id = ? OR contact_id = ?",
            (entry["user_id"], entry["user_id"]),
        )
        self._execute(
            "DELETE FROM contact_requests WHERE from_id = ? OR to_id = ?",
            (entry["user_id"], entry["user_id"]),
        )
        self._execute("DELETE FROM client_tokens WHERE user_id = ?", (entry["user_id"],))

    # ---------------------------------------------------- verification codes

    def issue_code(self, purpose, target):
        """Store a fresh verification code for a purpose and target.

        Args:
            purpose (str): One of ``CODE_PURPOSES``.
            target (str): Email address the code is delivered to.

        Returns:
            dict: ``{"code", "expires_in", "resend_after"}`` with the plaintext
                code, its lifetime in seconds and the cooldown before another
                code may be requested.

        Raises:
            ValueError: If the purpose or target is invalid, or a code for the
                same purpose and target was issued less than
                ``CODE_RESEND_SECONDS`` ago.
        """
        if purpose not in CODE_PURPOSES:
            raise ValueError(f"unknown verification purpose {purpose}")
        target = validate_email(target).lower()
        with self._lock:
            latest = self._fetch(
                "SELECT created_at FROM verification_codes WHERE purpose = ? AND target = ?"
                " ORDER BY id DESC LIMIT 1",
                (purpose, target),
            )
            if latest is not None:
                elapsed = time.time() - latest["created_at"]
                if elapsed < CODE_RESEND_SECONDS:
                    wait = int(CODE_RESEND_SECONDS - elapsed) + 1
                    raise ValueError(f"wait {wait} seconds before requesting another code")
            self._execute("DELETE FROM verification_codes WHERE expires_at < ?", (time.time(),))
            code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
            self._execute(
                "INSERT INTO verification_codes"
                " (purpose, target, code_hash, created_at, expires_at, used, attempts)"
                " VALUES (?, ?, ?, ?, ?, 0, 0)",
                (
                    purpose,
                    target,
                    _code_hash(purpose, target, code),
                    time.time(),
                    time.time() + CODE_TTL_SECONDS,
                ),
            )
        return {"code": code, "expires_in": CODE_TTL_SECONDS, "resend_after": CODE_RESEND_SECONDS}

    def verify_code(self, purpose, target, code):
        """Consume the verification code of a purpose and target.

        Args:
            purpose (str): One of ``CODE_PURPOSES``.
            target (str): Email address the code was sent to.
            code (str): Code as typed by the user.

        Raises:
            ValueError: If no code was requested for the pair, it expired, the
                code is wrong, or ``CODE_MAX_ATTEMPTS`` wrong entries were
                already made.
        """
        if purpose not in CODE_PURPOSES:
            raise ValueError(f"unknown verification purpose {purpose}")
        target = validate_email(target).lower()
        code = (code or "").strip()
        with self._lock:
            entry = self._fetch(
                "SELECT * FROM verification_codes WHERE purpose = ? AND target = ? AND used = 0"
                " ORDER BY id DESC LIMIT 1",
                (purpose, target),
            )
            if entry is None:
                raise ValueError("request a verification code first")
            if entry["expires_at"] < time.time():
                raise ValueError("the verification code has expired, request a new one")
            if entry["attempts"] >= CODE_MAX_ATTEMPTS:
                raise ValueError("too many wrong codes, request a new one")
            if not hmac.compare_digest(entry["code_hash"], _code_hash(purpose, target, code)):
                self._execute(
                    "UPDATE verification_codes SET attempts = attempts + 1 WHERE id = ?",
                    (entry["id"],),
                )
                raise ValueError("the verification code is incorrect")
            self._execute("UPDATE verification_codes SET used = 1 WHERE id = ?", (entry["id"],))

    def discard_codes(self, purpose, target):
        """Delete every stored code of a purpose and target.

        Used when the code could not be delivered, so the cooldown does not
        block an immediate retry.

        Args:
            purpose (str): One of ``CODE_PURPOSES``.
            target (str): Email address the code was sent to.
        """
        target = validate_email(target).lower()
        self._execute(
            "DELETE FROM verification_codes WHERE purpose = ? AND target = ?", (purpose, target)
        )

    # -------------------------------------------------------------- contacts

    def search_users(self, query, exclude_user_id=None):
        """Find accounts by user id, username or email substring.

        Args:
            query (str): Text matched case-insensitively inside the user id,
                username or email; 1-64 characters after trimming.
            exclude_user_id (str | None): Account to leave out of the results,
                usually the searcher's own.

        Returns:
            list[dict]: At most ``SEARCH_RESULT_LIMIT`` accounts without their
                passwords, sorted by username.

        Raises:
            ValueError: If the query is empty or longer than 64 characters.
        """
        query = (query or "").strip()
        if not query:
            raise ValueError("enter a user id, username or email to search for")
        if len(query) > SEARCH_QUERY_MAX_LENGTH:
            raise ValueError("the search text must be at most 64 characters")
        pattern = _like_pattern(query)
        rows = self._fetchall(
            "SELECT * FROM users WHERE (user_id LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\'"
            " OR email LIKE ? ESCAPE '\\') AND user_id <> ? ORDER BY username LIMIT ?",
            (pattern, pattern, pattern, exclude_user_id or "", SEARCH_RESULT_LIMIT),
        )
        return [_public(row) for row in rows]

    def contacts(self, user_id):
        """List the accounts one account is allowed to see.

        Args:
            user_id (str): Owner account.

        Returns:
            list[dict]: Contact accounts without their passwords, sorted by
                username.
        """
        rows = self._fetchall(
            "SELECT users.* FROM contacts JOIN users ON users.user_id = contacts.contact_id"
            " WHERE contacts.owner_id = ? ORDER BY users.username",
            (user_id,),
        )
        return [_public(row) for row in rows]

    def are_contacts(self, user_id, other_user_id):
        """Report whether two accounts are contacts of each other.

        Args:
            user_id (str): First account.
            other_user_id (str): Second account.

        Returns:
            bool: True when ``user_id`` lists ``other_user_id`` as a contact.
        """
        row = self._fetch(
            "SELECT 1 FROM contacts WHERE owner_id = ? AND contact_id = ?",
            (user_id, other_user_id),
        )
        return row is not None

    def request_contact(self, from_user_id, to_user_id):
        """Ask another account to become a contact.

        Args:
            from_user_id (str): Account sending the request.
            to_user_id (str): Account the request is addressed to.

        Returns:
            dict: Target account without its password.

        Raises:
            ValueError: If the target is unknown, it is the sender's own
                account, the two are already contacts, or a request is already
                pending.
        """
        target = self._find_row(to_user_id)
        if target is None:
            raise ValueError(f"unknown user {to_user_id}")
        if target["user_id"] == from_user_id:
            raise ValueError("you cannot add your own account")
        if self.are_contacts(from_user_id, target["user_id"]):
            raise ValueError(f"{target['username']} is already a contact")
        with self._lock:
            existing = self._fetch(
                "SELECT * FROM contact_requests WHERE from_id = ? AND to_id = ?",
                (from_user_id, target["user_id"]),
            )
            if existing is not None and existing["status"] == "pending":
                raise ValueError(f"a request to {target['username']} is already pending")
            if existing is not None:
                self._execute(
                    "UPDATE contact_requests SET status = 'pending', created_at = ? WHERE id = ?",
                    (time.time(), existing["id"]),
                )
            else:
                self._execute(
                    "INSERT INTO contact_requests (from_id, to_id, status, created_at)"
                    " VALUES (?, ?, 'pending', ?)",
                    (from_user_id, target["user_id"], time.time()),
                )
        return _public(target)

    def contact_requests(self, user_id):
        """List the pending contact requests of one account.

        Args:
            user_id (str): Account whose requests are read.

        Returns:
            dict: ``{"incoming": [...], "outgoing": [...]}`` where each entry is
                ``{"id", "user", "created_at"}`` and ``user`` is the other
                account without its password.
        """
        incoming = self._fetchall(
            "SELECT contact_requests.id AS request_id,"
            " contact_requests.created_at AS asked_at, users.*"
            " FROM contact_requests JOIN users ON users.user_id = contact_requests.from_id"
            " WHERE contact_requests.to_id = ? AND contact_requests.status = 'pending'"
            " ORDER BY contact_requests.created_at",
            (user_id,),
        )
        outgoing = self._fetchall(
            "SELECT contact_requests.id AS request_id,"
            " contact_requests.created_at AS asked_at, users.*"
            " FROM contact_requests JOIN users ON users.user_id = contact_requests.to_id"
            " WHERE contact_requests.from_id = ? AND contact_requests.status = 'pending'"
            " ORDER BY contact_requests.created_at",
            (user_id,),
        )
        return {
            "incoming": [
                {"id": row["request_id"], "user": _public(row), "created_at": row["asked_at"]}
                for row in incoming
            ],
            "outgoing": [
                {"id": row["request_id"], "user": _public(row), "created_at": row["asked_at"]}
                for row in outgoing
            ],
        }

    def respond_request(self, user_id, request_id, accept):
        """Accept or reject a pending contact request addressed to one account.

        Accepting records the contact for both accounts.

        Args:
            user_id (str): Account answering the request.
            request_id (int): Request id as reported by `contact_requests`.
            accept (bool): True to accept the request, False to reject it.

        Returns:
            dict: Requester account without its password.

        Raises:
            ValueError: If the request is unknown, is not addressed to
                ``user_id``, or is not pending any more.
        """
        entry = self._fetch("SELECT * FROM contact_requests WHERE id = ?", (request_id,))
        if entry is None or entry["to_id"] != user_id:
            raise ValueError("unknown contact request")
        if entry["status"] != "pending":
            raise ValueError("this contact request was already answered")
        requester = self._find_row(entry["from_id"])
        if requester is None:
            raise ValueError("the requesting account no longer exists")
        now = time.time()
        with self._lock:
            if accept:
                self._execute(
                    "INSERT OR IGNORE INTO contacts (owner_id, contact_id, created_at)"
                    " VALUES (?, ?, ?)",
                    (entry["from_id"], entry["to_id"], now),
                )
                self._execute(
                    "INSERT OR IGNORE INTO contacts (owner_id, contact_id, created_at)"
                    " VALUES (?, ?, ?)",
                    (entry["to_id"], entry["from_id"], now),
                )
            self._execute(
                "UPDATE contact_requests SET status = ? WHERE id = ?",
                ("accepted" if accept else "rejected", request_id),
            )
        return _public(requester)

    # ------------------------------------------------------ client sessions

    def create_session(self, user_id):
        """Open a client session for one account.

        Args:
            user_id (str): Account the session belongs to.

        Returns:
            str: Session token; clients send it back with every request.

        Raises:
            ValueError: If the account is unknown.
        """
        entry = self._find_row(user_id)
        if entry is None:
            raise ValueError(f"unknown user {user_id}")
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        self._execute(
            "INSERT INTO client_tokens (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, entry["user_id"], time.time()),
        )
        return token

    def session_user(self, token):
        """Resolve a client session token.

        Args:
            token (str): Token returned by `create_session`.

        Returns:
            dict | None: Account without its password, or ``None`` when the
                token is unknown.
        """
        token = (token or "").strip()
        if not token:
            return None
        row = self._fetch(
            "SELECT users.* FROM client_tokens JOIN users ON users.user_id = client_tokens.user_id"
            " WHERE client_tokens.token = ?",
            (token,),
        )
        return _public(row) if row is not None else None

    def drop_session(self, token):
        """Invalidate one client session token.

        Args:
            token (str): Token returned by `create_session`; an unknown token is
                ignored.
        """
        self._execute("DELETE FROM client_tokens WHERE token = ?", ((token or "").strip(),))

    def _close_connection(self):
        """Close and forget the database connection, if one is open."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    traceback.print_exc()
                self._conn = None

    def close(self):
        """Close the database connection."""
        self._close_connection()
