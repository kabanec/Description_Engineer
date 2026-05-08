"""
Description Quality Analyzer (DQA) — evaluates product descriptions
for customs classification reliability.

Core algorithm: run multiple classifiers on the same description and
measure agreement.  High classifier consensus = good description.
Low consensus = ambiguous or insufficient input.

Six signals, each scored 0-100, combined with configurable weights:

  1. Classifier Consensus (35%) — HS-2/4/6 agreement across classifiers
  2. Description Completeness (20%) — presence of material, use, form, specs
  3. Specificity (15%) — word count, technical terms, vague-term penalty
  4. Confidence Spread (10%) — range between classifier confidence scores
  5. Historical Consistency (10%) — matches ProductCatalog history
  6. Tariff Parroting Check (10%) — flags descriptions that copy HTS schedule
     language verbatim (prohibited practice, CBP penalty risk)

Standalone endpoint: POST /api/v1/description-quality
Scoring rule:       description_quality.check()
"""

import logging
import re
import hashlib
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Weights (must sum to 1.0) ─────────────────────────────────────────
# Rebalanced Apr 2026: reduced consensus weight (single-classifier penalty
# was too dominant) and boosted completeness/specificity which better reflect
# real description quality for invoices that pass through with one classifier.
W_CONSENSUS = 0.20
W_COMPLETENESS = 0.30
W_SPECIFICITY = 0.20
W_CONFIDENCE_SPREAD = 0.05
W_HISTORICAL = 0.10
W_TARIFF_PARROT = 0.15

# ── Grading thresholds ────────────────────────────────────────────────
GRADE_MAP = [
    (90, 'A', 'Excellent'),
    (75, 'B', 'Good'),
    (60, 'C', 'Fair'),
    (40, 'D', 'Poor'),
    (0,  'F', 'Inadequate'),
]

# ── Vague / technical term lists ──────────────────────────────────────
VAGUE_TERMS = {
    'stuff', 'thing', 'things', 'item', 'items', 'product', 'products',
    'misc', 'miscellaneous', 'other', 'others', 'accessories', 'accessory',
    'parts', 'part', 'goods', 'articles', 'article', 'samples', 'sample',
    'material', 'materials', 'supplies', 'supply', 'equipment',
    'general', 'various', 'assorted', 'mixed', 'lot',
}

TECHNICAL_TERMS = {
    # Materials
    'cotton', 'polyester', 'nylon', 'silk', 'wool', 'linen', 'leather',
    'stainless', 'steel', 'aluminum', 'aluminium', 'copper', 'brass',
    'titanium', 'carbon', 'ceramic', 'glass', 'rubber', 'plastic',
    'polycarbonate', 'polypropylene', 'polyethylene', 'pvc', 'abs',
    'acrylic', 'silicone', 'latex', 'bamboo', 'wood', 'hardwood',
    # Construction / form
    'woven', 'knitted', 'knit', 'crocheted', 'molded', 'forged',
    'cast', 'stamped', 'extruded', 'machined', 'printed', 'laminated',
    'welded', 'soldered', 'assembled', 'finished', 'unfinished', 'raw',
    'powder', 'liquid', 'granular', 'pellet', 'sheet', 'rod', 'tube',
    'coil', 'bar', 'plate', 'wire', 'film', 'coating',
    # Specifics
    'gauge', 'diameter', 'thickness', 'grade', 'alloy', 'tensile',
    'voltage', 'wattage', 'ampere', 'ohm', 'capacity', 'density',
    'concentration', 'purity', 'iso', 'astm', 'din', 'ansi',
}

# ── Risk terms by HS chapter (IND correlation data, March 2026) ──────
RISK_TERMS = {
    (33, 34): {'wellbeing', 'myrothamnus', 'hyamagic', '4d', 'retinol', 'peptide', 'nutraceutical'},
    (30, 30): {'supplement', 'nutraceutical', 'probiotic'},
}

