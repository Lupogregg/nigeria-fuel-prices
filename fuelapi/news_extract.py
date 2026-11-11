"""Extract fuel-price claims from news headlines.

Rule-based on purpose: it costs nothing to run, behaves the same every time,
and every decision can be traced to a rule and a test. It is tuned for
precision over recall - a headline it is unsure about yields nothing.

Only headlines are read. A headline such as

    "NNPC Cuts Petrol Price to N1,325 in Abuja, N1,355 in Lagos"

yields two claims (NNPC, petrol, pump price, falling; one per city). Amounts
that are not a current price are recognised and left out: the old price in
"No more N1,420", a change in "rises by N75", money in "N5.85 trillion", and
predictions such as "petrol may hit N2,000".
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from . import states

# ---------------------------------------------------------------- places

_CITIES = {
    "Ikeja": "lagos", "Lekki": "lagos", "Abuja": "fct", "Port Harcourt": "rivers",
    "Ibadan": "oyo", "Benin City": "edo", "Benin": "edo", "Zaria": "kaduna",
    "Aba": "abia", "Umuahia": "abia", "Onitsha": "anambra", "Awka": "anambra",
    "Warri": "delta", "Asaba": "delta", "Calabar": "cross-river", "Uyo": "akwa-ibom",
    "Maiduguri": "borno", "Jos": "plateau", "Ilorin": "kwara", "Abeokuta": "ogun",
    "Akure": "ondo", "Osogbo": "osun", "Owerri": "imo", "Yenagoa": "bayelsa",
    "Makurdi": "benue", "Lokoja": "kogi", "Minna": "niger", "Lafia": "nasarawa",
    "Gusau": "zamfara", "Birnin Kebbi": "kebbi", "Dutse": "jigawa",
    "Damaturu": "yobe", "Yola": "adamawa", "Jalingo": "taraba",
    "Abakaliki": "ebonyi", "Ado-Ekiti": "ekiti", "Ado Ekiti": "ekiti",
}
_PLACES: dict[str, str] = {**_CITIES}
for _s in states.STATES:
    _PLACES[_s.name] = _s.slug
    for _alias in _s.aliases:
        if _alias not in ("River",):
            _PLACES.setdefault(_alias, _s.slug)
_PLACES["Akwa-Ibom"] = "akwa-ibom"

# Case-sensitive: "Delta", "Niger", "Plateau", "Rivers" are ordinary words in
# lower case. A handful of phrases are never the state.
_PLACE_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted(map(re.escape, _PLACES), key=len, reverse=True)) + r")(?![A-Za-z])"
)
_NOT_A_STATE = re.compile(
    r"Niger[ -]Delta|Niger Republic|Republic of Niger|River Niger|Benin Republic|Republic of Benin"
    r"|Delta Air|Plateau United|Rivers United|Kano Pillars"
)

# -------------------------------------------------------------- marketers

_MARKETERS: list[tuple[str, str, str]] = [
    # (id, label, case-sensitive pattern)
    ("nnpc", "NNPC", r"NNPCL?|NNPC Retail"),
    ("dangote", "Dangote Refinery", r"Dangote"),
    ("mrs", "MRS", r"MRS"),
    ("totalenergies", "TotalEnergies", r"TotalEnergies|Total Energies"),
    ("mobil", "Mobil (11 Plc)", r"Mobil|11 ?[Pp][Ll][Cc]"),
    ("ardova", "Ardova (AP)", r"Ardova|AP"),
    ("conoil", "Conoil", r"Conoil"),
    ("oando", "Oando", r"Oando"),
    ("nipco", "NIPCO", r"NIPCO|Nipco"),
    ("rainoil", "Rainoil", r"Rainoil"),
    ("aa-rano", "A.A. Rano", r"A\.?A\.? ?Rano"),
    ("bovas", "Bovas", r"BOVAS|Bovas"),
    ("matrix", "Matrix", r"Matrix Energy|Matrix"),
    ("northwest", "Northwest", r"Northwest Petroleum|Northwest"),
    ("heyden", "Heyden", r"Heyden"),
    ("ipman", "Independent marketers (IPMAN)", r"IPMAN|[Ii]ndependent [Mm]arketers"),
    ("moman", "Major marketers (MOMAN)", r"MOMAN|[Mm]ajor [Mm]arketers"),
]
_MARKETER_RE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(f"(?P<m{i}>{p})" for i, (_, _, p) in enumerate(_MARKETERS))
    + r")(?![A-Za-z])"
)
MARKETER_LABELS = {mid: label for mid, label, _ in _MARKETERS}

# ------------------------------------------------------------------ fuels

_PMS_RE = re.compile(r"petrol|\bpms\b|premium motor spirit|fuel pump|fuel prices?|pump prices?", re.I)
_AGO_RE = re.compile(r"diesel|\bago\b|automotive gas oil", re.I)
_OTHER_FUEL_RE = re.compile(r"cooking gas|\blpg\b|\bcng\b|kerosene|\bdpk\b|jet a-?1|aviation fuel|crude", re.I)

# ----------------------------------------------------------------- prices

_PRICE_RE = re.compile(
    r"(?:₦|(?<![A-Za-z])N(?=\d)|(?<![A-Za-z])NGN ?)"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"(?P<magnitude>\s?(?:trn|tn|trillion|bn|billion|mn|million|[mbk])\b)?",
)
_PER_LITRE_AFTER = re.compile(r"^\s*(?:/|per |a |-per-)\s*(?:litre|liter|ltr|l)\b", re.I)
_OTHER_UNIT_AFTER = re.compile(
    r"^\s*(?:/|per |a |-per-)\s*(?:kg|kilo\w*|scm|cylinder|barrel|tonne|ton|bag|trip|month|day|year|share)\b", re.I)
_DELTA_AFTER = re.compile(
    r"^[\s/a-z-]{0,25}\b(?:discount|increase|increment|hike|reduction|cut|drop|rise|levy|tax|subsidy|rebate"
    r"|difference|gap|margin|fine|debt|loan|profit|loss)\b", re.I)
_POLICY_AFTER = re.compile(
    r"^[\s/a-z'\u2019-]{0,30}\b(?:cap|peg|ceiling|benchmark|target|announcement|promise|pledge|directive"
    r"|template|control)\b", re.I)
_OLD_BEFORE = re.compile(
    r"(?:no more|no longer|from|down from|up from|was|previous(?:ly)?(?: price)?(?: of)?|old price(?: of)?"
    r"|instead of|against|initial(?:ly)?|former(?:ly)?|earlier)\s*$", re.I)
_DELTA_BEFORE = re.compile(
    r"(?:\bby|(?:increase|increment|hike|rise|reduction|cut|drop|discount|slash|decrease|difference|gap|margin)s?"
    r"(?: of)?|adds?|saves?|extra|additional|sheds?|gains?|loses?|another)\s*$", re.I)
_SPECULATIVE = re.compile(
    r"\b(?:may|might|could|would|will|shall|to hit|set to|about to|expected|expects?|predicts?|forecasts?"
    r"|projects?|projected|warns?|fears?|likely|should|unless|possible|plans? to|proposes?|proposed"
    r"|demands?|urges?|wants?|seeks?|targets?|insists?|if|cap|caps|capped|peg|pegs|pegged|fix|fixes"
    r"|fixed)\b", re.I)
_LOCATION_AFTER = re.compile(r"^(?:\s*(?:/|per |a )\s*(?:litre|liter|ltr|l)\b)?\s*,?\s*(?:in|at|across|for)\s+", re.I)

_EX_DEPOT = re.compile(r"ex-?depot|depot|gantry|loading price|coastal|wholesale", re.I)
_COST_NOT_PRICE = re.compile(r"landing cost|landed cost|import cost|import parity|cost of import", re.I)
_PUMP_STRICT = re.compile(r"pump|retail|filling stations?|\bstations?\b|outlets?|motorists", re.I)
_PUMP = re.compile(_PUMP_STRICT.pattern + r"|\bsell", re.I)
# "below Dangote's price", "higher than NNPC": the marketer is a yardstick, not the seller
_COMPARED_TO = re.compile(
    r"\b(?:below|above|than|under|undercut\w*|beats?|versus|vs\.?|against)\s+(?:\w+\s+){0,2}$", re.I)
_OTHERS = re.compile(r"\b(?:other|others|rivals?|competitors?)\b", re.I)

_UP = re.compile(r"\b(?:rais\w*|hik\w*|increas\w*|jack\w*|climb\w*|ris(?:e|es|ing)|rose|jump\w*|soar\w*|surg\w*"
                 r"|hits?|up(?:ward)?)\b", re.I)
_DOWN = re.compile(r"\b(?:cuts?|slash\w*|reduc\w*|drops?|dropped|crash\w*|lower\w*|falls?|fell|dips?|dipped"
                   r"|declin\w*|down(?:ward)?|cheaper|no more)\b", re.I)

# Clause boundaries. A price takes its marketer, place and direction from its
# own clause first, so "Dangote cuts price to N1,215 as NNPC, MRS prepare new
# rates" credits Dangote, not NNPC.
_CLAUSE_SPLIT = re.compile(r"\s+(?:as|after|following|while|amid|despite|but|though)\s+|\s*[;:|]\s+|\s+[—–-]\s+", re.I)

MIN_PRICE = 100.0  # smaller naira amounts in a fuel headline are changes, not prices


@dataclass
class Claim:
    fuel: str  # pms | ago
    price: float
    price_type: str  # pump | ex_depot
    price_type_inferred: bool
    per_litre_stated: bool
    state: str | None  # state slug
    place: str | None  # the word the headline used, e.g. "Abuja"
    marketer: str | None
    direction: str | None  # up | down
    old_price: float | None
    confidence: str  # high | medium | low; only "high" claims are published


@dataclass
class Extraction:
    relevant: bool  # the headline mentions petrol or diesel and a naira amount
    claims: list[Claim] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)  # amounts seen but not used, with the reason

    def to_dict(self) -> dict:
        return {"relevant": self.relevant, "claims": [asdict(c) for c in self.claims],
                "ignored": self.ignored}


def _clauses(title: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _CLAUSE_SPLIT.finditer(title):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(title)))
    return spans


def _places_in(text: str, offset: int = 0) -> list[tuple[int, str, str]]:
    blocked = [(m.start(), m.end()) for m in _NOT_A_STATE.finditer(text)]
    out = []
    for m in _PLACE_RE.finditer(text):
        if any(a <= m.start() < b for a, b in blocked):
            continue
        out.append((offset + m.start(), m.group(1), _PLACES[m.group(1)]))
    return out


def _marketers_in(text: str) -> list[str]:
    found = []
    for m in _MARKETER_RE.finditer(text):
        if _COMPARED_TO.search(text[:m.start()]):
            continue
        idx = next(i for i in range(len(_MARKETERS)) if m.group(f"m{i}"))
        mid = _MARKETERS[idx][0]
        if mid not in found:
            found.append(mid)
    return found


def _direction(text: str) -> str | None:
    up, down = _UP.search(text), _DOWN.search(text)
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    if up and down:
        return "up" if up.start() < down.start() else "down"
    return None


def _fuel_for(title: str, pos: int, end: int) -> str | None:
    pms = [m.start() for m in _PMS_RE.finditer(title)]
    ago = [m.start() for m in _AGO_RE.finditer(title)]
    if not pms and not ago:
        return None
    nxt = title[end:end + 22]
    first_pms, first_ago = _PMS_RE.search(nxt), _AGO_RE.search(nxt)
    if first_ago and (not first_pms or first_ago.start() < first_pms.start()) and first_ago.start() <= 12:
        return "ago"
    if first_pms and (not first_ago or first_pms.start() < first_ago.start()) and first_pms.start() <= 12:
        return "pms"
    if pms and not ago:
        return "pms"
    if ago and not pms:
        return "ago"
    # Both are mentioned: the nearest fuel word before the price decides.
    before = [(p, "pms") for p in pms if p < pos] + [(p, "ago") for p in ago if p < pos]
    return max(before)[1] if before else "pms"


def _is_price(m: re.Match, after: str, before: str) -> bool:
    """True if a naira amount could be a per-litre price at all (not a sum or a change)."""
    return not (m.group("magnitude") or float(m.group("num").replace(",", "")) < MIN_PRICE
                or _OTHER_UNIT_AFTER.search(after) or _DELTA_BEFORE.search(before)
                or _DELTA_AFTER.search(after))


def _own_price(title: str, clauses: list[tuple[int, int]], marketer: str, skip: tuple[int, int]) -> bool:
    """Does the clause naming `marketer` carry a price of its own?"""
    for a, b in clauses:
        if (a, b) == skip or marketer not in _marketers_in(title[a:b]):
            continue
        for m in _PRICE_RE.finditer(title[a:b]):
            if _is_price(m, title[a + m.end():b], title[a:a + m.start()]):
                return True
    return False


def extract(title: str) -> Extraction:
    title = re.sub(r"\s+", " ", title.replace("‎", "").replace(" ", " ")).strip()
    prices = list(_PRICE_RE.finditer(title))
    has_fuel = bool(_PMS_RE.search(title) or _AGO_RE.search(title))
    result = Extraction(relevant=bool(prices) and has_fuel)
    if not result.relevant:
        return result

    only_other_fuel = False
    other = _OTHER_FUEL_RE.search(title)
    clauses = _clauses(title)
    title_places = _places_in(title)
    title_marketers = _marketers_in(title)

    parsed = []
    for m in prices:
        value = float(m.group("num").replace(",", ""))
        start, end = m.start(), m.end()
        c_start, c_end = next((a, b) for a, b in clauses if a <= start < b or (a <= start and b == len(title)))
        clause = title[c_start:c_end]
        before = title[c_start:start]
        after = title[end:c_end]

        def ignore(reason: str) -> None:
            result.ignored.append({"amount": m.group(0).strip(), "reason": reason})

        if m.group("magnitude"):
            ignore("a sum of money, not a price")
            continue
        if _OTHER_UNIT_AFTER.search(after):
            ignore("not priced per litre")
            continue
        if value < MIN_PRICE:
            ignore("too small to be a price per litre")
            continue
        if _DELTA_BEFORE.search(before) or _DELTA_AFTER.search(after):
            ignore("the size of a change, not a price")
            continue
        if _POLICY_AFTER.search(after):
            ignore("an announced or capped price, not an observed one")
            continue
        fuel = _fuel_for(title, start, end)
        if fuel is None or (other and not (_PMS_RE.search(clause) or _AGO_RE.search(clause))
                            and abs(other.start() - start) < 40):
            ignore("not clearly a petrol or diesel price")
            only_other_fuel = True
            continue
        if _COST_NOT_PRICE.search(clause):
            ignore("a landing or import cost, not a selling price")
            continue
        if _OLD_BEFORE.search(before):
            parsed.append({"role": "old", "value": value, "clause": (c_start, c_end)})
            ignore("a previous price")
            continue
        if _SPECULATIVE.search(before):
            ignore("a prediction or demand, not a reported price")
            continue

        per_litre = bool(_PER_LITRE_AFTER.search(after))
        clause_marketers = _marketers_in(clause)
        if len(clause_marketers) == 1:
            marketer = clause_marketers[0]
        elif (not clause_marketers and len(title_marketers) == 1 and not _OTHERS.search(clause)
              and not _own_price(title, clauses, title_marketers[0], (c_start, c_end))):
            # The only marketer named sits in another clause that has no price
            # of its own ("N1,405: NNPC increases petrol price by N30").
            marketer = title_marketers[0]
        else:
            marketer = None

        if _EX_DEPOT.search(clause) and not _PUMP.search(before):
            price_type, inferred = "ex_depot", False
        elif (_PUMP_STRICT if marketer == "dangote" else _PUMP).search(clause):
            price_type, inferred = "pump", False
        elif marketer == "dangote":
            # The refinery sells at the gantry; its headline prices are
            # wholesale unless the headline says otherwise.
            price_type, inferred = "ex_depot", True
        else:
            price_type, inferred = "pump", True

        place = state = None
        loc_after = _LOCATION_AFTER.search(after)
        if loc_after:
            here = _places_in(after[loc_after.end():loc_after.end() + 20])
            if here and here[0][0] == 0:
                place, state = here[0][1], here[0][2]
        if state is None:
            in_clause = {s: p for _, p, s in _places_in(clause)}
            in_title = {s: p for _, p, s in title_places}
            if len(in_clause) == 1:
                state, place = next(iter(in_clause.items()))
            elif not in_clause and len(in_title) == 1:
                state, place = next(iter(in_title.items()))

        parsed.append({
            "role": "new", "value": value, "clause": (c_start, c_end), "fuel": fuel,
            "price_type": price_type, "inferred": inferred, "per_litre": per_litre,
            "state": state, "place": place, "marketer": marketer,
            "direction": _direction(clause) or _direction(title),
        })

    for p in parsed:
        if p["role"] != "new":
            continue
        olds = [o["value"] for o in parsed if o["role"] == "old" and o["clause"] == p["clause"]]
        typed = p["per_litre"] or not p["inferred"]
        if p["marketer"]:
            # A named seller fixes what kind of price it is: retailers quote
            # pump prices, the refinery quotes gantry prices.
            confidence = "high"
        elif typed and (p["state"] or p["price_type"] == "ex_depot"):
            confidence = "high"
        elif typed or p["state"]:
            confidence = "medium"
        else:
            confidence = "low"
        result.claims.append(Claim(
            fuel=p["fuel"], price=round(p["value"], 2), price_type=p["price_type"],
            price_type_inferred=p["inferred"], per_litre_stated=p["per_litre"],
            state=p["state"], place=p["place"], marketer=p["marketer"],
            direction=p["direction"], old_price=olds[0] if len(olds) == 1 else None,
            confidence=confidence,
        ))
    if only_other_fuel and not result.claims:
        result.relevant = False
    return result
