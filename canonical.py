"""Canonical match-key resolution. The single source of truth for joining venues."""
from datetime import datetime, timezone

# ISO-3 team codes as canonical keys. Covers all 48 FIFA World Cup 2026 participants.
# Maps every source's native team string -> canonical ISO-3 code.
# Sources: Kalshi subtitles, Polymarket questions, SportMonks fixture names, OddsPortal.
# Add new spellings as you encounter them; raise KeyError is intentional.
TEAM_ALIASES: dict[str, str] = {
    # ── CONCACAF hosts ──────────────────────────────────────────────────────────
    "United States":          "USA", "USA":                   "USA",
    "USMNT":                  "USA", "US":                    "USA",
    "United States of America":"USA",

    "Canada":                 "CAN", "CAN":                   "CAN",

    "Mexico":                 "MEX", "MEX":                   "MEX",
    "México":                 "MEX",

    # ── CONCACAF (non-host qualifiers) ──────────────────────────────────────────
    "Haiti":                  "HAI", "HAI":                   "HAI",
    "HTI":                    "HAI",
    "Honduras":               "HON", "HON":                   "HON",
    "Panama":                 "PAN", "PAN":                   "PAN",
    "Jamaica":                "JAM", "JAM":                   "JAM",
    "Costa Rica":             "CRC", "CRC":                   "CRC",
    "El Salvador":            "SLV", "SLV":                   "SLV",
    "Cuba":                   "CUB", "CUB":                   "CUB",
    "Curaçao":                "CUW", "CUW":                   "CUW",
    "Curacao":                "CUW",

    # ── CONMEBOL ─────────────────────────────────────────────────────────────────
    "Argentina":              "ARG", "ARG":                   "ARG",
    "Brazil":                 "BRA", "BRA":                   "BRA",
    "Brasil":                 "BRA",
    "Uruguay":                "URU", "URU":                   "URU",
    "Colombia":               "COL", "COL":                   "COL",
    "Ecuador":                "ECU", "ECU":                   "ECU",
    "Venezuela":              "VEN", "VEN":                   "VEN",
    "Paraguay":               "PAR", "PAR":                   "PAR",
    "Bolivia":                "BOL", "BOL":                   "BOL",
    "Chile":                  "CHI", "CHI":                   "CHI",
    "Peru":                   "PER", "PER":                   "PER",

    # ── UEFA ─────────────────────────────────────────────────────────────────────
    "Germany":                "GER", "GER":                   "GER",
    "Spain":                  "ESP", "ESP":                   "ESP",
    "España":                 "ESP",
    "Portugal":               "POR", "POR":                   "POR",
    "France":                 "FRA", "FRA":                   "FRA",
    "England":                "ENG", "ENG":                   "ENG",
    "Netherlands":            "NED", "NED":                   "NED",
    "Holland":                "NED",
    "Belgium":                "BEL", "BEL":                   "BEL",
    "Italy":                  "ITA", "ITA":                   "ITA",
    "Austria":                "AUT", "AUT":                   "AUT",
    "Croatia":                "CRO", "CRO":                   "CRO",
    "Hungary":                "HUN", "HUN":                   "HUN",
    "Serbia":                 "SRB", "SRB":                   "SRB",
    "Denmark":                "DEN", "DEN":                   "DEN",
    "Poland":                 "POL", "POL":                   "POL",
    "Slovakia":               "SVK", "SVK":                   "SVK",
    "Ukraine":                "UKR", "UKR":                   "UKR",
    "Switzerland":            "SUI", "SUI":                   "SUI",
    "Romania":                "ROU", "ROU":                   "ROU",
    "Scotland":               "SCO", "SCO":                   "SCO",
    "Albania":                "ALB", "ALB":                   "ALB",
    "Turkey":                 "TUR", "TUR":                   "TUR",
    "Türkiye":                "TUR",
    "Greece":                 "GRE", "GRE":                   "GRE",
    "Czech Republic":         "CZE", "CZE":                   "CZE",
    "Czechia":                "CZE",
    "Slovenia":               "SVN", "SVN":                   "SVN",
    "Norway":                 "NOR", "NOR":                   "NOR",
    "Sweden":                 "SWE", "SWE":                   "SWE",
    "Finland":                "FIN", "FIN":                   "FIN",

    # ── AFC ──────────────────────────────────────────────────────────────────────
    "Japan":                  "JPN", "JPN":                   "JPN",
    "Korea Republic":         "KOR", "KOR":                   "KOR",
    "South Korea":            "KOR",
    "Korea":                  "KOR",
    "Iran":                   "IRN", "IRN":                   "IRN",
    "Australia":              "AUS", "AUS":                   "AUS",
    "Saudi Arabia":           "KSA", "KSA":                   "KSA",
    "Iraq":                   "IRQ", "IRQ":                   "IRQ",
    "Jordan":                 "JOR", "JOR":                   "JOR",
    "Uzbekistan":             "UZB", "UZB":                   "UZB",
    "Qatar":                  "QAT", "QAT":                   "QAT",
    "Oman":                   "OMA", "OMA":                   "OMA",
    "Bahrain":                "BHR", "BHR":                   "BHR",
    "China":                  "CHN", "CHN":                   "CHN",
    "China PR":               "CHN",
    "Indonesia":              "IDN", "IDN":                   "IDN",
    "Palestine":              "PSE", "PSE":                   "PSE",
    "India":                  "IND", "IND":                   "IND",
    "United Arab Emirates":   "UAE", "UAE":                   "UAE",
    "Kuwait":                 "KUW", "KUW":                   "KUW",

    # ── CAF ──────────────────────────────────────────────────────────────────────
    "Morocco":                "MAR", "MAR":                   "MAR",
    "Egypt":                  "EGY", "EGY":                   "EGY",
    "Nigeria":                "NGA", "NGA":                   "NGA",
    "Senegal":                "SEN", "SEN":                   "SEN",
    "DR Congo":               "COD", "COD":                   "COD",
    "Democratic Republic of Congo": "COD",
    "Congo DR":               "COD",
    "South Africa":           "RSA", "RSA":                   "RSA",
    "Cameroon":               "CMR", "CMR":                   "CMR",
    "Ivory Coast":            "CIV", "CIV":                   "CIV",
    "Côte d'Ivoire":          "CIV",
    "Cote d'Ivoire":          "CIV",
    "Ghana":                  "GHA", "GHA":                   "GHA",
    "Algeria":                "ALG", "ALG":                   "ALG",
    "Tunisia":                "TUN", "TUN":                   "TUN",
    "Mali":                   "MLI", "MLI":                   "MLI",
    "Zimbabwe":               "ZIM", "ZIM":                   "ZIM",
    "Uganda":                 "UGA", "UGA":                   "UGA",
    "Tanzania":               "TAN", "TAN":                   "TAN",
    "Zambia":                 "ZAM", "ZAM":                   "ZAM",
    "Cape Verde Islands":     "CPV", "CPV":                   "CPV",
    "Cape Verde":             "CPV",

    # ── OFC / Intercontinental ──────────────────────────────────────────────────
    "New Zealand":            "NZL", "NZL":                   "NZL",

    # ── Kalshi/Polymarket-specific spellings seen in games/*.json ───────────────
    "Bosnia and Herzegovina": "BIH", "BIH":                   "BIH",
    "Bosnia":                 "BIH",
    "Bosnia-Herzegovina":     "BIH",
}


def team_code(raw: str) -> str:
    key = raw.strip()
    if key not in TEAM_ALIASES:
        raise KeyError(f"Unmapped team '{raw}' — add it to TEAM_ALIASES before ingesting")
    return TEAM_ALIASES[key]


def match_id(date_iso: str, home_raw: str, away_raw: str) -> str:
    # date_iso = 'YYYY-MM-DD' (the match's UTC date). home/away in fixture order.
    return f"{date_iso}_{team_code(home_raw)}_{team_code(away_raw)}"


def now_ts() -> str:
    # One clock for all sources: UTC, ISO-8601. Non-negotiable for lead-lag analysis.
    return datetime.now(timezone.utc).isoformat()