# ── Approved description templates (Phase 1: hardcoded) ──────────────
APPROVED_TEMPLATES: Dict[str, List[str]] = {
    '_default_cosmetics': [
        'hydrating facial serum, non-animal origin, non-colorant, personal & single use only',
        'moisturising lip balm with natural ingredients, non-colorant, non-animal origin, personal & single use only',
        'eye make up preparations, liquid serum, non colorant, non animal origin, no steel, no aluminium, personal and single use only',
        'face powder pressed facial powder non colorant non animal origin no steel no aluminium personal and single use only',
        'skin product for hydration deep hydrating facial formulation, non-animals origin, non-colorant. personal & single use only',
        'beautifying face tonic, hydrating facial toner, alcohol-free, non-animal origin, non-colorant, no steel, no aluminium, personal & single use only',
        'manicure preparations, moisturizing hand solutions for manicure use, non colorant, no steel, no aluminium, non animal origin, personal and single use only',
        'eye make up preparations. volumizing serum non colorant non animal origin no steel no aluminium personal and single use only',
        'pedicure preparations, moisturizing foot solution for pedicure use, non colorant, no steel, no aluminium, non animal origin, personal and single use only',
    ],
}

# ── Completeness element detectors ─────────────────────────────────────
# Each tuple: (element_name, points, regex_pattern)
COMPLETENESS_ELEMENTS = [
    ('product_type', 20, re.compile(
        r'\b(shirt|dress|pants|trousers|jacket|coat|shoes|boots|sneakers|'
        r'bolt|screw|nut|washer|valve|pump|motor|engine|battery|cable|'
        r'phone|tablet|laptop|monitor|keyboard|camera|lens|sensor|'
        r'cream|lotion|serum|shampoo|soap|detergent|'
        r'toy|doll|puzzle|game|furniture|chair|table|desk|lamp|'
        r'bottle|container|bag|box|case|pouch|jar|can|'
        r'food|supplement|vitamin|capsule|tablet|powder|'
        r'fabric|textile|yarn|thread|ribbon|tape|'
        r'tool|wrench|drill|saw|hammer|plier|clamp)\b',
        re.IGNORECASE,
    )),
    ('material', 20, re.compile(
        r'\b(cotton|polyester|nylon|silk|wool|linen|leather|suede|'
        r'steel|stainless|aluminum|aluminium|copper|brass|iron|'
        r'titanium|zinc|nickel|chrome|gold|silver|platinum|'
        r'plastic|rubber|silicone|glass|ceramic|porcelain|wood|'
        r'paper|cardboard|carbon|fibre|fiber|bamboo|'
        r'polycarbonate|polypropylene|polyethylene|pvc|abs|acrylic|'
        r'\d+%\s*\w+)\b',  # "65% cotton"
        re.IGNORECASE,
    )),
    ('form_state', 15, re.compile(
        r'\b(finished|unfinished|semi-finished|raw|refined|processed|'
        r'powder|liquid|solid|gas|granular|pellet|sheet|coil|'
        r'assembled|unassembled|kit|component|part|whole|bulk|'
        r'concentrated|diluted|dried|frozen|fresh|canned|'
        r'woven|knitted|crocheted|molded|forged|cast|extruded)\b',
        re.IGNORECASE,
    )),
    ('intended_use', 15, re.compile(
        r'\b(for\s+(?:men|women|children|kids|babies|industrial|'
        r'commercial|residential|medical|food|automotive|'
        r'construction|agricultural|marine|military|personal|'
        r'professional|laboratory|outdoor|indoor)(?:\s+use)?|'
        r'(?:men\'?s|women\'?s|children\'?s|kid\'?s|baby\'?s)|'
        r'food[- ]?grade|medical[- ]?grade|industrial[- ]?grade|'
        r'consumer|household|office|garden|pet)\b',
        re.IGNORECASE,
    )),
    ('dimensions_specs', 15, re.compile(
        r'(?:'
        r'\d+(?:\.\d+)?\s*(?:mm|cm|m|in|inch|ft|kg|g|lb|oz|ml|l|v|w|a|mah|rpm|mg|mcg|ug|IU|cc|units?|pcs?|ea)\b|'  # measurements incl pharma units
        r'(?:model|part|sku|ref|grade|type|class|size|style)\s*[:#]?\s*\w+|'  # model numbers
        r'\d+\s*x\s*\d+|'  # dimensions like 10x20
        r'M\d+(?:x\d+)?|'  # metric thread (M8x1.25)
        r'#\d+'  # gauge numbers
        r')',
        re.IGNORECASE,
    )),
    ('brand_model', 10, re.compile(
        r'(?:'
        r'(?:brand|model|manufacturer|made\s+by)\s*[:#]?\s*\w+|'
        r'\b[A-Z][A-Za-z]+(?:®|™|©)|'  # Trademarked names
        r'\b[A-Z][a-z]{2,}\s+\d+\s*(?:mg|mcg|ug|IU|ml|cc|units?)\b|'  # Brand + dosage (Dysport 500IU, Evlaa 40 mg)
        r'\b(?:Samsung|Apple|Sony|Nike|Adidas|Bosch|Siemens|3M|HP|Dell|'
        r'Dysport|Botox|Juvederm|Restylane|Xeomin|Radiesse|Belotero|'  # Aesthetics/pharma
        r'Medtronic|Stryker|Zimmer|DePuy|Synthes|Arthrex|'  # Medical devices
        r'Philips|GE|Honeywell|Caterpillar|Makita|DeWalt|Hilti|'  # Industrial
        r'LG|Panasonic|Toshiba|Canon|Epson|Brother|Lenovo|Asus)\b'
        r')',
        re.IGNORECASE,
    )),
    ('construction', 5, re.compile(
        r'\b(woven|knitted|knit|crocheted|nonwoven|felted|'
        r'molded|injection[- ]?molded|blow[- ]?molded|thermoformed|'
        r'forged|cast|die[- ]?cast|stamped|pressed|rolled|drawn|'
        r'welded|soldered|brazed|riveted|glued|bonded|laminated|'
        r'printed|screen[- ]?printed|embroidered|embossed|engraved)\b',
        re.IGNORECASE,
    )),
]

