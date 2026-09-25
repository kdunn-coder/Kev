"""Normalization of pods, stages and deal types.

CRM exports carry free-text-ish picklist values that drift over 24 months:
"New Business" becomes "New Logo", a stage gets renamed, a territory picklist
is restructured. Everything downstream depends on collapsing that drift, so it
all happens here and nowhere else.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

UNASSIGNED = "(unassigned)"

# ---------------------------------------------------------------------------
# key(): the canonical comparison form for any picklist value
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def key(value: object) -> str:
    """Fold a raw picklist value to a comparison key.

    Lowercases, strips accents, and collapses every run of punctuation and
    whitespace to a single space. So "UKI & Nordics", "uki/nordics" and
    "UKI  -  Nordics" all fold to "uki nordics".
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _NON_ALNUM.sub(" ", text.lower())
    return text.strip()


# ---------------------------------------------------------------------------
# Deal type
# ---------------------------------------------------------------------------

#: Canonical deal types, in the order they should appear in reports.
DEAL_TYPES = [
    "New Business",
    "Add-On / Expansion",
    "Renewal",
    "Migration",
    "Other",
]

_DEAL_TYPE_ALIASES: dict[str, str] = {}


def _register_deal_types() -> None:
    table = {
        "New Business": [
            "new business", "new", "new logo", "new logo business",
            "new customer", "new account", "net new", "net new business",
            "new business acquisition", "acquisition", "new name",
            "greenfield", "new deal", "new opportunity", "new logo acq",
            "new business new logo", "hunting",
        ],
        "Add-On / Expansion": [
            "add on", "addon", "add on business", "add on existing business",
            "expansion", "expand", "upsell", "up sell", "cross sell",
            "cross sell upsell", "growth", "existing business",
            "existing customer", "existing business add on", "uplift",
            "additional capacity", "capacity add", "farming", "land and expand",
        ],
        "Renewal": [
            "renewal", "renew", "renewals", "contract renewal",
            "renewal business", "existing business renewal", "re up",
            "recommit", "renewal extension", "extension", "true up",
            "auto renewal",
        ],
        "Migration": [
            "migration", "migrate", "competitive migration", "competitive displacement",
            "displacement", "rip and replace", "replacement", "lift and shift",
        ],
    }
    for canonical, raws in table.items():
        for raw in raws:
            _DEAL_TYPE_ALIASES[key(raw)] = canonical


_register_deal_types()


def normalize_deal_type(value: object) -> str:
    """Map a raw deal-type / opportunity-type value to a canonical bucket."""
    k = key(value)
    if not k:
        return UNASSIGNED
    if k in _DEAL_TYPE_ALIASES:
        return _DEAL_TYPE_ALIASES[k]

    # Substring fallback for compound values like
    # "Existing Business - Add-On (EMEA)".
    #
    # Order matters: check renewal before add-on, because "renewal add on"
    # should count as a renewal, and check add-on before new business, because
    # "add on to new business" is an add-on. Migration is checked first since
    # "competitive migration new logo" is really a migration play.
    for canonical, needles in (
        ("Migration", ("migration", "displacement", "rip and replace", "lift and shift")),
        ("Renewal", ("renewal", "renew", "true up", "re up")),
        ("Add-On / Expansion", ("add on", "addon", "expansion", "upsell", "up sell", "cross sell", "uplift")),
        ("New Business", ("new logo", "new business", "net new", "new customer", "new account", "acquisition")),
    ):
        if any(n in k for n in needles):
            return canonical
    return "Other"


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------
#
# Two things get reported about stage, and they answer different questions:
#
#   stage  -- the value as the CRM knows it, lightly title-cased. Kept so the
#             report can show the real funnel the team works.
#   outcome-- a coarse bucket (Open / Won / Lost / Rejected / Expired). This is
#             what makes rep-to-rep comparison honest: a rep holding 30 regs
#             that were all rejected as duplicates was not really allocated 30
#             deals.

#: Coarse outcome buckets, in report order.
OUTCOMES = ["Open", "Won", "Lost", "Rejected / Duplicate", "Expired", UNASSIGNED]

_OUTCOME_ALIASES: dict[str, str] = {}


