-- Phase 1: settings, security journal, sessions, vault and the first private table.
-- Migrations are additive only (docs/UPDATES.md). Private columns hold AES-256-GCM ciphertext only.

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Security events, never with private data (docs/SECURITY.md).
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

-- One-time tokens: login links (60 s) and passkey enrollment grants (10 min). SHA-256 digests only.
CREATE TABLE launch_tokens (
    token_hash TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose IN ('login', 'enroll', 'rekey')),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_abs REAL NOT NULL
);

-- Wrapped keys: data keys (versioned), ledger HMAC key, master key copy wrapped by the recovery key.
CREATE TABLE vault_keys (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('data', 'ledger_hmac', 'master_recovery')),
    version INTEGER NOT NULL,
    wrapped BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, version)
);

CREATE TABLE vault_passkeys (
    id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL UNIQUE,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

-- WebAuthn challenges: single use, 2 minutes, bound to the session.
CREATE TABLE vault_challenges (
    challenge_hash TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose IN ('register', 'unlock')),
    session_id TEXT NOT NULL,
    challenge BLOB NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);

CREATE TABLE vault_tokens (
    token_hash TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used REAL NOT NULL,
    expires_abs REAL NOT NULL,
    last_assertion_at REAL NOT NULL
);

CREATE TABLE vault_unlock_failures (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL
);

-- Private: coverage per material (stock in weeks, supplier lead time, price threshold). Encrypted.
CREATE TABLE user_coverage (
    id TEXT PRIMARY KEY,
    material_ref TEXT NOT NULL UNIQUE,
    payload BLOB NOT NULL,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