# ── Fix suggestion templates ──────────────────────────────────────────
SUGGESTION_TEMPLATES = {
    'missing_material': (
        "Add material composition (e.g., '65% cotton, 35% polyester' "
        "or 'stainless steel 304')"
    ),
    'missing_use': (
        "Specify intended use or end-user (e.g., 'for industrial machinery', "
        "'women\u2019s casual wear')"
    ),
    'missing_form': (
        "Describe the form or state (e.g., 'finished garment', 'raw powder', "
        "'liquid concentrate')"
    ),
    'missing_construction': (
        "Specify construction method (woven, knitted, molded, forged, etc.) "
        "to narrow the HS subheading"
    ),
    'missing_dimensions': (
        "Add specific measurements, model numbers, or technical grades "
        "to improve classification precision"
    ),
    'too_short': (
        "Description is only {word_count} word(s). Add product details "
        "to reach at least 6-8 words for reliable classification."
    ),
    'too_vague': (
        "Replace vague terms ({vague_terms}) with specific product "
        "characteristics."
    ),
    'classifier_disagree_hs2': (
        "Classifiers disagree at the HS chapter level \u2014 description is "
        "fundamentally ambiguous. Specify the exact product type."
    ),
    'classifier_disagree_hs6': (
        "Classifiers agree on the product category but differ on subheading. "
        "Add distinguishing details (material, construction, use)."
    ),
    'no_hs_code': (
        "No classifier could determine an HS code. Rewrite with the "
        "product\u2019s function, material, and form."
    ),
    'historical_drift': (
        "This product was previously classified as {old_hs}. Current "
        "description suggests {new_hs}. Verify the description hasn\u2019t "
        "changed meaning."
    ),
    'tariff_parroting': (
        "Description appears to copy HTS tariff schedule language verbatim. "
        "CBP considers this a prohibited practice (\u2018parroting the tariff\u2019). "
        "Rewrite using your own commercial product description with specific "
        "brand, model, material, and use details."
    ),
    'description_mutation': (
        "Description appears to be a modified version of approved text. "
        "Use the exact approved description or write entirely custom text "
        "— partial modifications trigger additional CBP scrutiny "
        "(18.9% vs 11.2% loss rate)."
    ),
    'description_too_long': (
        "Description exceeds 30 words. Over-detailed descriptions with "
        "product-specific terminology may trigger additional customs "
        "scrutiny. Aim for 10-20 words of customs-relevant information."
    ),
}