def _register_outcomes() -> None:
    table = {
        "Won": [
            "closed won", "won", "win", "closed won booked", "booked",
            "closed  won", "6 closed won", "7 closed won", "100 closed won",
            "complete", "completed", "converted",
        ],
        "Lost": [
            "closed lost", "lost", "closed lost no decision", "no decision",
            "closed lost to competitor", "0 closed lost", "abandoned",
            "closed no decision", "dead", "disqualified", "closed lost churn",
        ],
        "Rejected / Duplicate": [
            "rejected", "declined", "denied", "duplicate", "dupe",
            "duplicate registration", "rejected duplicate", "not approved",
            "invalid", "rejected invalid", "withdrawn", "cancelled", "canceled",
        ],
        "Expired": [
            "expired", "lapsed", "timed out", "expired unworked", "aged out",
        ],
        "Open": [
            "submitted", "pending", "pending approval", "awaiting approval",
            "under review", "in review", "approved", "registered",
            "accepted", "active", "open", "qualifying", "qualification",
            "discovery", "prospecting", "engaged", "in progress",
            "needs analysis", "value proposition", "technical validation",
            "poc", "proof of concept", "trial", "evaluation", "proposal",
            "proposal price quote", "negotiation", "negotiation review",
            "contracting", "legal", "verbal commit", "commit",
            "closing", "1 qualify", "2 discover", "3 validate", "4 propose",
            "5 negotiate", "stage 1", "stage 2", "stage 3", "stage 4", "stage 5",
        ],
    }
    for canonical, raws in table.items():
        for raw in raws:
            _OUTCOME_ALIASES[key(raw)] = canonical


_register_outcomes()

#: Canonical funnel order for display. Anything unrecognised sorts after these.
STAGE_DISPLAY_ORDER = [
    "Submitted", "Pending Approval", "Approved", "Registered",
    "Qualification", "Discovery", "Technical Validation", "POC",
    "Proposal", "Negotiation", "Contracting",
    "Closed Won", "Closed Lost", "Rejected", "Duplicate", "Expired",
]

_STAGE_ORDER_KEYS = {key(s): i for i, s in enumerate(STAGE_DISPLAY_ORDER)}


