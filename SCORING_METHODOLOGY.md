# Company Scoring Methodology

How DonorIQ turns raw research data into a score, a tier, and a pipeline
stage for a company. This documents the code as it currently behaves, not
the original design guidelines — where the code diverges from the
guidelines (e.g. weights zeroed out, compliance never blocking), that is
called out explicitly.

## 1. Pipeline order

```
research_company()               research_agent.py
        |
research_company_with_financials()   research_agent.py (Screener.in + CSR annexure PDF)
        |
check_compliance()               compliance_agent.py
        |
score_company()                  scoring_agent.py  ──┬── rate_scoring_and_fitment()   (LLM)
                                                       ├── fitment_agents.decide_record_stage()
                                                       └── writes score/tier/record_stage to Mongo
```

Each company ends up with **three separate, independently-computed outputs**
stored on the document: `score` (0–100), `category` (Tier A/B/C/Not a Fit),
and `record_stage` (Prospect/Nurture/Enriched Data/Disqualified). They are
shown together in the UI but are not derived from one another except where
noted below.

---

## 2. Compliance check (`compliance_agent.py`)

Runs before scoring. Flags issues (missing `source_url`, unverified
`education_csr_spend`) and sets `confidence: verified|unverified` — **but
`blocked` is hardcoded to `False`**. Nothing is ever hard-dropped at this
stage; weak/unverified data is left for the fitment checklist (`§4`) to
route into Nurture/Enriched Data/Disqualified instead.

---

## 3. The 0–100 score and Tier (`scoring_agent.py`)

### 3.1 Factor ratings
One LLM call (`rate_scoring_and_fitment`) rates 8 factors from 0.0–1.0 based
strictly on the research JSON (grounding rules: no outside knowledge; missing
data → 0.0–0.2; only rate above 0.5 if the data explicitly supports it).

### 3.2 Weights (`WEIGHTS` dict)

| Factor | Weight | Counted? |
|---|---|---|
| `education_focus` | 30 | Yes |
| `spend_capacity` | 30 | Yes |
| `geography_match` | 20 | Yes |
| `strategic_fit` | 20 | Yes |
| `decision_maker_access` | 0 | No — rated by the LLM but contributes nothing |
| `urgency_signal` | 0 | No |
| `governance_quality` | 0 | No |
| `warm_connection` | 0 | No |

**`final_score = round(Σ rating × weight)`**, over a max of 100.

Geography match is explicitly graded: High (3+ states / pan-India) →
0.8–1.0, Medium (2 states) → 0.4–0.6, Low/empty → 0.0–0.2.

### 3.3 Hard financial override on `spend_capacity`
If the company independently meets the hard financial "prospect" criteria
(`financial_extractor.check_prospect_criteria`, computed during the
financials stage and stored as `is_prospect`), `spend_capacity` is forced to
a full **1.0** regardless of what the LLM guessed from text — real Screener
numbers override a qualitative guess.

`check_prospect_criteria` (Companies Act Sec 135 basis), evaluated on the
most recent completed fiscal year:

```
turnover   >= ₹1000 Cr   OR
net_worth  >= ₹500 Cr    OR
net_profit >= ₹5 Cr
```
Any one of the three trips it (OR, not AND — deliberately loosened from an
earlier stricter AND-based rule that excluded genuinely obligated companies).

> A second, currently **commented out** gate exists in the code
> (`scoring_agent.py` around line 339): a 2%-of-average-PBT /
> 2%-of-average-net-profit CSR-capacity check that would zero out
> `spend_capacity` if both 3-year averages are below ₹3 Cr. It is inactive.

### 3.4 Tier bands (`assign_category`)
Applied to `final_score`, contiguous and gap-free:

| Score | Tier |
|---|---|
| ≥ 65 | Tier A |
| 30–64 | Tier B |
| < 30 | Tier C |

### 3.5 Program-fitment gate on the tier
A high numeric score alone is **not sufficient**. `program_fit["Ennoble
Fitment"]` (the strongest of the 6 program fits, see §3.6) must be at least
**Medium Fit**, or the company is force-set to category **"Not a Fit"** and
excluded from Tier A/B/C entirely — the numeric score is kept for
diagnostics but no longer drives the category.

```
if Ennoble Fitment in {High Fit, Medium Fit}:
    category = Tier A/B/C via score
else:
    category = "Not a Fit"
```

### 3.6 Program fitment (6 Ennoble programs)
Same LLM call also rates fit against each program as
`High Fit / Medium Fit / Low Fit / Not Evident`:

- STEM Education
- School Infrastructure Transformation
- Holistic School Transformation
- Anganwadi Transformation
- Quality Education
- Model School Transformation