# ── HTS schedule description fragments (common tariff language) ────────
# Descriptions that closely match these are likely copied from the HTS
# schedule, which is a prohibited practice. CBP requires merchants to
# describe goods in their OWN commercial terms, not copy tariff headings.
HTS_SCHEDULE_PHRASES = [
    # Chapter 61-62: Apparel
    "articles of apparel and clothing accessories",
    "men's or boys' suits, ensembles, suit-type jackets",
    "women's or girls' suits, ensembles, suit-type jackets",
    "men's or boys' overcoats, car coats, capes, cloaks",
    "women's or girls' overcoats, car coats, capes, cloaks",
    "babies' garments and clothing accessories",
    "garments made up of knitted or crocheted fabrics",
    "other made-up clothing accessories",
    # Chapter 39: Plastics
    "other plates, sheets, film, foil and strip, of plastics",
    "articles of plastics and articles of other materials",
    # Chapter 73: Iron/Steel articles
    "other articles of iron or steel",
    "tubes, pipes and hollow profiles, of iron or steel",
    "screws, bolts, nuts, coach screws, screw hooks",
    # Chapter 84-85: Machinery / Electrical
    "parts suitable for use solely or principally with",
    "electrical apparatus for switching or protecting",
    "parts and accessories suitable for use solely",
    "machines and mechanical appliances having individual functions",
    # Chapter 94: Furniture
    "other furniture and parts thereof",
    "seats, whether or not convertible into beds",
    # Chapter 95: Toys
    "other toys; reduced-scale models and similar recreational models",
    # Generic tariff language patterns
    "not elsewhere specified or included",
    "of a kind used for",
    "of other materials",
    "other, including parts",
    "nesoi",  # Not Elsewhere Specified Or Included
]


# ═══════════════════════════════════════════════════════════════════════
# Signal calculators
# ═══════════════════════════════════════════════════════════════════════

def _score_consensus(classifier_results: List[Dict]) -> Tuple[int, Dict]:
    """Score classifier agreement at HS-2, HS-4, HS-6 levels.

    Returns (score_0_100, detail_dict).
    """
    valid = [r for r in classifier_results if r.get('hs6')]
    if len(valid) < 2:
        # Can't measure consensus with fewer than 2 classifiers
        if len(valid) == 1:
            # Single classifier returning a result IS a positive signal —
            # the description was parseable. Score 70 so single-classifier
            # invoices aren't unfairly penalized.
            return 70, {
                'score': 70,
                'reason': 'only_one_classifier',
                'hs2_agreement': True,
                'hs4_agreement': True,
                'hs6_agreement': True,
                'results': {r['classifier']: r for r in valid},
            }
        return 0, {
            'score': 0,
            'reason': 'no_classifiers_returned_results',
            'hs2_agreement': False,
            'hs4_agreement': False,
            'hs6_agreement': False,
            'results': {},
        }

    hs2_set = {r['hs6'][:2] for r in valid}
    hs4_set = {r['hs6'][:4] for r in valid}
    hs6_set = {r['hs6'][:6] for r in valid}

    n = len(valid)
    hs2_agree = len(hs2_set) == 1
    hs4_agree = len(hs4_set) == 1
    hs6_agree = len(hs6_set) == 1

    if hs6_agree:
        score = 100
    elif hs4_agree:
        score = 70 if n >= 3 else 80
    elif hs2_agree:
        # Agree at chapter but differ at subheading
        # Check how many agree at hs4
        from collections import Counter
        hs4_counts = Counter(r['hs6'][:4] for r in valid)
        max_agree_4 = hs4_counts.most_common(1)[0][1]
        if max_agree_4 >= 2:
            score = 50
        else:
            score = 40
    else:
        # Complete disagreement at HS-2
        from collections import Counter
        hs2_counts = Counter(r['hs6'][:2] for r in valid)
        max_agree_2 = hs2_counts.most_common(1)[0][1]
        if max_agree_2 >= 2:
            score = 20
        else:
            score = 0

    return score, {
        'score': score,
        'hs2_agreement': hs2_agree,
        'hs4_agreement': hs4_agree,
        'hs6_agreement': hs6_agree,
        'results': {r['classifier']: r for r in valid},
    }


