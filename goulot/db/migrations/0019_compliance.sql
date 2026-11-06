-- 6 November 2026, phase 6: compliance export (docs/SPEC.md, section 10).
-- `compliance_exports` keeps what an export was (template, materials, basis, fingerprint, ledger entry), never its
-- content. `mitigations` holds one encrypted profile: mitigation choices per material, company and signatory
-- (AES-256-GCM, bound to the logical field `compliance_profile.v1` and to the record UUID; rule 13).
CREATE TABLE compliance_exports (
    id TEXT PRIMARY KEY,                 -- UUID
    created_at TEXT NOT NULL,
    template_id TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    basis TEXT NOT NULL CHECK (basis IN ('typical', 'my_chain')),
    material_ids TEXT NOT NULL,          -- JSON list
    draft INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,           -- SHA-256 of the report content (JSON annex)
    ledger_seq INTEGER NOT NULL
);

CREATE TABLE mitigations (
    id TEXT PRIMARY KEY,                 -- UUID, bound into the ciphertext
    payload BLOB NOT NULL,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
