"""ISO 3166-1 alpha-2 and alpha-3 codes (UN Comtrade reports alpha-3, the chain uses alpha-2), and country names
(goulot/data/country_names.json, generated from the Unicode CLDR names of Intl.DisplayNames)."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

_PAIRS = """
AD AND AE ARE AF AFG AG ATG AI AIA AL ALB AM ARM AO AGO AQ ATA AR ARG AS ASM AT AUT AU AUS AW ABW AX ALA AZ AZE
BA BIH BB BRB BD BGD BE BEL BF BFA BG BGR BH BHR BI BDI BJ BEN BL BLM BM BMU BN BRN BO BOL BQ BES BR BRA BS BHS
BT BTN BV BVT BW BWA BY BLR BZ BLZ CA CAN CC CCK CD COD CF CAF CG COG CH CHE CI CIV CK COK CL CHL CM CMR CN CHN
CO COL CR CRI CU CUB CV CPV CW CUW CX CXR CY CYP CZ CZE DE DEU DJ DJI DK DNK DM DMA DO DOM DZ DZA EC ECU EE EST
EG EGY EH ESH ER ERI ES ESP ET ETH FI FIN FJ FJI FK FLK FM FSM FO FRO FR FRA GA GAB GB GBR GD GRD GE GEO GF GUF
GG GGY GH GHA GI GIB GL GRL GM GMB GN GIN GP GLP GQ GNQ GR GRC GS SGS GT GTM GU GUM GW GNB GY GUY HK HKG HM HMD
HN HND HR HRV HT HTI HU HUN ID IDN IE IRL IL ISR IM IMN IN IND IO IOT IQ IRQ IR IRN IS ISL IT ITA JE JEY JM JAM
JO JOR JP JPN KE KEN KG KGZ KH KHM KI KIR KM COM KN KNA KP PRK KR KOR KW KWT KY CYM KZ KAZ LA LAO LB LBN LC LCA
LI LIE LK LKA LR LBR LS LSO LT LTU LU LUX LV LVA LY LBY MA MAR MC MCO MD MDA ME MNE MF MAF MG MDG MH MHL MK MKD
ML MLI MM MMR MN MNG MO MAC MP MNP MQ MTQ MR MRT MS MSR MT MLT MU MUS MV MDV MW MWI MX MEX MY MYS MZ MOZ NA NAM
NC NCL NE NER NF NFK NG NGA NI NIC NL NLD NO NOR NP NPL NR NRU NU NIU NZ NZL OM OMN PA PAN PE PER PF PYF PG PNG
PH PHL PK PAK PL POL PM SPM PN PCN PR PRI PS PSE PT PRT PW PLW PY PRY QA QAT RE REU RO ROU RS SRB RU RUS RW RWA
SA SAU SB SLB SC SYC SD SDN SE SWE SG SGP SH SHN SI SVN SJ SJM SK SVK SL SLE SM SMR SN SEN SO SOM SR SUR SS SSD
ST STP SV SLV SX SXM SY SYR SZ SWZ TC TCA TD TCD TF ATF TG TGO TH THA TJ TJK TK TKL TL TLS TM TKM TN TUN TO TON
TR TUR TT TTO TV TUV TW TWN TZ TZA UA UKR UG UGA UM UMI US USA UY URY UZ UZB VA VAT VC VCT VE VEN VG VGB VI VIR
VN VNM VU VUT WF WLF WS WSM YE YEM YT MYT ZA ZAF ZM ZMB ZW ZWE
""".split()  # noqa: SIM905

ISO3_TO_ISO2: dict[str, str] = {_PAIRS[i + 1]: _PAIRS[i] for i in range(0, len(_PAIRS), 2)}
ISO2_CODES: frozenset[str] = frozenset(ISO3_TO_ISO2.values())

_NAMES_FILE = Path(__file__).resolve().parent.parent / "data" / "country_names.json"
# Names used by GDACS and the United Nations that differ from the CLDR ones.
_ALIASES = {
    "bolivia plurinational state of": "BO", "brunei darussalam": "BN", "cabo verde": "CV", "cape verde": "CV",
    "congo": "CG", "congo brazzaville": "CG", "republic of the congo": "CG", "congo kinshasa": "CD",
    "democratic republic of congo": "CD", "democratic republic of the congo": "CD", "cote d ivoire": "CI",
    "ivory coast": "CI", "czech republic": "CZ", "eswatini": "SZ", "swaziland": "SZ", "gambia": "GM",
    "the gambia": "GM", "iran islamic republic of": "IR", "korea": "KR", "republic of korea": "KR",
    "south korea": "KR",
    "north korea": "KP", "democratic people s republic of korea": "KP", "lao people s democratic republic": "LA",
    "laos": "LA", "macedonia": "MK", "moldova republic of": "MD", "republic of moldova": "MD", "myanmar": "MM",
    "burma": "MM", "palestine": "PS", "occupied palestinian territory": "PS", "russia": "RU",
    "russian federation": "RU", "syrian arab republic": "SY", "tanzania united republic of": "TZ",
    "united republic of tanzania": "TZ", "timor leste": "TL", "east timor": "TL", "turkey": "TR",
    "united kingdom": "GB",
    "uk": "GB", "united states": "US", "united states of america": "US", "usa": "US",
    "venezuela bolivarian republic of": "VE",
    "viet nam": "VN", "vietnam": "VN", "bosnia and herzegovina": "BA", "sao tome and principe": "ST",
    "saint vincent and the grenadines": "VC", "trinidad and tobago": "TT", "antigua and barbuda": "AG",
}


def _norm(name: str) -> str:
    folded = "".join(c for c in unicodedata.normalize("NFKD", name.lower()) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z]+", " ", folded).split())


@lru_cache(maxsize=1)
def _names() -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = json.loads(_NAMES_FILE.read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def _by_name() -> dict[str, str]:
    return {**{_norm(name): iso for iso, name in _names()["en"].items()}, **_ALIASES}


def iso2_from_name(name: str) -> str | None:
    """ISO2 of an English country name, or None. External text, only looked up."""
    return _by_name().get(_norm(name))


def name_fr(iso2: str) -> str:
    return _names()["fr"].get(iso2, iso2)
