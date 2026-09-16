"""Account, verification-code, contact and client-session store.

The store is the single place where a server's accounts live, so these tests pin
its contract: unique usernames, emails and ids, codes that expire after five
minutes and may not be requested twice a minute, contacts that are mutual, and
client session tokens that stop working once dropped.
"""

import json
import sqlite3
import time

import pytest

from PyFlow.transfer_web.web_backend import user_database
from PyFlow.transfer_web.web_backend.user_database import (
    CODE_MAX_ATTEMPTS,
    CODE_RESEND_SECONDS,
    CODE_TTL_SECONDS,
    UserDatabase,
    mask_email,
    validate_email,
)


@pytest.fixture
def store(tmp_path):
    db = UserDatabase(str(tmp_path / "flow_web.db"))
    yield db
    db.close()


def _raw(db):
    """Open the underlying database for white-box manipulation of timestamps."""
    return sqlite3.connect(db.path)


def age_codes(db, seconds):
    """Backdate stored codes so cooldown and expiry can be tested without waiting."""
    with _raw(db) as conn:
        conn.execute(
            "UPDATE verification_codes SET created_at = created_at - ?,"
            " expires_at = expires_at - ?",
            (seconds, seconds),
        )


def test_new_store_seeds_the_default_admin(tmp_path):
    db = UserDatabase(str(tmp_path / "flow_web.db"))
    admin = db.find("admin")
    assert admin["role"] == "admin"
    assert admin["user_id"] and admin["user_id"].isupper()
    assert db.authenticate("admin", "admin")["user_id"] == admin["user_id"]
    db.close()


def test_register_assigns_a_unique_id_and_keeps_the_email_unique(store):
    alice = store.register("alice", "Alice@Example.com", "alices-password")
    bob = store.register("bob", "bob@example.com", "bobs-password")
    assert alice["user_id"] != bob["user_id"]
    assert len(alice["user_id"]) == 8
    assert alice["email"] == "Alice@Example.com"
    assert alice["role"] == "user"

    # every identifier form finds the account, case-insensitively
    assert store.find("ALICE")["user_id"] == alice["user_id"]
    assert store.find("alice@example.com")["user_id"] == alice["user_id"]
    assert store.find(alice["user_id"].lower())["user_id"] == alice["user_id"]
    assert store.authenticate("alice@example.com", "alices-password")["username"] == "alice"

    with pytest.raises(ValueError):
        store.register("alice", "other@example.com", "another-password")
    with pytest.raises(ValueError):
        store.register("other", "alice@example.com", "another-password")
    with pytest.raises(ValueError):
        store.register("shorty", "shorty@example.com", "abc")


def test_password_record_is_hashed_and_never_plaintext(store):
    store.register("alice", "alice@example.com", "alices-password")
    with _raw(store) as conn:
        record = conn.execute("SELECT password FROM users WHERE username = 'alice'").fetchone()[0]
    assert record.startswith("pbkdf2_sha256$")
    assert "alices-password" not in record
    assert store.authenticate("alice", "wrong-password") is None
    assert store.authenticate("nobody", "alices-password") is None


def test_code_single_use_and_resend_cooldown(store):
    issued = store.issue_code("register", "carol@example.com")
    assert issued["expires_in"] == CODE_TTL_SECONDS == 300
    assert issued["resend_after"] == CODE_RESEND_SECONDS == 60

    with pytest.raises(ValueError, match="wait"):
        store.issue_code("register", "carol@example.com")

    with pytest.raises(ValueError, match="incorrect"):
        store.verify_code("register", "carol@example.com", "000000" if issued["code"] != "000000"
                          else "111111")

    store.verify_code("register", "carol@example.com", issued["code"])
    with pytest.raises(ValueError, match="request a verification code first"):
        store.verify_code("register", "carol@example.com", issued["code"])  # single use


def test_code_expires_after_five_minutes(store):
    issued = store.issue_code("login", "carol@example.com")
    age_codes(store, CODE_TTL_SECONDS + 1)
    with pytest.raises(ValueError, match="expired"):
        store.verify_code("login", "carol@example.com", issued["code"])

    # the cooldown has passed as well, so a fresh code is issued
    store.issue_code("login", "carol@example.com")


def test_code_attempt_limit(store):
    issued = store.issue_code("login", "carol@example.com")
    wrong = "000000" if issued["code"] != "000000" else "111111"
    for _ in range(CODE_MAX_ATTEMPTS):
        with pytest.raises(ValueError, match="incorrect"):
            store.verify_code("login", "carol@example.com", wrong)
    with pytest.raises(ValueError, match="too many wrong codes"):
        store.verify_code("login", "carol@example.com", issued["code"])


