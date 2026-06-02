"""AprilTag ID -> traffic-sign meaning.

This is the dictionary the rest of the project agrees on. Person A keeps it
correct; Person B's state machine only ever sees the `sign_type` strings and
the `turns` lists defined here, never raw tag numbers.

Two layers:
  * SIGN_INFO        - the *vocabulary*: every sign type the bot understands,
                       its category, and (for intersection signs) which turns
                       it permits. This is stable Duckietown knowledge.
  * DEFAULT_TAG_IDS  - which printed AprilTag carries which sign. Tag IDs are
                       town-specific, so this is only a sensible default. The
                       real mapping for your map can be pasted into
                       config/traffic_signs_config.yaml under `signs:` and it
                       overrides this table at load time (see build_lookup()).
"""

from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Sign vocabulary
# ---------------------------------------------------------------------------
# Turn codes used everywhere downstream:
LEFT     = "left"
STRAIGHT = "straight"
RIGHT    = "right"

# Categories let Person B branch quickly (e.g. "is this an intersection sign?").
CAT_REGULATORY   = "regulatory"     # stop, yield
CAT_INTERSECTION = "intersection"   # tell you which turns exist
CAT_INFO         = "informational"  # parking, pedestrian, etc.

# sign_type -> metadata.
#   category : one of the CAT_* constants
#   turns    : allowed turn directions at the intersection ahead (intersection
#              signs only); None for non-intersection signs
#   desc     : human-readable label for overlays / logs
SIGN_INFO: Dict[str, dict] = {
    # --- Regulatory ---------------------------------------------------------
    "stop":              {"category": CAT_REGULATORY,   "turns": None,                          "desc": "Stop"},
    "yield":             {"category": CAT_REGULATORY,   "turns": None,                          "desc": "Yield"},

    # --- Intersection (turn-option) signs ----------------------------------
    # A "T" where the through-road runs left<->right and you arrive from the
    # stem: you may go left or right, not straight.
    "T-intersection":    {"category": CAT_INTERSECTION, "turns": [LEFT, RIGHT],                 "desc": "T-intersection (L/R)"},
    # Through-road continues straight and also goes left.
    "left-T-intersect":  {"category": CAT_INTERSECTION, "turns": [LEFT, STRAIGHT],              "desc": "T-intersection (L/S)"},
    # Through-road continues straight and also goes right.
    "right-T-intersect": {"category": CAT_INTERSECTION, "turns": [STRAIGHT, RIGHT],             "desc": "T-intersection (S/R)"},
    # Full 4-way: any direction allowed.
    "4-way-intersect":   {"category": CAT_INTERSECTION, "turns": [LEFT, STRAIGHT, RIGHT],       "desc": "4-way intersection"},

    # --- Informational ------------------------------------------------------
    "t-light-ahead":     {"category": CAT_INFO,         "turns": None,                          "desc": "Traffic light ahead"},
    "pedestrian":        {"category": CAT_INFO,         "turns": None,                          "desc": "Pedestrian crossing"},
    "duck-crossing":     {"category": CAT_INFO,         "turns": None,                          "desc": "Duck crossing"},
    "parking":           {"category": CAT_INFO,         "turns": None,                          "desc": "Parking"},
    "oneway-left":       {"category": CAT_INFO,         "turns": None,                          "desc": "One-way (left)"},
    "oneway-right":      {"category": CAT_INFO,         "turns": None,                          "desc": "One-way (right)"},
}

# ---------------------------------------------------------------------------
# Default tag-ID -> sign_type assignment  (OVERRIDE PER MAP IN THE CONFIG!)
# ---------------------------------------------------------------------------
# IMPORTANT: these IDs are an example layout, not gospel. Every Duckietown lays
# out its tags differently. Read the tag numbers printed under your signs (or
# your town's apriltagsDB.yaml) and put the real mapping in
# config/traffic_signs_config.yaml, e.g.:
#
#   signs:
#     25: stop
#     9:  T-intersection
#     10: left-T-intersect
#
# Anything here that the config does not mention stays as-is.
DEFAULT_TAG_IDS: Dict[int, str] = {
    # regulatory
    25: "stop",
    26: "stop",
    32: "yield",
    33: "yield",
    # intersection turn-option signs
    9:  "T-intersection",
    10: "left-T-intersect",
    11: "right-T-intersect",
    12: "4-way-intersect",
    # informational
    13: "t-light-ahead",
    14: "pedestrian",
    15: "duck-crossing",
    16: "parking",
}


class SignLookup:
    """Resolves a raw AprilTag ID to its sign meaning."""

    def __init__(self, tag_ids: Optional[Dict[int, str]] = None):
        self._tag_ids: Dict[int, str] = dict(DEFAULT_TAG_IDS)
        if tag_ids:
            # Validate that every override names a known sign type; a typo here
            # would silently produce signs Person B can't handle.
            for tid, stype in tag_ids.items():
                if stype not in SIGN_INFO:
                    raise ValueError(
                        f"config signs: tag {tid} -> unknown sign type {stype!r}. "
                        f"Known types: {sorted(SIGN_INFO)}"
                    )
            self._tag_ids.update({int(k): v for k, v in tag_ids.items()})

    def sign_type(self, tag_id: int) -> Optional[str]:
        """Sign-type string for a tag, or None if the tag is not a known sign."""
        return self._tag_ids.get(int(tag_id))

    def info(self, tag_id: int) -> Optional[dict]:
        """Full metadata dict (category, turns, desc) for a tag, or None."""
        stype = self.sign_type(tag_id)
        return SIGN_INFO.get(stype) if stype else None

    def turns(self, tag_id: int) -> Optional[List[str]]:
        """Allowed turns for an intersection sign, else None."""
        meta = self.info(tag_id)
        return meta["turns"] if meta else None

    def is_known(self, tag_id: int) -> bool:
        return int(tag_id) in self._tag_ids


def build_lookup(config: Optional[dict]) -> SignLookup:
    """Build a SignLookup, applying any `signs:` overrides from the config."""
    overrides = (config or {}).get("signs") if config else None
    return SignLookup(overrides)
