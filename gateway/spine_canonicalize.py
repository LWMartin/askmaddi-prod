"""spine_canonicalize.py — clean a minted SKU's model string.

WHY THIS EXISTS (2026-09-03, aerial-vertical junk-leak class):

The eBay category tap (tools/ebay_category_tap.py) mints drone proposals whose
`model` field is the RAW listing title. Those titles carry two contaminants the
spine must not inherit:

  1. A LEADING DUPLICATE BRAND. eBay sellers repeat the brand in the title, so the
     tap emits vendor='Autel', model='Autel Evo II Pro ...'. slug_normalizer then
     slugifies f"{vendor} {model}" → 'autel-autel-evo-ii-pro-...' (the doubled
     slug), and the card label renders 'Autel Autel Evo II Pro'. The whole spine
     convention is a BRAND-LESS model (sony-a7-iv: vendor 'Sony', model 'A7 IV',
     label reconstructs 'Sony A7 IV'). A brand-leading model breaks slug, label,
     and identity-key all at once.

  2. TRAILING LISTING CRUFT. '- Never Opened', 'PRISTINE Condition', 'w/ RC',
     '| 640*512 Infrared Thermal', '& FLIR Lepton 3.5 Sensor', ', Waterproof ,
     Water Takeoff Landing', a trailing all-digit seller SKU. None of it is model
     identity; all of it poisons the card title and the relevance query.

canonicalize_model(vendor, model) returns the brand-less, cruft-less model. It is
CONSERVATIVE by design — it strips only unambiguous listing noise and never
touches version/variant tokens (V3, 640T, 4T, Mini 3) that ARE model identity.
Pure and total: same input → same output, never raises, empty-safe.

Applied at mint (resolve_sku.lookup_or_mint, forward) and by a one-time migration
over the already-contaminated entries (tools/canonicalize_spine_models.py).
"""
import re

# Corporate suffix words seen trailing a brand in eBay titles ('Autel Robotics',
# 'Skydio Inc'). Stripped ONLY when they immediately follow the leading brand, so
# a legitimate model word is never consumed.
_BRAND_SUFFIX = {"robotics", "inc", "camera", "cameras", "technology", "tech", "co"}

# Delimiters after which an eBay title turns into spec/feature/condition prose.
# We keep the head, drop the tail. None of these — '&' '|' '(' '*' ':' ',' '/'
# 'w/' 'with' — appears inside a real camera-gear model identity; a comma in a
# gear title is always a feature/condition separator ('X1, Waterproof, ...').
_CUT_DELIMS = re.compile(r"\s*(?:\||&|\*|\(|:|,|/|(?<=\s)w/|(?<=\s)with\s)", re.I)

# A leading 4-digit year eBay sellers prepend ('2026 Autel EVO ...'). Stripped
# before the brand match so the brand can still be found at the head.
_LEADING_YEAR = re.compile(r"^\s*(?:19|20)\d{2}\s+")

# Trailing condition / listing phrases (whole-tail, case-insensitive). Anchored to
# a delimiter or start so we never bite into a model word.
_CONDITION_TAIL = re.compile(
    r"\s*[-,]?\s*\b("
    r"never opened|brand new|like new|pristine(?:\s+condition)?|mint(?:\s+condition)?|"
    r"condition only|read description|read|for parts|as is|open box|"
    r"only|premium|standard|basic pack|bundle|combo|kit)\b.*$",
    re.I,
)

# A trailing pure seller-SKU token: 5+ digits, or a mixed alnum code with 4+ digits
# (SDRC2V1, 102000410). Version tokens (V3, 4T, 640T, X1) are shorter / not matched.
_TRAILING_SKU = re.compile(r"\s+(?=\S*\d)(?=\S*[A-Za-z0-9]{6,})[A-Za-z]*\d{4,}[A-Za-z0-9]*$")

_WS = re.compile(r"\s+")


def _norm_tokens(s):
    return _WS.sub(" ", s.strip()).lower().split()


def canonicalize_model(vendor, model):
    """Return `model` with a leading duplicate brand and trailing listing cruft
    removed. Brand-less by convention (the card label re-adds the vendor).

    Conservative: strips only unambiguous noise; leaves version/variant tokens.
    Never raises; returns '' for empty input. If stripping would empty the model
    (e.g. model == vendor exactly), returns the ORIGINAL model untouched — an
    empty model is worse than a doubled one, and that case is a human-review flag.
    """
    if not model or not model.strip():
        return ""
    original = _WS.sub(" ", model.strip())
    work = _LEADING_YEAR.sub("", original)

    # 1) Strip a leading brand (vendor tokens, then any brand-suffix words).
    if vendor and vendor.strip():
        vtok = _norm_tokens(vendor)
        mtok = work.split()
        i = 0
        # consume the vendor tokens if the model leads with them
        while i < len(mtok) and i < len(vtok) and mtok[i].lower() == vtok[i]:
            i += 1
        if i == len(vtok):  # full brand matched at the head
            # consume trailing corporate-suffix words too
            while i < len(mtok) and mtok[i].lower() in _BRAND_SUFFIX:
                i += 1
            work = " ".join(mtok[i:])

    # 2) Cut at the first spec/feature delimiter.
    work = _CUT_DELIMS.split(work, maxsplit=1)[0]

    # 3) Strip a trailing condition/listing phrase.
    work = _CONDITION_TAIL.sub("", work)

    # 4) Strip a trailing pure seller-SKU token.
    work = _TRAILING_SKU.sub("", work)

    # 5) Tidy: collapse ws, strip dangling separators/punctuation.
    work = _WS.sub(" ", work).strip(" -,&|*:/")

    if not work:
        return original  # never empty out an identity — flag by no-op
    return work


# Marketing / condition / spec-prose words that never belong in a model identity.
# Their SURVIVAL past canonicalize_model means the title was prose, not a model —
# a card built from it would carry a listing sentence as its name.
_PROSE_WORDS = frozenset("""
foldable flying self follow follow-me waterproof skiing action takeoff landing
obstacle avoidance transmission video cmos hdr 10bit ip67 digial droneer mini
range finding speaker spotlight tactical goggles rugged pack premium standard
""".split())

# Vendors that are listing noise, not brands (a leading dimension/spec token the
# tap mistook for a maker): pure digits, 5G/6K-style, sub-3-char, or a bare quote.
def _is_junk_vendor(vendor):
    v = (vendor or "").strip().lower()
    if not v or len(v) < 3:
        return True
    if re.fullmatch(r"\d+[a-z]?", v):        # '5', '6k', '5g'
        return True
    if re.fullmatch(r"[\d.\"']+", v):        # '2.5"'
        return True
    return False


def is_clean_model(vendor, model, *, max_tokens=6):
    """True if `model` (already canonicalized) is a build-worthy identity, not
    residual listing prose. Conservative gate for the requeue decision: a False
    here keeps the slug validly held rather than minting a prose-titled card."""
    if _is_junk_vendor(vendor):
        return False
    m = (model or "").strip()
    if not m:
        return False
    toks = m.lower().split()
    if len(toks) > max_tokens:
        return False
    if any(t.strip(",.-") in _PROSE_WORDS for t in toks):
        return False
    # Residual brand: the vendor (or a corporate parent like ZeroZero) still
    # sits inside the model → canonicalize couldn't fully de-double it. Holding
    # is safer than minting 'HoverAir ZeroZero Roboics HoverAir X1'.
    vtok = set(t for t in (vendor or "").lower().split())
    if vtok and vtok.issubset(set(toks)):
        return False
    return True