def test_codes_are_purposed_and_cannot_be_borrowed(store):
    issued = store.issue_code("register", "carol@example.com")
    with pytest.raises(ValueError, match="request a verification code first"):
        store.verify_code("reset_password", "carol@example.com", issued["code"])
    with pytest.raises(ValueError, match="unknown verification purpose"):
        store.issue_code("nonsense", "carol@example.com")


def test_discarding_a_code_frees_the_cooldown(store):
    store.issue_code("register", "carol@example.com")
    store.discard_codes("register", "carol@example.com")
    store.issue_code("register", "carol@example.com")  # an undeliverable code does not block


def test_register_with_code_spends_the_code_only_when_the_form_is_valid(store):
    issued = store.issue_code("register", "carol@example.com")
    with pytest.raises(ValueError, match="password"):
        store.register_with_code("carol", "carol@example.com", "short", issued["code"])

    # the rejected attempt left the code usable, and no account behind
    assert store.find("carol") is None
    carol = store.register_with_code(
        "carol", "carol@example.com", "carols-password", issued["code"]
    )
    assert carol["email"] == "carol@example.com"
    assert store.email_registered("CAROL@example.com") is True
    assert store.email_registered("dave@example.com") is False


def test_reset_password_with_code(store):
    carol = store.register("carol", "carol@example.com", "carols-password")
    issued = store.issue_code("reset_password", "carol@example.com")

    with pytest.raises(ValueError, match="incorrect"):
        store.reset_password_with_code(
            "carol", "000000" if issued["code"] != "000000" else "111111", "a-new-password"
        )
    store.reset_password_with_code(carol["user_id"], issued["code"], "a-new-password")
    assert store.authenticate("carol", "carols-password") is None
    assert store.authenticate("carol", "a-new-password")["user_id"] == carol["user_id"]

    # an account without an email address cannot reset its password
    with pytest.raises(ValueError, match="no email address"):
        store.reset_password_with_code("admin", "123456", "another-password")
    with pytest.raises(ValueError, match="no account matches"):
        store.reset_password_with_code("nobody@example.com", "123456", "another-password")


def test_search_finds_accounts_by_every_identifier(store):
    alice = store.register("alice", "alice@example.com", "alices-password")
    bob = store.register("bob", "bob@example.com", "bobs-password")

    assert [u["username"] for u in store.search_users("ali", bob["user_id"])] == ["alice"]
    assert [u["username"] for u in store.search_users("BOB@EXAMPLE", alice["user_id"])] == ["bob"]
    found = store.search_users(bob["user_id"][:4], alice["user_id"])
    assert [u["username"] for u in found] == ["bob"]
    assert store.search_users("alice", alice["user_id"]) == []  # never the searcher itself

    with pytest.raises(ValueError):
        store.search_users("  ", alice["user_id"])
    with pytest.raises(ValueError):
        store.search_users("x" * 65, alice["user_id"])


def test_contacts_are_created_on_accept_and_are_mutual(store):
    alice = store.register("alice", "alice@example.com", "alices-password")
    bob = store.register("bob", "bob@example.com", "bobs-password")

    assert store.contacts(alice["user_id"]) == []
    store.request_contact(alice["user_id"], bob["user_id"])
    assert store.are_contacts(alice["user_id"], bob["user_id"]) is False  # pending, not accepted

    requests = store.contact_requests(bob["user_id"])
    assert [r["user"]["username"] for r in requests["incoming"]] == ["alice"]
    assert requests["outgoing"] == []
    assert store.contact_requests(alice["user_id"])["outgoing"][0]["user"]["username"] == "bob"

    requester = store.respond_request(bob["user_id"], requests["incoming"][0]["id"], True)
    assert requester["username"] == "alice"
    assert store.are_contacts(bob["user_id"], alice["user_id"]) is True
    assert store.are_contacts(alice["user_id"], bob["user_id"]) is True
    assert [c["username"] for c in store.contacts(alice["user_id"])] == ["bob"]

    with pytest.raises(ValueError, match="already answered"):
        store.respond_request(bob["user_id"], requests["incoming"][0]["id"], True)


