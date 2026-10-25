"""Release chain (docs/SECURITY.md, threats 14 and 15; docs/UPDATES.md): workflows pinned and least privileged,
signature in an isolated job, additive migrations only, the signing key never on a command line."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from goulot.settings import REPO_ROOT
from goulot.updater import signing

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict[str, object]:
    data: dict[str, object] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return data


def test_actions_pinned_by_commit() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for uses in re.findall(r"uses:\s*(\S+)", path.read_text(encoding="utf-8")):
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses), f"{path.name}: {uses}"


def test_workflow_permissions_least_privilege() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        data = _workflow(path.name)
        assert data["permissions"] in ({}, {"contents": "read"}), path.name
    release = _workflow("release.yml")
    jobs = release["jobs"]
    assert isinstance(jobs, dict)
    build, sign = jobs["build"], jobs["sign"]
    assert build["permissions"] == {"contents": "read", "id-token": "write", "attestations": "write"}
    assert "environment" not in build and "secrets." not in yaml.safe_dump(build)
    assert sign["environment"] == "release" and sign["permissions"]["contents"] == "write"
    # The signing job runs no repository code and installs nothing: no checkout, no setup action, no package.
    text = yaml.safe_dump(sign)
    for forbidden in ("actions/checkout", "setup-uv", "setup-node", "npm ", "uv ", "pip "):
        assert forbidden not in text, forbidden
    assert "gh attestation verify" in text and "openssl pkeyutl -sign" in text and "shred -u" in text
    raw = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "--source-ref refs/heads/main" in raw and "--deny-self-hosted-runners" in raw
    assert '--target "$GITHUB_SHA"' in raw  # the release is tied to the commit of this run, not to an artifact


def test_dependabot_cooldown() -> None:
    data = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {u["package-ecosystem"]: u for u in data["updates"]}
    assert set(ecosystems) == {"uv", "npm", "github-actions"}
    assert all(u["cooldown"]["default-days"] >= 7 for u in ecosystems.values())
    automerge = (WORKFLOWS / "dependabot-automerge.yml").read_text(encoding="utf-8")
    assert "dependabot[bot]" in automerge and "semver-patch" in automerge
    # Only GitHub's auto-merge, which waits for the required checks; `gh pr merge` would merge at once.
    assert "enablePullRequestAutoMerge" in automerge and "gh pr merge" not in automerge


def test_migrations_additive_only() -> None:
    forbidden = re.compile(r"\b(DROP\s+(TABLE|COLUMN|INDEX|TRIGGER|VIEW)|RENAME|UPDATE\s+ledger|DELETE\s+FROM\s+"
                           r"ledger|ALTER\s+TABLE\s+\w+\s+DROP)\b", re.IGNORECASE)
    for path in sorted((REPO_ROOT / "goulot" / "db" / "migrations").glob("*.sql")):
        sql = "\n".join(line.split("--", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())
        assert not forbidden.search(sql), path.name


def test_signing_key_never_on_a_command_line(tmp_path: Path) -> None:
    trust = tmp_path / "trust.py"
    trust.write_text((REPO_ROOT / "goulot" / "updater" / "trust.py").read_text(encoding="utf-8"), encoding="utf-8")
    signing.write_trusted(trust, [])  # a first key, as on a new installation
    seen: list[tuple[list[str], str | None]] = []

    def runner(args: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        seen.append((args, stdin))
        exists = args[:2] == [signing.SECURITY, "find-generic-password"]
        return subprocess.CompletedProcess(args, 44 if exists else 0, "", "")

    pair = signing.keygen(trust, "owner/repo", "/opt/homebrew/bin/gh", runner=runner)
    secret_hex, secret_pem = pair.private_raw.hex(), pair.private_pem
    assert all(secret_hex not in " ".join(a) and "PRIVATE KEY" not in " ".join(a) for a, _ in seen)
    stdins = [s for _, s in seen if s]
    assert any(secret_hex in s and '-T ""' in s for s in stdins)  # keychain copy, no trusted application
    assert secret_pem in stdins  # GitHub secret, through stdin
    assert signing.read_trusted(trust) == [(pair.key_id, pair.public_b64)]
    assert secret_hex not in trust.read_text(encoding="utf-8")