`Ennoble Fitment` = the single strongest label across all 6
(`best_program_fit`, ranked High=3 > Medium=2 > Low=1 > Not Evident=0).

**Keyword fallback** (`compute_fallback_program_fitment`) fills in any
program the LLM left as `Not Evident` or missing, using — in priority order:
1. Dedicated per-program Yes/No CSR flag field (e.g. `csr_stem_education`)
   — Yes → High Fit, No → Low Fit.
2. High-signal keyword match in CSR-focus/thematic-focus/past-projects text
   → High Fit.
3. Medium-signal (broader) keyword match → Medium Fit.
4. Otherwise → Not Evident.

The fallback text is built **only from field values**, never from
`json.dumps()` of the whole record — field *names* like
`csr_stem_education` contain the same trigger words as the keywords, which
previously caused false positives on empty records.

### 3.7 Score reliability caveat
If the LLM call itself fails (e.g. rate-limited), `ratings = {}` and
`final_score` comes out as `0` — indistinguishable from a genuinely
zero-rated company by the score alone. `scoring_error` is stored separately
on the record specifically so the UI/reviewer can tell "unrated due to API
failure" apart from "actually scored 0."

---

## 4. Record stage (`fitment_agents.decide_record_stage`)

Independent of the numeric score. A deterministic 7-item checklist:

| Checklist item | Passes when |
|---|---|
| `csr_activity_visible` | `company_csr_focus` is present and not "Not Found" |
| `education_relevance` | CSR focus/thematic-focus text contains education/school/child/infra/anganwadi/learning keywords, **or** any of the 6 per-program Yes/No flags is Yes |
| `ennoble_fit_clear` | At least one of the 6 programs is High or Medium Fit |
| `spend_priority_ok` | `csr_spend_priority` is High or Medium |
| `geography_priority_ok` | `geographical_priority` is High or Medium |
| `source_backed_description` | Same as `csr_activity_visible` |
| `contact_route_exists` | A named contact was found, **or** the company has its own foundation |

`passed_count` = number of checklist items that are `True`.

Decision rule (in order):

1. **Disqualified** — no education/CSR relevance at all AND no Ennoble
   program fit.
2. **Prospect** — CSR-relevant AND clear Ennoble fit AND `passed_count >= 6`.
3. **Nurture** — CSR-relevant AND (some Ennoble fit OR `passed_count >= 4`).
4. **Enriched Data** — everything else (fit unclear; kept as researched
   data rather than dropped).

---

## 5. Where each input comes from

| Field used in scoring | Populated by |
|---|---|
| `company_csr_focus`, `thematic_focus`, contact info, geo/spend priority | `research_agent.research_company()` → LLM extraction (`extraction_tool.py`) |
| `csr_<program>_transformation` flags | `extraction_tool.extract_education_fields()` |
| `is_prospect`, turnover/net worth/net profit | `research_agent.research_company_with_financials()` → `search_tool.get_financials_from_screener()` → `financial_extractor.check_prospect_criteria()` |
| `csr_budget_2pct` / `csr_budget_2pct_net_profit` | `financial_extractor.calculate_csr_budget()` / `calculate_net_profit_csr_budget()` (currently only diagnostic — the CSR-capacity gate that would use them is commented out) |
| `compliance_issues`, `confidence` | `compliance_agent.check_compliance()` (never blocks) |

---

## 6. Known gaps / things to be aware of

- 4 of the 8 rated scoring factors (`decision_maker_access`,
  `urgency_signal`, `governance_quality`, `warm_connection`) are rated by
  the LLM every run but carry **zero weight** — wasted LLM output unless
  intentionally reserved for future use.
- The PBT/net-profit 2%-CSR-capacity gate is present in code but disabled
  (commented out).
- `check_prospect_criteria` uses **OR** across turnover/net worth/net
  profit — any single threshold trips it, not all three.
- A `final_score` of `0` can mean either "genuinely scored 0" or "LLM call
  failed" — always check `scoring_error` alongside the score.
- `category = "Not a Fit"` overrides Tier A/B/C but does **not** override
  `record_stage` — a company can be "Not a Fit" on the score side while
  still being "Prospect" on the record-stage side if the checklist passes;
  these two outputs are not reconciled with each other.

---

## 7. File reference

- `scoring_agent.py` — factor weights, LLM scoring call, tier assignment,
  fitment-gate, `score_company()` orchestration.
- `fitment_agents.py` — record-stage checklist and decision rule.
- `compliance_agent.py` — non-blocking pre-scoring compliance flags.
- `financial_extractor.py` — prospect criteria, CSR budget math.
- `models.py` — `ScoringResults`, `FitmentDecision`, `CompanyResearch`
  schemas that carry the fields referenced above.