def test_request_contact_rejects_self_strangers_and_duplicates(store):
    alice = store.register("alice", "alice@example.com", "alices-password")
    bob = store.register("bob", "bob@example.com", "bobs-password")
    carol = store.register("carol", "carol@example.com", "carols-password")

    with pytest.raises(ValueError, match="unknown user"):
        store.request_contact(alice["user_id"], "ZZZZZZZZ")
    with pytest.raises(ValueError, match="your own account"):
        store.request_contact(alice["user_id"], alice["user_id"])

    store.request_contact(alice["user_id"], bob["user_id"])
    with pytest.raises(ValueError, match="already pending"):
        store.request_contact(alice["user_id"], bob["user_id"])

    request_id = store.contact_requests(bob["user_id"])["incoming"][0]["id"]
    store.respond_request(bob["user_id"], request_id, False)
    assert store.are_contacts(alice["user_id"], bob["user_id"]) is False
    assert store.contact_requests(bob["user_id"])["incoming"] == []

    store.request_contact(alice["user_id"], bob["user_id"])  # a rejected request may be repeated
    assert len(store.contact_requests(bob["user_id"])["incoming"]) == 1

    with pytest.raises(ValueError, match="unknown contact request"):
        store.respond_request(carol["user_id"], request_id, True)


def test_sessions_resolve_to_their_account_and_can_be_dropped(store):
    alice = store.register("alice", "alice@example.com", "alices-password")
    token = store.create_session(alice["user_id"])
    assert store.session_user(token)["username"] == "alice"
    assert store.session_user("not-a-token") is None
    assert store.session_user("") is None

    store.drop_session(token)
    assert store.session_user(token) is None
    with pytest.raises(ValueError):
        store.create_session("ZZZZZZZZ")


def test_removing_an_account_takes_its_contacts_and_sessions_with_it(store):
    alice = store.register("alice", "alice@example.com", "alices-password")
    bob = store.register("bob", "bob@example.com", "bobs-password")
    store.request_contact(alice["user_id"], bob["user_id"])
    request_id = store.contact_requests(bob["user_id"])["incoming"][0]["id"]
    store.respond_request(bob["user_id"], request_id, True)
    bob_token = store.create_session(bob["user_id"])

    store.remove_user("alice")
    assert store.find("alice") is None
    assert store.contacts(bob["user_id"]) == []
    assert store.contact_requests(bob["user_id"])["incoming"] == []

    store.remove_user(bob["user_id"])
    assert store.session_user(bob_token) is None


def test_update_credentials_can_change_the_email(store):
    alice = store.register("alice", "alice@example.com", "alices-password")
    bob = store.register("bob", "bob@example.com", "bobs-password")

    updated = store.update_credentials("alice", new_username="alice2", email="alice2@example.com")
    assert (updated["username"], updated["email"]) == ("alice2", "alice2@example.com")
    relogin = store.authenticate("alice2@example.com", "alices-password")
    assert relogin["user_id"] == alice["user_id"]

    with pytest.raises(ValueError, match="already registered"):
        store.update_credentials("alice2", email=bob["email"])
    with pytest.raises(ValueError, match="already exists"):
        store.update_credentials("alice2", new_username="bob")
    with pytest.raises(ValueError, match="unknown user"):
        store.update_credentials("nobody", new_password="another-password")


def test_legacy_users_json_is_imported_once_and_archived(tmp_path):
    legacy = {
        "users": [
            {"username": "admin", "role": "admin", "password": "pbkdf2_sha256$1$salt$deadbeef"},
            {"username": "ghost", "role": "user", "password": ""},  # incomplete, skipped
        ]
    }
    (tmp_path / "users.json").write_text(json.dumps(legacy), encoding="utf-8")

    db = UserDatabase(str(tmp_path / "flow_web.db"))
    assert [u["username"] for u in db.list_users()] == ["admin"]
    assert db.find("admin")["email"] is None
    assert (tmp_path / "users.json.migrated").exists()
    assert not (tmp_path / "users.json").exists()
    db.close()


def test_validate_email_and_mask_email():
    assert validate_email(" someone@example.com ") == "someone@example.com"
    for bad in ["", "no-at-sign", "a@b", "@example.com", "a b@example.com"]:
        with pytest.raises(ValueError):
            validate_email(bad)
    assert mask_email("alice@example.com") == "a***@example.com"
    assert mask_email("") == ""
    assert mask_email(None) == ""


def test_database_file_is_not_world_readable(tmp_path):
    db = UserDatabase(str(tmp_path / "flow_web.db"))
    mode = (tmp_path / "flow_web.db").stat().st_mode
    assert mode & 0o077 == 0
    db.close()


def test_codes_are_stored_hashed(store):
    issued = store.issue_code("register", "carol@example.com")
    with _raw(store) as conn:
        stored = conn.execute("SELECT code_hash, expires_at FROM verification_codes").fetchone()
    assert stored[0] != issued["code"]
    assert issued["code"] not in stored[0]
    assert stored[1] <= time.time() + CODE_TTL_SECONDS
    assert user_database.CODE_DIGITS == 6