def _score_completeness(description: str) -> Tuple[int, Dict]:
    """Check for presence of key customs classification elements."""
    present = []
    missing = []
    total_points = 0

    for name, points, pattern in COMPLETENESS_ELEMENTS:
        if pattern.search(description):
            present.append(name)
            total_points += points
        else:
            missing.append(name)

    score = min(total_points, 100)
    return score, {
        'score': score,
        'present': present,
        'missing': missing,
    }


def _score_specificity(description: str, hs_chapter: Optional[str] = None) -> Tuple[int, Dict]:
    """Measure how specific vs. vague the description is."""
    words = description.lower().split()
    word_count = len(words)

    # Word count score — with diminishing returns for 30+ words
    if word_count < 3:
        wc_score = 0
    elif word_count <= 5:
        wc_score = 30
    elif word_count <= 10:
        wc_score = 60
    elif word_count <= 20:
        wc_score = 80
    elif word_count <= 30:
        wc_score = 90
    else:
        wc_score = 70  # Diminishing returns — over-detailed descriptions

    # Technical terms — with risk term awareness
    risk_term_set = set()
    if hs_chapter:
        try:
            ch = int(hs_chapter[:2]) if len(hs_chapter) >= 2 else 0
        except (ValueError, TypeError):
            ch = 0
        for (start, end), terms in RISK_TERMS.items():
            if start <= ch <= end:
                risk_term_set = terms
                break

    tech_count = 0
    risk_count = 0
    risk_terms_found = []
    for w in words:
        clean = w.strip('.,;:()')
        if clean in risk_term_set:
            risk_count += 1
            risk_terms_found.append(clean)
        elif clean in TECHNICAL_TERMS:
            tech_count += 1

    tech_score = max(0, min(tech_count * 5, 30) - (risk_count * 5))

    # Vague terms — reduced penalty (5 per word, was 10)
    vague_found = [w for w in words if w.strip('.,;:()') in VAGUE_TERMS]
    vague_penalty = len(vague_found) * 5

    # If ONLY vague terms, floor at 10
    non_vague = [w for w in words if w.strip('.,;:()') not in VAGUE_TERMS
                 and len(w.strip('.,;:()')) > 2]
    if not non_vague and vague_found:
        raw_score = 10
    else:
        raw_score = wc_score * 0.4 + tech_score * 0.4 - vague_penalty * 0.2

    score = max(0, min(100, int(raw_score)))
    return score, {
        'score': score,
        'word_count': word_count,
        'technical_terms': tech_count,
        'risk_terms': risk_count,
        'risk_terms_found': risk_terms_found,
        'vague_terms': len(vague_found),
        'vague_terms_found': vague_found[:5],
    }


def _score_confidence_spread(classifier_results: List[Dict]) -> Tuple[int, Dict]:
    """Score the range between highest and lowest confidence."""
    confidences = [r.get('confidence', 0) for r in classifier_results
                   if r.get('confidence') is not None]

    if len(confidences) < 2:
        return 75, {'score': 75, 'spread': 0, 'highest': 0, 'lowest': 0}

    highest = max(confidences)
    lowest = min(confidences)
    spread = highest - lowest

    if spread < 0.10:
        score = 100
    elif spread < 0.20:
        score = 80
    elif spread < 0.35:
        score = 60
    elif spread < 0.50:
        score = 40
    elif spread < 0.70:
        score = 20
    else:
        score = 0

    return score, {
        'score': score,
        'spread': round(spread, 3),
        'highest': round(highest, 3),
        'lowest': round(lowest, 3),
    }


