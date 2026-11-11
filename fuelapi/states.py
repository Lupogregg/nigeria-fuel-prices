"""Canonical list of Nigeria's 36 states + FCT, with zones and name aliases.

NBS spells a few states inconsistently across workbooks ("Nassarawa", "Abuja"
for the FCT, "Akwa-Ibom"), so every label is normalised before lookup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class State:
    name: str
    slug: str
    zone: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


ZONE_SLUGS = {
    "North Central": "north-central",
    "North East": "north-east",
    "North West": "north-west",
    "South East": "south-east",
    "South South": "south-south",
    "South West": "south-west",
}

_RAW: list[tuple[str, str, tuple[str, ...]]] = [
    # (name, zone, extra aliases)
    ("Abia", "South East", ()),
    ("Adamawa", "North East", ()),
    ("Akwa Ibom", "South South", ()),
    ("Anambra", "South East", ()),
    ("Bauchi", "North East", ()),
    ("Bayelsa", "South South", ()),
    ("Benue", "North Central", ()),
    ("Borno", "North East", ()),
    ("Cross River", "South South", ("Cross Rivers",)),
    ("Delta", "South South", ()),
    ("Ebonyi", "South East", ()),
    ("Edo", "South South", ()),
    ("Ekiti", "South West", ()),
    ("Enugu", "South East", ()),
    ("FCT", "North Central", ("Abuja", "Abuja FCT", "FCT Abuja", "Federal Capital Territory")),
    ("Gombe", "North East", ()),
    ("Imo", "South East", ()),
    ("Jigawa", "North West", ()),
    ("Kaduna", "North West", ()),
    ("Kano", "North West", ()),
    ("Katsina", "North West", ()),
    ("Kebbi", "North West", ()),
    ("Kogi", "North Central", ()),
    ("Kwara", "North Central", ()),
    ("Lagos", "South West", ()),
    ("Nasarawa", "North Central", ("Nassarawa",)),
    ("Niger", "North Central", ()),
    ("Ogun", "South West", ()),
    ("Ondo", "South West", ()),
    ("Osun", "South West", ()),
    ("Oyo", "South West", ()),
    ("Plateau", "North Central", ()),
    ("Rivers", "South South", ("River",)),
    ("Sokoto", "North West", ()),
    ("Taraba", "North East", ()),
    ("Yobe", "North East", ()),
    ("Zamfara", "North West", ()),
]


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def normalize(label: str) -> str:
    """Collapse a label to lowercase letters only, dropping a trailing 'state'."""
    s = re.sub(r"[^a-z]", "", str(label).lower())
    if s.endswith("state") and len(s) > 5:
        s = s[:-5]
    return s


STATES: list[State] = [
    State(name=n, slug=_slugify(n), zone=z, aliases=a) for n, z, a in _RAW
]
BY_SLUG: dict[str, State] = {s.slug: s for s in STATES}

_LOOKUP: dict[str, State] = {}
for _s in STATES:
    for _label in (_s.name, *_s.aliases):
        _LOOKUP[normalize(_label)] = _s

assert len(STATES) == 37


def lookup(label: object) -> State | None:
    """Return the State for an NBS row label, or None if it isn't a state."""
    if not isinstance(label, str):
        return None
    return _LOOKUP.get(normalize(label))
