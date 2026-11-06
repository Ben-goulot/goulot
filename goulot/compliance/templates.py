"""Report templates of `config/compliance/` (versioned: a change makes a `_v2` file, never a rewrite)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from goulot.settings import CONFIG_DIR

TEMPLATE_FILE = re.compile(r"^(?P<id>[a-z0-9_]+)_v(?P<version>\d+)\.yaml$")
NOT_TEMPLATES = {"strategic_raw_materials"}
MITIGATION_LABELS = {"stock_de_securite": "Stock de sécurité", "second_fournisseur": "Second fournisseur",
                     "contrat_cadre": "Contrat-cadre", "substitution": "Substitution", "autre": "Autre"}
SUBTITLES = {"article24": "Modèle aligné sur l'article 24 du règlement (UE) 2024/1252 : cartographie, facteurs, "
                          "vulnérabilité, mesures"}


class TemplateError(LookupError):
    pass


def _directory(config_dir: Path | None) -> Path:
    return (config_dir or CONFIG_DIR) / "compliance"


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def available(config_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Latest version of every template, by id."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(_directory(config_dir).glob("*_v*.yaml")):
        m = TEMPLATE_FILE.match(path.name)
        if m is None or m["id"] in NOT_TEMPLATES:
            continue
        data = _load(path)
        if data.get("id") != m["id"] or int(data.get("version", 0)) != int(m["version"]):
            raise TemplateError(f"modèle incohérent : {path.name}")
        if m["id"] not in out or int(m["version"]) > out[m["id"]]["version"]:
            data.setdefault("subtitle", SUBTITLES.get(m["id"], ""))
            out[m["id"]] = data
    return out


def get(template_id: str, config_dir: Path | None = None) -> dict[str, Any]:
    templates = available(config_dir)
    if template_id not in templates:
        raise TemplateError("Modèle inconnu.")
    return templates[template_id]


def strategic_list(config_dir: Path | None = None) -> dict[str, Any]:
    return _load(_directory(config_dir) / "strategic_raw_materials_v1.yaml")


def strategic_match(names: list[str], config_dir: Path | None = None) -> dict[str, Any]:
    """Whether a material looks listed in annex I, from its names only: always « à confirmer »."""
    words = re.compile(r"[\w-]+", re.UNICODE)
    data = strategic_list(config_dir)
    text = " ".join(names).lower()
    tokens = set(words.findall(text))
    for entry in data["entries"]:
        for w in entry["words"]:
            w = str(w).lower()
            if (" " in w and w in text) or w in tokens:
                return {"listed": True, "entry": entry["label"], "source": data["source"]}
    return {"listed": False, "entry": None, "source": data["source"]}
