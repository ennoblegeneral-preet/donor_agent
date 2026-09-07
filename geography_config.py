"""Ennoble's own priority operating/target geographies.

Used to override the generic LLM "how many states does this company operate
in" geography judgment: a company found operating in ANY of these specific
places is treated as a strong (High) geography match, because this list is
the organisation's actual priority regions - not a generic notion of
geographic spread. When nothing here matches, scoring falls back to the
LLM's own judgment instead of forcing a Low rating, since absence from this
list is not proof the company has no relevant footprint elsewhere.
"""

PRIORITY_GEOGRAPHIES = [
    "Uttar Pradesh",
    "Rajasthan",
    "Bihar",
    "Gujarat",
    "Delhi NCR",
    "Delhi",          # alias for Delhi NCR - research often just says "Delhi"/"New Delhi"
    "New Delhi",       # alias for Delhi NCR
    "Mumbai",
    "Maharashtra",
    "Palghar",
    "Andaman",
    "Ladakh",
    "Vapi",
    "Anjar",
    "Taloja",
    "Pune Shirur",
    "Pimpri-Chinchwad Municipal Corporation",
    "Pune Municipal Corporation",
    "Kerala",
    "Ernakulam",
]


def find_priority_geography_matches(text: str) -> list:
    """Case-insensitive substring match of `text` against PRIORITY_GEOGRAPHIES.
    Returns the matched entries (original casing) for use in scoring
    reasons/audit trails. Empty list means no priority-geography evidence was
    found in the given text - not that the company has no footprint at all."""
    if not text:
        return []
    text_lower = text.lower()
    return [place for place in PRIORITY_GEOGRAPHIES if place.lower() in text_lower]