def _score_historical(description: str, hs6: Optional[str]) -> Tuple[int, Dict]:
    """Check consistency with ProductCatalog history + mutation detection."""

    # ── Phase 1: Approved template mutation detection ──
    normalized_desc = re.sub(r'[^\w\s]', '', description.lower()).strip()
    normalized_desc = re.sub(r'\s+', ' ', normalized_desc)

    templates_to_check = list(APPROVED_TEMPLATES.get('_default_cosmetics', []))

    for template in templates_to_check:
        normalized_tmpl = re.sub(r'[^\w\s]', '', template.lower()).strip()
        normalized_tmpl = re.sub(r'\s+', ' ', normalized_tmpl)

        if normalized_desc == normalized_tmpl:
            return 100, {
                'score': 100, 'has_history': True,
                'mutation_detected': False, 'matched_template': template,
            }

        if normalized_desc.startswith(normalized_tmpl):
            suffix = normalized_desc[len(normalized_tmpl):].strip()
            if len(suffix) > 0 and len(normalized_desc) > len(normalized_tmpl) * 1.1:
                return 30, {
                    'score': 30, 'has_history': True,
                    'mutation_detected': True, 'matched_template': template,
                    'mutation_suffix': suffix,
                    'warning': (
                        f'Description is a modified version of approved text. '
                        f'Suffix added: "{suffix[:50]}". Use exact approved text '
                        f'or write entirely custom — partial modifications '
                        f'trigger additional CBP scrutiny (18.9% loss rate).'
                    ),
                }

    # Phase 2: ProductCatalog lookup — DISABLED in POC (no DB)
    return 50, {'score': 50, 'has_history': False, 'reason': 'no_history_in_poc'}


def _score_tariff_parroting(description: str) -> Tuple[int, Dict]:
    """Detect if the description copies HTS tariff schedule language.

    CBP considers using tariff schedule descriptions verbatim on invoices
    a prohibited practice.  Even though classifiers will give high confidence,
    this will be flagged at customs and may result in penalties.

    Returns high score (100) when description does NOT parrot the tariff,
    and low score (0-30) when it closely matches tariff language.
    """
    desc_lower = description.lower().strip()
    matches = []

    for phrase in HTS_SCHEDULE_PHRASES:
        phrase_lower = phrase.lower()
        # Check for exact substring match or high overlap
        if phrase_lower in desc_lower:
            matches.append(phrase)
            continue
        # Check word overlap: if 80%+ of tariff phrase words appear in description
        phrase_words = set(phrase_lower.split())
        desc_words = set(desc_lower.split())
        if len(phrase_words) >= 3:
            overlap = len(phrase_words & desc_words) / len(phrase_words)
            if overlap >= 0.80:
                matches.append(phrase)

    if not matches:
        return 100, {
            'score': 100,
            'is_parroting': False,
            'matched_phrases': [],
        }

    # More matches = worse score
    if len(matches) >= 3:
        score = 0
    elif len(matches) == 2:
        score = 15
    else:
        score = 30

    return score, {
        'score': score,
        'is_parroting': True,
        'matched_phrases': matches[:3],
        'warning': (
            'Description appears to copy HTS tariff schedule language. '
            'CBP requires commercial descriptions in the importer\'s '
            'own terms, not tariff heading language.'
        ),
    }


# ═══════════════════════════════════════════════════════════════════════
# Fix suggestion generator
# ═══════════════════════════════════════════════════════════════════════

