"""Conservative English suffix stripping for evidence-coverage matching.

The coverage gate asks "does the evidence contain this question term?". Exact
matching rejected correct answers whenever the document used a morphological
variant of the term, so terms are reduced to a shared stem first.

This is deliberately *not* a full Porter/Snowball stemmer: those are tuned for
indexing recall, and over-stemming here would make unrelated words collide
("renew" from "renewable" and "renewal" is correct; "university" must not become
"univers"). The rules below only strip inflectional endings, which is the class
of variation that actually appears between a question and its source text.
"""

import re

_PLURAL_RULES = (
    ("sses", "ss"),
    ("ies", "y"),
    ("ss", "ss"),
    ("s", ""),
)
_DOUBLED_CONSONANT = re.compile(r"([bdfgmnprt])\1$")
_PLURAL_EXCEPTIONS = frozenset({
    # -s is part of the word, not a plural marker.
    "analysis", "basis", "business", "bias", "status", "census", "focus", "genus",
    "process", "series", "species", "news", "always", "this", "his", "its",
    "as", "is", "was", "has", "gas", "plus", "less", "class", "pass", "axis",
    "thesis", "crisis", "diagnosis", "emphasis", "synthesis", "prognosis",
})
# Ordered longest-suffix-first: "izations" must be tested before "ations".
_VERB_RULES = (
    ("izations", "ization"),
    ("isations", "isation"),
    ("ization", "ization"),
    ("isation", "isation"),
    ("iveness", "iveness"),
    ("fulness", "fulness"),
    ("ousness", "ousness"),
    ("ations", "ation"),
    ("ing", ""),
    ("ings", ""),
    ("ied", "y"),
    ("ies", "y"),
    ("ical", "ic"),
    ("ically", "ic"),
    ("ally", ""),
    ("ely", ""),
    ("ed", ""),
    ("ely", ""),
    ("es", ""),
)
# Words whose stem is not recoverable by suffix stripping, and whose short forms
# collide with unrelated words. Mapped explicitly rather than left to the rules.
IRREGULAR = {
    "children": "child", "men": "man", "women": "woman", "people": "person",
    "feet": "foot", "teeth": "tooth", "geese": "goose", "mice": "mouse",
    "licence": "license", "practise": "practice", "organisation": "organization",
    "organisations": "organizations", "recognise": "recognize", "labour": "labor",
    "neighbour": "neighbor", "neighbours": "neighbors", "behaviour": "behavior",
    "favour": "favor", "centre": "center", "metre": "meter", "litre": "liter",
    "fibre": "fiber", "programme": "program", "utilise": "utilize",
    "utilisation": "utilization", "authorised": "authorized", "authorisation": "authorization",
    "modelling": "modeling", "cancelled": "canceled", "labelling": "labeling",
    "fulfil": "fulfill", "enrol": "enroll", "instalment": "installment",
    "travelling": "traveling", "cancelling": "canceling", "totalling": "totaling",
    "signalling": "signaling", "levelled": "leveled", "fuelled": "fueled",
    # Latin/Greek plurals that suffix rules would mangle into a different word.
    "analyses": "analysis", "crises": "crisis", "theses": "thesis",
    "diagnoses": "diagnosis", "hypotheses": "hypothesis", "parentheses": "parenthesis",
    "oases": "oasis", "axes": "axis", "bases": "basis", "data": "datum",
    "media": "medium", "criteria": "criterion", "phenomena": "phenomenon",
    "indices": "index", "matrices": "matrix", "vertices": "vertex",
    # -e / -ed / -ation forms of one word, so "authorised" and "authorization"
    # reduce to the same stem.
    "authorize": "authoriz", "authorizing": "authoriz", "authorized": "authoriz",
    "authorization": "authoriz", "authorizations": "authoriz",
    "authorize": "authoriz", "authorise": "authoriz", "authorised": "authoriz",
    "authorising": "authoriz", "authorisation": "authoriz", "authorisations": "authoriz",
    "recognize": "recogniz", "recognized": "recogniz", "recognizes": "recogniz",
    "recognizing": "recogniz", "recognition": "recogniz",
    "recognise": "recogniz", "recognised": "recogniz", "recognises": "recogniz",
    "utilize": "utiliz", "utilized": "utiliz", "utilizes": "utiliz",
    "utilizing": "utiliz", "utilization": "utiliz",
    "utilise": "utiliz", "utilised": "utiliz", "utilises": "utiliz",
    "utilising": "utiliz", "utilisation": "utiliz",
}
# A stem shorter than this is not informative enough to be worth matching on.
MIN_STEM_LENGTH = 3


def stem(word: str) -> str:
    """Reduce `word` to an inflection-insensitive form, lower-cased."""
    word = word.lower().strip(".'\u2019")
    if not word:
        return ""
    if word in IRREGULAR:
        return IRREGULAR[word]
    # Numbers and clause identifiers ("5.2", "2030") are matched exactly.
    if any(ch.isdigit() for ch in word):
        return word
    if word in _PLURAL_EXCEPTIONS:
        return word
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for suffix, replacement in _PLURAL_RULES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            candidate = word[: len(word) - len(suffix)] + replacement
            if suffix == "s" and _DOUBLED_CONSONANT.search(candidate):
                candidate = candidate[:-1]  # "planned" -> "plan", not "plann"
            return candidate
    if len(word) > 5:
        for suffix, replacement in _VERB_RULES:
            if word.endswith(suffix) and len(word) - len(suffix) >= MIN_STEM_LENGTH:
                stem = word[: len(word) - len(suffix)] + replacement
                return stem[:-1] if _DOUBLED_CONSONANT.search(stem) else stem
    return word


def stem_all(words) -> set[str]:
    return {stem(w) for w in words if w}