def normalize_stage(value: object) -> str:
    """Clean a raw stage value for display, preserving the CRM's own funnel.

    Strips the numeric sort prefixes CRMs love ("3 - Technical Validation",
    "05 Negotiation") so the same stage from different picklist eras collapses
    into one row.
    """
    if value is None:
        return UNASSIGNED
    text = str(value).strip()
    if not text:
        return UNASSIGNED
    # Drop a leading sort prefix: "3 - ", "05.", "(4)", "Stage 3 -".
    text = re.sub(r"^\(?\s*(?:stage\s*)?\d{1,3}\s*\)?\s*[-.:)]?\s*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return UNASSIGNED
    # Title-case only all-lower or all-upper input; leave mixed case (which the
    # admin probably set deliberately, e.g. "POC") alone.
    if text.islower() or text.isupper():
        text = text.title()
    return text


def stage_outcome(value: object) -> str:
    """Bucket a raw stage into Open / Won / Lost / Rejected / Expired."""
    k = key(value)
    if not k:
        return UNASSIGNED
    if k in _OUTCOME_ALIASES:
        return _OUTCOME_ALIASES[k]
    for canonical, needles in (
        ("Won", ("closed won", "won", "booked")),
        ("Lost", ("closed lost", "lost", "no decision", "dead", "disqualified")),
        ("Rejected / Duplicate", ("reject", "declin", "duplicate", "dupe", "invalid", "withdraw", "cancel")),
        ("Expired", ("expire", "lapsed", "aged out")),
    ):
        if any(n in k for n in needles):
            return canonical
    # Anything left that the CRM still lists is a live working stage.
    return "Open"


def stage_sort_index(stage: str) -> tuple[int, str]:
    """Sort key placing known funnel stages in order, unknowns alphabetically after."""
    k = key(stage)
    if k in _STAGE_ORDER_KEYS:
        return (_STAGE_ORDER_KEYS[k], "")
    return (len(STAGE_DISPLAY_ORDER), stage)


# ---------------------------------------------------------------------------
# Pod resolution
# ---------------------------------------------------------------------------


@dataclass
class PodResolver:
    """Resolves a deal registration to one of the configured EMEA pods.

    Built from `config/pods.yml`. Resolution order is documented there and
    implemented in `resolve`; the order exists so that an authoritative
    rep->pod list always beats a stale territory picklist on the record.
    """

    pods: list[str]
    pod_alias_to_pod: dict[str, str] = field(default_factory=dict)
    country_to_pod: dict[str, str] = field(default_factory=dict)
    rep_overrides: dict[str, str] = field(default_factory=dict)
    isr_overrides: dict[str, str] = field(default_factory=dict)
    excluded_owner_patterns: list[str] = field(default_factory=list)
    #: Aliases ordered longest-first (by token count, then length), so the
    #: token rescue prefers the most specific match. Built in __post_init__.
    _aliases_by_token_length: list[tuple[str, str]] = field(default_factory=list)
    #: Country names long enough to be safe to match inside a territory path.
    _long_country_names: list[tuple[str, str]] = field(default_factory=list)
    #: Counts of how each record got its pod, for the data-quality report.
    provenance: dict[str, int] = field(default_factory=dict)
    #: Raw values that failed to resolve, so the user can fix the config.
    unresolved_pod_values: dict[str, int] = field(default_factory=dict)
    unresolved_countries: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._aliases_by_token_length = sorted(
            self.pod_alias_to_pod.items(),
            key=lambda kv: (-len(kv[0].split()), -len(kv[0])),
        )
        self._long_country_names = sorted(
            ((name, pod) for name, pod in self.country_to_pod.items() if len(name) >= 4),
            key=lambda kv: (-len(kv[0].split()), -len(kv[0])),
        )

    @classmethod
    def from_config(cls, cfg: dict) -> "PodResolver":
        pods = list(cfg.get("pods") or [])

        alias_to_pod: dict[str, str] = {}
        for pod in pods:
            # A pod always matches its own name.
            alias_to_pod[key(pod)] = pod
        for pod, aliases in (cfg.get("pod_aliases") or {}).items():
            alias_to_pod[key(pod)] = pod
            for alias in aliases or []:
                alias_to_pod[key(alias)] = pod

        excluded_countries = {key(c) for c in (cfg.get("country_to_pod_excluded") or [])}
        country_to_pod: dict[str, str] = {}
        for pod, countries in (cfg.get("country_to_pod") or {}).items():
            for country in countries or []:
                k = key(country)
                if k and k not in excluded_countries:
                    country_to_pod[k] = pod

        return cls(
            pods=pods,
            pod_alias_to_pod=alias_to_pod,
            country_to_pod=country_to_pod,
            rep_overrides={key(k): v for k, v in (cfg.get("rep_overrides") or {}).items()},
            isr_overrides={key(k): v for k, v in (cfg.get("isr_overrides") or {}).items()},
            excluded_owner_patterns=[key(p) for p in (cfg.get("excluded_owner_patterns") or []) if key(p)],
        )

    def is_excluded_owner(self, name: object) -> bool:
        """True for queues, integration users and other non-rep record holders."""
        k = key(name)
        if not k:
            return False
        return any(pat in k for pat in self.excluded_owner_patterns)

    def resolve(self, *, rep: object = None, isr: object = None,
                pod_value: object = None, country: object = None) -> str:
        """Return the canonical pod for one record, or UNASSIGNED."""
        rk = key(rep)
        if rk and rk in self.rep_overrides:
            self._bump("rep_override")
            return self.rep_overrides[rk]

        ik = key(isr)
        if ik and ik in self.isr_overrides:
            self._bump("isr_override")
            return self.isr_overrides[ik]

        pk = key(pod_value)
        if pk:
            if pk in self.pod_alias_to_pod:
                self._bump("pod_column")
                return self.pod_alias_to_pod[pk]
            # Token rescue for values like "EMEA - DACH - Enterprise" that
            # carry the pod inside a path.
            #
            # This matches on whole tokens, never raw substrings. A raw
            # substring test makes short aliases catastrophic: "ce" (Central
            # Europe) is inside "fran-ce", so a territory of "France" resolved
            # to DACH. Longest alias first, so "uki nordics" wins over the
            # bare "nordics" when a value contains both.
            v_tokens = pk.split()
            for alias, pod in self._aliases_by_token_length:
                a_tokens = alias.split()
                n = len(a_tokens)
                if n and any(v_tokens[i:i + n] == a_tokens
                             for i in range(len(v_tokens) - n + 1)):
                    self._bump("pod_column_token")
                    return pod
            # Territory fields are routinely filled with a country rather than
            # a pod ("France", "Germany"), so try the country map on this value
            # before giving up. Exact match only: a substring match here would
            # let a country name buried in a longer territory string outrank
            # the pod aliases already checked above.
            if pk in self.country_to_pod:
                self._bump("pod_column_as_country")
                return self.country_to_pod[pk]
            # A country embedded in a longer path ("EMEA - France - Enterprise").
            # Restricted to country names of 4+ characters: the 2-3 letter ISO
            # codes are matched exactly above, and would be reckless as tokens
            # inside a path, where "IT" means Information Technology far more
            # often than Italy, and "NO", "IS" and "AT" are ordinary words.
            for name, pod in self._long_country_names:
                n_tokens = name.split()
                n = len(n_tokens)
                if any(v_tokens[i:i + n] == n_tokens
                       for i in range(len(v_tokens) - n + 1)):
                    self._bump("pod_column_contains_country")
                    return pod
            self.unresolved_pod_values[str(pod_value).strip()] = (
                self.unresolved_pod_values.get(str(pod_value).strip(), 0) + 1
            )

        ck = key(country)
        if ck:
            if ck in self.country_to_pod:
                self._bump("country_fallback")
                return self.country_to_pod[ck]
            self.unresolved_countries[str(country).strip()] = (
                self.unresolved_countries.get(str(country).strip(), 0) + 1
            )

        self._bump("unassigned")
        return UNASSIGNED

    def _bump(self, reason: str) -> None:
        self.provenance[reason] = self.provenance.get(reason, 0) + 1


def dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