def _generate_suggestions(
    consensus_detail: Dict,
    completeness_detail: Dict,
    specificity_detail: Dict,
    historical_detail: Dict,
    tariff_parrot_detail: Optional[Dict] = None,
    hs_code: Optional[str] = None,
) -> List[str]:
    """Generate ordered fix suggestions (highest impact first).

    Args:
        hs_code: Optional HS code for context-aware material recommendations.
                 If provided and NOT in Section 232 chapters, skip steel/aluminum/copper penalties.
    """
    suggestions = []

    # Tariff parroting (highest priority — penalty risk)
    if tariff_parrot_detail and tariff_parrot_detail.get('is_parroting'):
        suggestions.append(('tariff_parroting', 50,
                            SUGGESTION_TEMPLATES['tariff_parroting']))

    # Classifier disagreement (highest impact)
    if not consensus_detail.get('hs2_agreement') and consensus_detail.get('score', 100) < 40:
        suggestions.append(('classifier_disagree_hs2', 40,
                            SUGGESTION_TEMPLATES['classifier_disagree_hs2']))
    elif not consensus_detail.get('hs6_agreement') and consensus_detail.get('score', 100) < 80:
        suggestions.append(('classifier_disagree_hs6', 30,
                            SUGGESTION_TEMPLATES['classifier_disagree_hs6']))

    if consensus_detail.get('reason') == 'no_classifiers_returned_results':
        suggestions.append(('no_hs_code', 50,
                            SUGGESTION_TEMPLATES['no_hs_code']))

    # Completeness gaps — with Section 232 context awareness
    missing = completeness_detail.get('missing', [])
    element_to_suggestion = {
        'material': 'missing_material',
        'intended_use': 'missing_use',
        'form_state': 'missing_form',
        'construction': 'missing_construction',
        'dimensions_specs': 'missing_dimensions',
    }

    # Check if this HS code is subject to Section 232 metal tariffs
    # Uses RAG-backed cache table (section_232_codes) with heading-level fallback
    is_section_232 = False
    if hs_code:
        # Inline Section 232 detection (POC has no RAG lookup)
        hs_clean = (hs_code or '').replace('.', '').replace(' ', '')
        try:
            heading = int(hs_clean[:4]) if len(hs_clean) >= 4 else 0
        except ValueError:
            heading = 0
            _FALLBACK_STEEL = {7206,7207,7208,7209,7210,7211,7212,7213,7214,7215,7216,7217,7218,7219,7220,7221,7222,7223,7224,7225,7226,7227,7228,7229,7301,7302,7304,7305,7306,7307,7308,7309,7310,7311,7312,7313,7314,7315,7316,7317,7318,7319,7320,7321,7322,7323,7324,7325,7326}
            _FALLBACK_ALUM = {7601,7602,7603,7604,7605,7606,7607,7608,7609,7610,7611,7612,7613,7614,7615,7616}
            is_section_232 = heading in _FALLBACK_STEEL or heading in _FALLBACK_ALUM

    for elem in missing:
        if elem == 'material' and not is_section_232:
            # For non-Section 232 codes (e.g., apparel, toys, plastics), use softer language
            suggestions.append((elem, 10,
                                "Consider adding material composition (e.g., '100% cotton' "
                                "or 'plastic') for better classification accuracy."))
        else:
            key = element_to_suggestion.get(elem)
            if key:
                suggestions.append((key, 20, SUGGESTION_TEMPLATES[key]))

    # Specificity issues
    wc = specificity_detail.get('word_count', 0)
    if wc < 6:
        suggestions.append(('too_short', 25,
                            SUGGESTION_TEMPLATES['too_short'].format(word_count=wc)))

    vague = specificity_detail.get('vague_terms_found', [])
    if vague:
        suggestions.append(('too_vague', 15,
                            SUGGESTION_TEMPLATES['too_vague'].format(
                                vague_terms=', '.join(vague[:3]))))

    # Description too long
    if specificity_detail.get('word_count', 0) > 30:
        suggestions.append(('too_long', 12,
                            SUGGESTION_TEMPLATES['description_too_long']))

    # Mutation detection
    if historical_detail.get('mutation_detected'):
        suggestions.append(('description_mutation', 42,
                            SUGGESTION_TEMPLATES['description_mutation']))

    # Historical drift
    if historical_detail.get('has_history') and historical_detail.get('score', 100) < 50:
        old_hs = historical_detail.get('historical_hs6', '?')
        new_hs = historical_detail.get('current_hs6', '?')
        suggestions.append(('historical_drift', 10,
                            SUGGESTION_TEMPLATES['historical_drift'].format(
                                old_hs=old_hs, new_hs=new_hs)))

    # Sort by impact (highest first), return text only, max 5
    suggestions.sort(key=lambda x: x[1], reverse=True)
    return [s[2] for s in suggestions[:5]]


