"""Passkeys (WebAuthn) that open the vault (docs/SECURITY.md, passkey).

rpId `localhost`, exact origin `http://localhost:<port>`, user verification required, attestation `none`,
32-byte single-use challenge valid 2 minutes. The `webauthn` library (Duo Labs) checks signature, origin,
rpId and flags; the app stores the challenge (consumed at the first attempt) and the signature counter.
5 failures in 10 minutes block unlocking for 15 minutes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from goulot.db.connection import Database, get_setting, set_setting, utcnow
from goulot.security.audit import audit
from goulot.security.clock import Clock
from goulot.security.tokens import digest
from goulot.settings import (
    UNLOCK_BLOCK_DURATION,
    UNLOCK_FAILURE_WINDOW,
    UNLOCK_MAX_FAILURES,
    WEBAUTHN_CHALLENGE_TTL,
)

RP_ID = "localhost"
RP_NAME = "Goulot"
CHALLENGE_BYTES = 32


class PasskeyError(Exception):
    pass


class UnlockBlocked(PasskeyError):
    def __init__(self, until: float) -> None:
        super().__init__("Ouverture bloquée")
        self.until = until


@dataclass(frozen=True)
class PasskeyInfo:
    id: str
    label: str
    created_at: str
    last_used_at: str | None


class PasskeyService:
    def __init__(self, db: Database, clock: Clock, origin: str) -> None:
        self.db = db
        self.clock = clock
        self.origin = origin

    # --- challenges ------------------------------------------------------------------------------------
    def _new_challenge(self, conn: sqlite3.Connection, purpose: str, session_id: str) -> bytes:
        now = self.clock.time()
        conn.execute("DELETE FROM vault_challenges WHERE expires_at < ? OR used_at IS NOT NULL", (now,))
        conn.execute("DELETE FROM vault_challenges WHERE session_id = ? AND purpose = ?", (session_id, purpose))
        challenge = os.urandom(CHALLENGE_BYTES)
        conn.execute(
            "INSERT INTO vault_challenges (challenge_hash, purpose, session_id, challenge, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (digest(challenge.hex()), purpose, session_id, challenge, now, now + WEBAUTHN_CHALLENGE_TTL),
        )
        return challenge

    def _consume_challenge(self, conn: sqlite3.Connection, purpose: str, session_id: str) -> bytes | None:
        """The pending challenge of this session, consumed whatever the outcome of the verification."""
        now = self.clock.time()
        row = conn.execute(
            "SELECT challenge_hash, challenge FROM vault_challenges WHERE session_id = ? AND purpose = ? "
            "AND used_at IS NULL AND expires_at >= ? ORDER BY created_at DESC LIMIT 1",
            (session_id, purpose, now),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE vault_challenges SET used_at = ? WHERE challenge_hash = ?", (now, row["challenge_hash"]))
        return bytes(row["challenge"])

    # --- registration ----------------------------------------------------------------------------------
    def count(self) -> int:
        with self.db.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM vault_passkeys WHERE revoked_at IS NULL").fetchone()[0])

    def registration_options(self, session_id: str) -> str:
        with self.db.transaction() as conn:
            user_id = get_setting(conn, "vault_user_id")
            if not user_id:
                raise PasskeyError("Coffre non configuré.")
            existing = [
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"]))
                for r in conn.execute("SELECT credential_id FROM vault_passkeys WHERE revoked_at IS NULL")
            ]
            challenge = self._new_challenge(conn, "register", session_id)
        options = generate_registration_options(
            rp_id=RP_ID,
            rp_name=RP_NAME,
            user_name="goulot",
            user_display_name="Coffre Goulot",
            user_id=bytes.fromhex(user_id),
            challenge=challenge,
            timeout=WEBAUTHN_CHALLENGE_TTL * 1000,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=existing,
        )
        return options_to_json(options)

    def verify_registration(self, session_id: str, credential: dict[str, Any], label: str) -> str:
        with self.db.transaction() as conn:
            challenge = self._consume_challenge(conn, "register", session_id)
        if challenge is None:
            raise PasskeyError("Défi expiré : recommencez.")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=RP_ID,
                expected_origin=self.origin,
                require_user_verification=True,
            )
        except (InvalidRegistrationResponse, ValueError, KeyError, TypeError) as exc:
            with self.db.transaction() as conn:
                audit(conn, "passkey_enrollment_failed")
            raise PasskeyError("Enregistrement refusé.") from exc
        passkey_id = str(uuid.uuid4())
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO vault_passkeys (id, credential_id, public_key, sign_count, label, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    passkey_id,
                    bytes_to_base64url(verified.credential_id),
                    verified.credential_public_key,
                    verified.sign_count,
                    label[:60] or "Clé d'accès",
                    utcnow(),
                ),
            )
            # Every enrollment is announced (alert in phase 3) and listed in Settings.
            audit(conn, "passkey_enrolled", passkey_id=passkey_id)
        return passkey_id

    # --- unlocking -------------------------------------------------------------------------------------
    def blocked_until(self) -> float | None:
        with self.db.connect() as conn:
            raw = get_setting(conn, "vault_blocked_until")
        until = float(raw) if raw else 0.0
        return until if until > self.clock.time() else None

    def authentication_options(self, session_id: str) -> str:
        until = self.blocked_until()
        if until is not None:
            raise UnlockBlocked(until)
        with self.db.transaction() as conn:
            creds = [
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"]))
                for r in conn.execute("SELECT credential_id FROM vault_passkeys WHERE revoked_at IS NULL")
            ]
            if not creds:
                raise PasskeyError("Aucune clé d'accès enregistrée.")
            challenge = self._new_challenge(conn, "unlock", session_id)
        options = generate_authentication_options(
            rp_id=RP_ID,
            challenge=challenge,
            timeout=WEBAUTHN_CHALLENGE_TTL * 1000,
            allow_credentials=creds,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return options_to_json(options)

    def _record_failure(self) -> None:
        now = self.clock.time()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO vault_unlock_failures (at) VALUES (?)", (now,))
            conn.execute("DELETE FROM vault_unlock_failures WHERE at < ?", (now - UNLOCK_FAILURE_WINDOW,))
            failures = conn.execute(
                "SELECT COUNT(*) FROM vault_unlock_failures WHERE at >= ?", (now - UNLOCK_FAILURE_WINDOW,)
            ).fetchone()[0]
            audit(conn, "vault_unlock_failed")
            if failures >= UNLOCK_MAX_FAILURES:
                set_setting(conn, "vault_blocked_until", str(now + UNLOCK_BLOCK_DURATION))
                conn.execute("DELETE FROM vault_unlock_failures")
                audit(conn, "vault_unlock_blocked", minutes=UNLOCK_BLOCK_DURATION // 60)

    def verify_authentication(self, session_id: str, credential: dict[str, Any]) -> str:
        """Returns the passkey id. Raises PasskeyError (failure counted) or UnlockBlocked."""
        until = self.blocked_until()
        if until is not None:
            raise UnlockBlocked(until)
        with self.db.transaction() as conn:
            challenge = self._consume_challenge(conn, "unlock", session_id)
        try:
            if challenge is None:
                raise PasskeyError("Défi expiré : recommencez.")
            raw_id = credential.get("rawId") or credential.get("id")
            if not isinstance(raw_id, str):
                raise PasskeyError("Réponse incomplète.")
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT id, public_key, sign_count FROM vault_passkeys WHERE credential_id = ? "
                    "AND revoked_at IS NULL",
                    (bytes_to_base64url(base64url_to_bytes(raw_id)),),
                ).fetchone()
            if row is None:
                raise PasskeyError("Clé d'accès inconnue ou révoquée.")
            try:
                verified = verify_authentication_response(
                    credential=credential,
                    expected_challenge=challenge,
                    expected_rp_id=RP_ID,
                    expected_origin=self.origin,
                    credential_public_key=bytes(row["public_key"]),
                    credential_current_sign_count=int(row["sign_count"]),
                    require_user_verification=True,
                )
            except (InvalidAuthenticationResponse, ValueError, KeyError, TypeError) as exc:
                raise PasskeyError("Signature refusée.") from exc
        except PasskeyError:
            self._record_failure()
            raise
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE vault_passkeys SET sign_count = ?, last_used_at = ? WHERE id = ?",
                (verified.new_sign_count, utcnow(), row["id"]),
            )
            conn.execute("DELETE FROM vault_unlock_failures")
        return str(row["id"])

    # --- management ------------------------------------------------------------------------------------
    def list(self) -> list[PasskeyInfo]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, label, created_at, last_used_at FROM vault_passkeys WHERE revoked_at IS NULL "
                "ORDER BY created_at"
            ).fetchall()
        return [PasskeyInfo(r["id"], r["label"], r["created_at"], r["last_used_at"]) for r in rows]

    def revoke(self, passkey_id: str) -> None:
        with self.db.transaction() as conn:
            active = conn.execute("SELECT COUNT(*) FROM vault_passkeys WHERE revoked_at IS NULL").fetchone()[0]
            if active <= 1:
                raise PasskeyError("Gardez au moins une clé d'accès.")
            cur = conn.execute(
                "UPDATE vault_passkeys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (utcnow(), passkey_id)
            )
            if cur.rowcount != 1:
                raise PasskeyError("Clé d'accès introuvable.")
            audit(conn, "passkey_revoked", passkey_id=passkey_id)


def parse_options(options_json: str) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(options_json)
    return parsed