# ═══════════════════════════════════════════════════════════════════════
# Main analyzer
# ═══════════════════════════════════════════════════════════════════════

def _get_grade(score: int) -> Tuple[str, str]:
    """Return (grade_letter, grade_label) for a 0-100 score."""
    for threshold, letter, label in GRADE_MAP:
        if score >= threshold:
            return letter, label
    return 'F', 'Inadequate'



def analyze_description(
    description: str,
    classifier_results: Optional[List[Dict]] = None,
    hs_code_declared: Optional[str] = None,
) -> Dict[str, Any]:
    """Score a product description against the 6 DQA signals.

    The standalone POC is classifier-agnostic — caller supplies
    ``classifier_results`` (a list of dicts with ``classifier``, ``hs6``,
    ``hs_code``, ``confidence``).  In the dqa-poc service, this list is
    populated by parallel calls to Avalara HSAC + HSAC10.

    Returns a dict with quality_score, grade, signals, recommendations,
    recommended_hs_code, and brand_info=None.
    """
    if not description or not description.strip():
        return {
            "quality_score": 0,
            "grade": "F",
            "grade_label": "Inadequate",
            "signals": {
                "classifier_consensus": {"score": 0, "reason": "empty_description"},
                "completeness": {"score": 0, "present": [], "missing": [e[0] for e in COMPLETENESS_ELEMENTS]},
                "specificity": {"score": 0, "word_count": 0},
                "confidence_spread": {"score": 0},
                "historical": {"score": 50, "has_history": False},
                "tariff_parroting": {"score": 100, "is_parroting": False, "matched_phrases": []},
            },
            "recommendations": ["Provide a product description for classification."],
            "recommended_hs_code": None,
            "recommended_hs_confidence": None,
            "brand_info": None,
        }

    desc_clean = description.strip()
    classifier_results = classifier_results or []

    best_result = (
        max(classifier_results, key=lambda r: r.get("confidence", 0))
        if classifier_results else None
    )
    best_hs6 = best_result["hs6"] if best_result else None
    hs_chapter = best_result["hs6"][:2] if best_result else None

    consensus_score, consensus_detail = _score_consensus(classifier_results)
    completeness_score, completeness_detail = _score_completeness(desc_clean)
    specificity_score, specificity_detail = _score_specificity(desc_clean, hs_chapter=hs_chapter)
    spread_score, spread_detail = _score_confidence_spread(classifier_results)
    historical_score, historical_detail = _score_historical(desc_clean, best_hs6)
    tariff_score, tariff_detail = _score_tariff_parroting(desc_clean)

    quality_score = int(
        consensus_score * W_CONSENSUS
        + completeness_score * W_COMPLETENESS
        + specificity_score * W_SPECIFICITY
        + spread_score * W_CONFIDENCE_SPREAD
        + historical_score * W_HISTORICAL
        + tariff_score * W_TARIFF_PARROT
    )
    quality_score = max(0, min(100, quality_score))
    grade, grade_label = _get_grade(quality_score)

    recommendations = _generate_suggestions(
        consensus_detail, completeness_detail, specificity_detail,
        historical_detail, tariff_detail,
        hs_code=(best_result["hs_code"] if best_result else hs_code_declared),
    )

    return {
        "quality_score": quality_score,
        "grade": grade,
        "grade_label": grade_label,
        "signals": {
            "classifier_consensus": consensus_detail,
            "completeness": completeness_detail,
            "specificity": specificity_detail,
            "confidence_spread": spread_detail,
            "historical": historical_detail,
            "tariff_parroting": tariff_detail,
        },
        "recommendations": recommendations,
        "recommended_hs_code": best_result["hs_code"] if best_result else None,
        "recommended_hs_confidence": best_result["confidence"] if best_result else None,
        "brand_info": None,
    }
