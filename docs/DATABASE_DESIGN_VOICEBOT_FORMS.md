# Database Design — Voicebot Nutrition Consultation Capture

Companion document to `Database Design.docx`. That document covers master data,
configurational data, and the conversational transcript. This document covers
**what the voicebot extracts during a consultation and where the answer lands** —
the nutrition consultation form.

The existing `conversations` collection records *what was said*. It cannot answer
"what is this patient's current weight?" or "who entered that value?". So the form
is modelled separately, with one record per captured value rather than one record
per consultation.

---

## 1. Design principles

These drive every decision below.

**1.1 The schema is data-driven, not code-driven.**
Fields are documents in `form_fields`, not columns. A new question is a new
document — no migration, no redeploy. The bot's extraction prompt is generated
from these documents, so the form and the prompt can never drift apart.

**1.2 Every value carries its provenance.**
The brief states that the nutritionist's assessment fields "should generally be
completed by the nutritionist, rather than automatically inferred by the
voicebot." A single `nutrition_assessment` string cannot honour that. Every
captured value therefore records **who or what produced it**, so a human's entry
is never silently overwritten by a misheard sentence.

**1.3 The bot must never overwrite a human.**
Enforced in the application layer (see §6.1) and visible in the schema, because
`source` is on the value itself. This is the single most important rule here: a
bot that can clobber a nutritionist's clinical judgement is unsafe to deploy.

**1.4 Measurements are numeric, with the unit beside them.**
The current extractor returns strings like `"72.5 kg"`. You cannot compute BMI,
trend weight loss, or compare against a target from that. Measurements are stored
as `value_num` + `unit`, with the raw utterance kept for audit.

**1.5 Derived values are computed, not stored.**
`age` and `bmi` are projections of `date_of_birth` + height + weight. Storing them
invites the two from disagreeing. They are computed on read, and marked
`is_derived` in the field definition so the UI knows to render them read-only.

**1.6 Only what was actually said is stored.**
The prompt already instructs the model to omit unspoken fields. A field with no
value has **no row** in `consultation_field_values` — it is not stored as `null`.
This keeps "not mentioned" distinguishable from "mentioned as unknown", and keeps
partial consultations (the normal case) cheap.

---

## 2. Configurational Data

### 2.1 Form Templates Collection

`form_templates` describes the form itself. One document per form version, so a
published form can be edited without altering consultations already captured
against it — `consultations.template_id` pins the exact version used.

| Field | Data Type | Required | Constraints |
|---|---|---|---|
| `_id` | ObjectId | Yes | Primary identifier, auto-generated |
| `form_code` | String | Yes | Unique, system-readable code, e.g. `NUTRITION_CONSULTATION` |
| `name` | String | Yes | Non-empty display name |
| `version` | Number | Yes | Monotonically increasing per `form_code`; not reused once referenced |
| `description` | String | No | Purpose of the form |
| `section_order` | Array of String | Yes | Ordered list of `section_code` values, controls rendering order |
| `status` | String (Value Set) | Yes | `DRAFT` \| `PUBLISHED` \| `RETIRED`; default `DRAFT` |
| `is_active` | Boolean | Yes | Default true |
| `is_delete` | Boolean | Yes | Indicates soft deletion; default false |
| `created_by` | ObjectId | Conditional | User ID of creator |
| `updated_by` | ObjectId | Conditional | User ID of last updater |
| `created_at` | DateTime | Yes | Automatically generated |
| `updated_at` | DateTime | Yes | Automatically updated |

```json
{
  "_id": ObjectId("68cbf0010000000000000001"),
  "form_code": "NUTRITION_CONSULTATION",
  "name": "Nutrition Consultation",
  "version": 1,
  "description": "Initial nutrition assessment captured during a voice consultation.",
  "section_order": [
    "PATIENT_DETAILS",
    "MEDICAL_HISTORY",
    "ANTHROPOMETRIC",
    "DIETARY_HABITS",
    "LIFESTYLE",
    "ASSESSMENT",
    "APPOINTMENT"
  ],
  "status": "PUBLISHED",
  "is_active": true,
  "is_delete": false,
  "created_at": ISODate("2026-09-28T08:00:00Z"),
  "updated_at": ISODate("2026-09-28T08:00:00Z")
}
```

### 2.2 Form Fields Collection

`form_fields` is the authoritative field list. It is both the render definition
and the bot's extraction contract.

| Field | Data Type | Required | Constraints |
|---|---|---|---|
| `_id` | ObjectId | Yes | Primary identifier, auto-generated |
| `template_id` | ObjectId | Yes | References `form_templates._id` |
| `section_code` | String | Yes | Must exist in the parent template's `section_order` |
| `field_code` | String | Yes | Unique within the template; stable system-readable code |
| `label` | String | Yes | Non-empty; shown to the nutritionist |
| `data_type` | String (Value Set) | Yes | `TEXT` \| `LONG_TEXT` \| `DATE` \| `NUMBER` \| `PHONE` \| `EMAIL` \| `ENUM` \| `MULTI_ENUM` \| `AUTO_ID` \| `DERIVED` |
| `capture` | String (Value Set) | Yes | `VOICE` \| `HUMAN_ONLY` \| `SYSTEM`; default `VOICE` — see §2.3 |
| `is_required` | Boolean | Yes | Default false |
| `is_derived` | Boolean | Yes | Default false; true for `age` and `bmi` — computed on read, never stored |
| `derivation` | Object | Conditional | Required when `is_derived` is true; holds the formula (see §2.4) |
| `value_set_code` | String | Conditional | Required for `ENUM` / `MULTI_ENUM`; references `value_sets.set_code` |
| `default_value_set_code` | String | Conditional | Advisory default for the bot when the speaker is ambiguous |
| `unit` | String | Conditional | Required for `NUMBER` measurements, e.g. `cm`, `kg`, `litres`, `hours` |
| `numeric_range` | Object | Conditional | `{ "min": Number, "max": Number }`; clinically implausible values are rejected |
| `required_when` | Object | Conditional | Conditional requirement expression (see §2.5) |
| `capture_prompt` | String | Conditional | Plain-language hint appended to the LLM prompt for this field |
| `sort_order` | Number | Yes | Position within the section |
| `is_active` | Boolean | Yes | Default true |
| `is_delete` | Boolean | Yes | Indicates soft deletion; default false |
| `created_at` | DateTime | Yes | Automatically generated |
| `updated_at` | DateTime | Yes | Automatically updated |

```json
{
  "_id": ObjectId("68cbf0020000000000000003"),
  "template_id": ObjectId("68cbf0010000000000000001"),
  "section_code": "ANTHROPOMETRIC",
  "field_code": "current_weight_kg",
  "label": "Current weight (kg)",
  "data_type": "NUMBER",
  "capture": "VOICE",
  "is_required": true,
  "is_derived": false,
  "unit": "kg",
  "numeric_range": { "min": 20, "max": 400 },
  "capture_prompt": "Patient's current weight in kilograms. Do not confuse with a target or previous weight.",
  "sort_order": 2,
  "is_active": true,
  "is_delete": false
}
```

### 2.3 The `capture` field

This is the mechanism that implements the brief's split between what the bot may
infer and what it may not.

| Value | Meaning | Effect on the bot |
|---|---|---|
| `VOICE` | Bot may extract this from speech | Field is included in the extraction prompt |
| `HUMAN_ONLY` | Clinical judgement, nutritionist's responsibility | Field is **excluded from the extraction prompt entirely**; the bot does not even know it exists |
| `SYSTEM` | Generated by the application | Auto-generated ID, timestamps, calculated values |

`HUMAN_ONLY` is stronger than "prefer human input". The bot is never asked about
these fields, so it cannot hallucinate a clinical assessment. If the bot *does*
return one, the application rejects it — see §6.1.

This is why `nutrition_assessment` and `nutritionist_recommendations` are
`HUMAN_ONLY`, and `nutritionist_id` is recorded on the value row.

### 2.4 Derived fields

`is_derived: true` fields are computed on read. `derivation` is documentation plus
a machine-checkable name; the computation lives in the API layer, not the database.

| Field | Data type | Derivation | Note |
|---|---|---|---|
| `age` | `DERIVED` | `floor((consultation_date - date_of_birth) / 365.2425 days)` | Years at the **consultation date**, not today — a patient's age is a fact about the visit. Falls back to the value spoken in session if DOB was never captured. |
| `bmi` | `DERIVED` | `current_weight_kg / (height_cm / 100)^2` | Only when both inputs exist; rounded to 1 decimal. |
| `weight_change_kg` | `DERIVED` | `current_weight_kg - previous_weight_kg` | Negative means loss. |
| `weight_to_target_kg` | `DERIVED` | `target_weight_kg - current_weight_kg` | |

### 2.5 Conditional requirements

`required_when` is a small declarative expression, evaluated against the
consultation's current values. This avoids burying clinical rules in application
code where they cannot be reviewed or changed by a dietitian.

```json
{ "field": "appointment_type", "operator": "equals", "value": "FOLLOW_UP" }
```

The operators are `equals`, `not_equals`, `in`, `not_in`, `is_empty`, `gt`, `lt`.
An unknown operator must fail closed (treat as **required**), never silently pass.

### 2.6 Value Sets additions

Reuse the existing `value_sets` collection from `Database Design.docx`; these are
new `set_code` entries, not a new collection. Each follows the documented
`{ code, value, labels }` item structure.

| `set_code` | Values |
|---|---|
| `GENDER` | `MALE`, `FEMALE`, `OTHERS` (already exists in the master data) |
| `DIETARY_PREFERENCE` | `VEGETARIAN`, `VEGAN`, `NON_VEGETARIAN`, `EGGETARIAN`, `OTHER` |
| `APPETITE` | `POOR`, `NORMAL`, `INCREASED` |
| `PHYSICAL_ACTIVITY_LEVEL` | `SEDENTARY`, `LIGHT`, `MODERATE`, `ACTIVE` |
| `SLEEP_QUALITY` | `POOR`, `AVERAGE`, `GOOD` |
| `STRESS_LEVEL` | `LOW`, `MODERATE`, `HIGH` |
| `YES_NO` | `YES`, `NO` |
| `ALCOHOL_FREQUENCY` | `NEVER`, `OCCASIONAL`, `WEEKLY`, `DAILY` |
| `CONDITION_TYPE` | `DIABETES`, `HYPERTENSION`, `THYROID`, `PCOS`, `ANAEMIA`, `OTHER` |
| `FIELD_SOURCE` | `VOICE_BOT`, `NUTRITIONIST`, `ADMIN`, `IMPORT`, `CALCULATED` |
| `CAPTURE_MODE` | `VOICE`, `HUMAN_ONLY`, `SYSTEM` |

Note `YES_NO` and `FIELD_SOURCE` are **shared sets**. Storing `smoking_status` as a
free-text `"Yes"` makes it unqueryable, because `"yes"`, `"Yes"` and `"Y"` are three
different strings. A closed set plus the per-value `labels` object is what keeps
analytics honest.

---

## 3. Transactional Data

### 3.1 Consultations Collection

One document per consultation. Deliberately sparse: it holds identity, the
appointment, and references. All field answers live in
`consultation_field_values`, so a document never grows past a few kilobytes
regardless of how much was captured.

| Field | Data Type | Required | Constraints |
|---|---|---|---|
| `_id` | ObjectId | Yes | Primary identifier, auto-generated |
| `consultation_code` | String | Yes | Unique, human-readable, e.g. `NUT-20260928-0007` |
| `patient_id` | String | Yes | Auto-generated; unique. Format `NUT-<YYYYMMDD>-<seq>`, allocated by the application under a unique index |
| `template_id` | ObjectId | Yes | References `form_templates._id`; pins the field definition version used |
| `user_id` | ObjectId | Conditional | References `users._id`; set when a signed-in user is identified |
| `patient_name` | String | No | Denormalised for search; also recoverable from `full_name` |
| `nutritionist_id` | ObjectId | Conditional | References `users._id`; required once a plan is signed off |
| `conversation_id` | ObjectId | Conditional | References `conversations._id` when a voice session produced this record |
| `session_code` | String | No | Voicebot session identifier, for traceability back to the transcript |
| `consultation_date` | Date | Yes | Must not be in the future |
| `follow_up_date` | Date | Conditional | Must be after `consultation_date`; required when the consultation is not one-off |
| `status` | String (Value Set) | Yes | `IN_PROGRESS` \| `AWAITING_REVIEW` \| `COMPLETED` \| `CANCELLED`; default `IN_PROGRESS` |
| `completion_percent` | Number | Yes | Derived on read; stored denormalised for list views. 0–100 |
| `captured_fields_count` | Number | Yes | Number of `VOICE`-capture fields with a stored value |
| `reviewer_notes` | String | No | Human notes at sign-off |
| `is_active` | Boolean | Yes | Default true |
| `is_delete` | Boolean | Yes | Indicates soft deletion; default false |
| `created_by` | ObjectId | Conditional | User ID of creator |
| `updated_by` | ObjectId | Conditional | User ID of last updater |
| `created_at` | DateTime | Yes | Automatically generated |
| `updated_at` | DateTime | Yes | Automatically updated |

```json
{
  "_id": ObjectId("68cbf0100000000000000001"),
  "consultation_code": "NUT-20260928-0007",
  "patient_id": "NUT-20260928-0007",
  "template_id": ObjectId("68cbf0010000000000000001"),
  "user_id": ObjectId("68cbe0000000000000000002"),
  "patient_name": "Priya Sharma",
  "session_code": "20260928_154205",
  "consultation_date": ISODate("2026-09-28T00:00:00Z"),
  "follow_up_date": ISODate("2026-10-26T00:00:00Z"),
  "status": "AWAITING_REVIEW",
  "completion_percent": 78,
  "captured_fields_count": 28,
  "is_active": true,
  "is_delete": false,
  "created_at": ISODate("2026-09-28T15:42:05Z"),
  "updated_at": ISODate("2026-09-28T15:58:11Z")
}
```

### 3.2 Consultation Field Values Collection

The core collection. **One document per captured value**, append-only, with
supersession instead of overwrite. This is what makes provenance, correction
history, and the "bot never overwrites a human" rule enforceable.

| Field | Data Type | Required | Constraints |
|---|---|---|---|
| `_id` | ObjectId | Yes | Primary identifier, auto-generated |
| `consultation_id` | ObjectId | Yes | References `consultations._id` |
| `field_id` | ObjectId | Yes | References `form_fields._id`; the `field_code` is denormalised onto this row for query convenience |
| `field_code` | String | Yes | Denormalised from `form_fields`; e.g. `current_weight_kg` |
| `section_code` | String | Yes | Denormalised, enables per-section progress queries without a join |
| `value_text` | String | Conditional | Required for `TEXT`, `LONG_TEXT`, `PHONE`, `EMAIL`, `ENUM` |
| `value_num` | Number | Conditional | Required for `NUMBER`; must satisfy `numeric_range` |
| `value_date` | Date | Conditional | Required for `DATE`; must be a valid, non-future date where applicable |
| `value_enum` | Array of String | Conditional | Required for `MULTI_ENUM`; each item must exist in the referenced value set |
| `unit` | String | Conditional | Denormalised for `NUMBER`, so a later read needs no join |
| `raw_utterance` | String | No | The exact transcript fragment the value came from; the audit trail for a `VOICE_BOT` value |
| `confidence` | Number | Conditional | 0.0–1.0; populated for `VOICE_BOT` values only. Values below `0.7` are queued for review |
| `source` | String (Value Set) | Yes | `VOICE_BOT` \| `NUTRITIONIST` \| `ADMIN` \| `IMPORT` \| `CALCULATED` |
| `captured_by` | ObjectId | Conditional | References `users._id`; required when `source` is not `VOICE_BOT` |
| `transcript_start_ms` | Number | No | Offset into the session transcript, so the UI can replay the moment the value was heard |
| `transcript_end_ms` | Number | No | End offset of that fragment |
| `supersedes_id` | ObjectId | Conditional | References the previous value row this one replaces |
| `is_current` | Boolean | Yes | Exactly one current row per (`consultation_id`, `field_code`); superseded rows set false |
| `review_status` | String (Value Set) | Yes | `PENDING` \| `ACCEPTED` \| `REJECTED`; `ACCEPTED` is implicit when `source` is `NUTRITIONIST` |
| `reviewed_by` | ObjectId | Conditional | References `users._id`; who accepted or rejected the value |
| `reviewed_at` | DateTime | Conditional | When the review happened |
| `created_at` | DateTime | Yes | Automatically generated |
| `updated_at` | DateTime | Yes | Automatically updated |

```json
{
  "_id": ObjectId("68cbf0200000000000000001"),
  "consultation_id": ObjectId("68cbf0100000000000000001"),
  "field_id": ObjectId("68cbf0020000000000000003"),
  "field_code": "current_weight_kg",
  "section_code": "ANTHROPOMETRIC",
  "value_num": 68.4,
  "unit": "kg",
  "raw_utterance": "I currently weigh about sixty eight kilos",
  "confidence": 0.91,
  "source": "VOICE_BOT",
  "transcript_start_ms": 412000,
  "transcript_end_ms": 412410,
  "supersedes_id": null,
  "is_current": true,
  "review_status": "PENDING",
  "created_at": ISODate("2026-09-28T15:49:02Z"),
  "updated_at": ISODate("2026-09-28T15:49:02Z")
}
```

### 3.3 Field Capture Events Collection

An append-only log of every extraction attempt, including the ones that captured
nothing. A separate collection rather than a flag on the value row, because
"we asked and got nothing" is exactly the signal needed to debug a form that
silently under-fills — and it cannot be stored on a row that does not exist.

| Field | Data Type | Required | Constraints |
|---|---|---|---|
| `_id` | ObjectId | Yes | Primary identifier, auto-generated |
| `consultation_id` | ObjectId | Yes | References `consultations._id` |
| `session_code` | String | Yes | Voicebot session identifier |
| `segment_id` | ObjectId | No | References the transcript segment that triggered extraction |
| `model` | String | Yes | Model that produced the extraction, e.g. `sarvam-105b` |
| `prompt_version` | String | Yes | Version of the field schema prompt; makes extraction quality measurable over time |
| `fields_returned` | Array of String | Yes | `field_code` values the model claimed to have found |
| `fields_accepted` | Array of String | Yes | Those that passed validation and were written |
| `fields_rejected` | Array of Objects | Yes | Rejections with a reason, e.g. `{ "field_code": "age", "reason": "OUT_OF_RANGE" }` |
| `latency_ms` | Number | No | Round-trip time of the extraction call |
| `transcript_tail` | String | No | The text sent to the model |
| `raw_response` | String | No | Unmodified model response, for offline evaluation |
| `is_active` | Boolean | Yes | Default true |
| `is_delete` | Boolean | Yes | Indicates soft deletion; default false |
| `created_at` | DateTime | Yes | Automatically generated |

This collection is what lets you answer "the model claims 3 fields, we accepted
1, and the other 2 were out of range" — the single most useful question when a
voice form silently under-fills.

### 3.4 Extraction Reviews Collection

Optional, but the reason `review_status` on the value row is worth having. Records
the human decision on low-confidence bot output, which is the training signal for
tightening `capture_prompt` and `numeric_range`.

| Field | Data Type | Required | Constraints |
|---|---|---|---|
| `_id` | ObjectId | Yes | Primary identifier, auto-generated |
| `consultation_id` | ObjectId | Yes | References `consultations._id` |
| `value_id` | ObjectId | Yes | References `consultation_field_values._id` |
| `event_id` | ObjectId | No | References `field_capture_events._id` |
| `decision` | String (Value Set) | Yes | `ACCEPT` \| `CORRECT` \| `REJECT` |
| `original_value` | Object | No | Snapshot of the bot's value, preserved after correction |
| `corrected_value` | Object | Conditional | The human's value; required when `decision` is `CORRECT` |
| `reason_code` | String | No | Why it was rejected or corrected, e.g. `MISHEARD`, `WRONG_SUBJECT`, `OUT_OF_SCOPE` |
| `notes` | String | No | Free-text comment |
| `reviewed_by` | ObjectId | Yes | References `users._id` |
| `reviewed_at` | DateTime | Yes | Automatically generated |

---

## 4. Field coverage

Every field from the brief, mapped to its `field_code`, `data_type`, `capture`, and
storage shape. This is the checklist the `form_fields` seed documents must satisfy.

### 4.1 Patient personal details — `PATIENT_DETAILS`

| Field | `field_code` | `data_type` | `capture` | Notes |
|---|---|---|---|---|
| Patient ID | `patient_id` | `AUTO_ID` | `SYSTEM` | Auto-generated; unique index on `consultations.patient_id` |
| Full name | `full_name` | `TEXT` | `VOICE` | — |
| Date of birth | `date_of_birth` | `DATE` | `VOICE` | Not in the future; feeds the `age` derivation |
| Age | `age` | `DERIVED` | `SYSTEM` | Derived from `date_of_birth` at `consultation_date`; **not stored** |
| Gender | `gender` | `ENUM` | `VOICE` | Value Set `GENDER` |
| Phone number | `phone_number` | `PHONE` | `VOICE` | E.164-ish; validated, not free text |
| Email address | `email_address` | `EMAIL` | `VOICE` | Format validated |
| Address | `address` | `TEXT` | `VOICE` | — |
| Occupation | `occupation` | `TEXT` | `VOICE` | — |
| Emergency contact name | `emergency_contact_name` | `TEXT` | `VOICE` | — |
| Emergency contact number | `emergency_contact_number` | `PHONE` | `VOICE` | — |

### 4.2 Medical history — `MEDICAL_HISTORY`

| Field | `field_code` | `data_type` | `capture` | Notes |
|---|---|---|---|---|
| Existing medical conditions | `existing_medical_conditions` | `MULTI_ENUM` + `LONG_TEXT` | `VOICE` | Two columns: `value_enum` against Value Set `CONDITION_TYPE`, plus `value_text` for the free-text remainder. Satisfies "Multi-select / text" without forcing one to hold the other |

### 4.3 Anthropometric — `ANTHROPOMETRIC`

| Field | `field_code` | `data_type` | `capture` | `numeric_range` |
|---|---|---|---|---|
| Height (cm) | `height_cm` | `NUMBER` | `VOICE` | 50–250 |
| Current weight (kg) | `current_weight_kg` | `NUMBER` | `VOICE` | 20–400 |
| Previous weight (kg) | `previous_weight_kg` | `NUMBER` | `VOICE` | 20–400 |
| Target weight (kg) | `target_weight_kg` | `NUMBER` | `VOICE` | 20–400 |
| BMI | `bmi` | `DERIVED` | `SYSTEM` | Computed; **not stored** |
| Weight change (kg) | `weight_change_kg` | `DERIVED` | `SYSTEM` | Computed; **not stored** |

### 4.4 Dietary habits and food preferences — `DIETARY_HABITS`

| Field | `field_code` | `data_type` | `capture` | Notes |
|---|---|---|---|---|
| Dietary preference | `dietary_preference` | `ENUM` | `VOICE` | Value Set `DIETARY_PREFERENCE` |
| Number of meals per day | `meals_per_day` | `NUMBER` | `VOICE` | 1–12 |
| Typical breakfast | `typical_breakfast` | `TEXT` | `VOICE` | — |
| Typical lunch | `typical_lunch` | `TEXT` | `VOICE` | — |
| Typical dinner | `typical_dinner` | `TEXT` | `VOICE` | — |
| Daily water intake (litres) | `daily_water_intake_litres` | `NUMBER` | `VOICE` | 0–15 |
| Food allergies or intolerances | `food_allergies` | `TEXT` | `VOICE` | — |
| Foods disliked or avoided | `foods_disliked` | `TEXT` | `VOICE` | — |
| Food restrictions | `food_restrictions` | `TEXT` | `VOICE` | — |
| Appetite | `appetite` | `ENUM` | `VOICE` | Value Set `APPETITE` |

### 4.5 Lifestyle and daily routine — `LIFESTYLE`

| Field | `field_code` | `data_type` | `capture` | Notes |
|---|---|---|---|---|
| Physical activity level | `physical_activity_level` | `ENUM` | `VOICE` | Value Set `PHYSICAL_ACTIVITY_LEVEL` |
| Type of exercise | `type_of_exercise` | `TEXT` | `VOICE` | — |
| Exercise frequency (days/week) | `exercise_frequency_days_per_week` | `NUMBER` | `VOICE` | 0–7 |
| Average sleep duration (hours) | `average_sleep_hours` | `NUMBER` | `VOICE` | 0–16 |
| Sleep quality | `sleep_quality` | `ENUM` | `VOICE` | Value Set `SLEEP_QUALITY` |
| Work schedule | `work_schedule` | `TEXT` | `VOICE` | — |
| Stress level | `stress_level` | `ENUM` | `VOICE` | Value Set `STRESS_LEVEL` |
| Smoking status | `smoking_status` | `ENUM` | `VOICE` | Value Set `YES_NO` |
| Alcohol consumption | `alcohol_consumption` | `ENUM` | `VOICE` | Value Set `YES_NO` |
| Alcohol frequency | `alcohol_frequency` | `ENUM` | `VOICE` | Value Set `ALCOHOL_FREQUENCY`; required when `alcohol_consumption` = `YES` |

The brief specifies alcohol as "Yes/No + frequency". That is one human answer with
two parts, so it is two columns plus a `required_when` link, rather than one
`"Yes, weekends"` string that cannot be filtered.

### 4.6 Nutritionist's assessment and diet plan — `ASSESSMENT`

All `HUMAN_ONLY`. These are excluded from the extraction prompt entirely.

| Field | `field_code` | `data_type` | `capture` | Notes |
|---|---|---|---|---|
| Nutrition assessment | `nutrition_assessment` | `LONG_TEXT` | `HUMAN_ONLY` | `captured_by` records the nutritionist |
| Recommended daily calorie intake | `recommended_daily_calorie_intake` | `NUMBER` | `HUMAN_ONLY` | 800–6000 kcal; unit `kcal` |
| Nutritionist recommendations | `nutritionist_recommendations` | `LONG_TEXT` | `HUMAN_ONLY` | — |

### 4.7 Appointment and follow-up — `APPOINTMENT`

| Field | `field_code` | `data_type` | `capture` | Notes |
|---|---|---|---|---|
| Consultation date | `consultation_date` | `DATE` | `SYSTEM` | Also on the `consultations` document; the field row is the fielded view |
| Follow-up date | `follow_up_date` | `DATE` | `HUMAN_ONLY` | Must be after `consultation_date` |

---

## 5. Relationships

```
form_templates (1) ────< form_fields (many)
      │  template_id
      │
      └────< consultations (many) >──── users (1)
                 │  consultation_id          nutritionist_id
                 │
                 └────< consultation_field_values (many) >──── form_fields (1)
                 │      field_id                        is_current = true
                 │
                 ├────< field_capture_events (many)     session_code
                 │
                 └────< extraction_reviews (many) >──── consultation_field_values (1)
```

Relationship rules:

- `consultations.template_id` must reference a `form_templates` document whose
  `status` is `PUBLISHED`. A consultation never points at a `DRAFT`.
- `consultation_field_values.field_id` must reference a `form_fields` document
  belonging to the consultation's own `template_id`. A value cannot be written
  against a field from another version of the form.
- Exactly one `is_current: true` row per (`consultation_id`, `field_code`).
  Enforced by a partial unique index (§7.3).
- A `supersedes_id` chain forms an ordered history. Its root has
  `supersedes_id: null`.
- `form_templates.version` is never edited after a consultation references it.
  Changes mean a new version, so a historical consultation always renders with
  the field set it was captured against.

---

## 6. Rules the application layer must enforce

The database cannot express these as constraints, so they are stated explicitly.

**6.1 A `HUMAN_ONLY` field can never receive a `VOICE_BOT` value.**
On write: if the field's `capture` is `HUMAN_ONLY` and `source` is `VOICE_BOT`,
reject the write and record the attempt in `field_capture_events.fields_rejected`
with reason `HUMAN_ONLY_FIELD`. This is the guard that stops a hallucinated
clinical assessment. It is also why such fields are omitted from the prompt.

**6.2 A `HUMAN_ONLY` or `ADMIN` value is never superseded by a `VOICE_BOT` value.**
Before inserting a new current row, if the existing current row's `source` is
`NUTRITIONIST` or `ADMIN` and the incoming `source` is `VOICE_BOT`, reject it.
The bot may suggest; the human decides.

**6.3 Measurements must be numeric and in range.**
`value_text` like `"72.5 kg"` is rejected in favour of `value_num: 72.5`,
`unit: "kg"`. Out-of-range values are rejected with reason `OUT_OF_RANGE` — this
catches both unit confusion (metres read as cm) and genuine extraction errors.

**6.4 Nothing is inferred when it was not said.**
If the model returns a field, it must be accompanied by a `raw_utterance`. A value
without a supporting utterance is rejected. Combined with §1.5, this keeps the
record honest: every number traces to something a person actually said.

**6.5 Derived fields are never written.**
Writes to a `form_fields` document with `is_derived: true` are rejected. They are
computed on read from the current values.

**6.6 `is_current` transitions are append-only.**
Corrections insert a new row and supersede the old one. History is never rewritten
— the original bot value must stay auditable after a correction.

---

## 7. Indexes

**7.1 `consultations`**

| Index | Type | Purpose |
|---|---|---|
| `{ patient_id: 1 }` | Unique | Patient identity lookup |
| `{ consultation_code: 1 }` | Unique | Human-readable lookup |
| `{ consultation_date: -1 }` | Compound | Default listing, newest first |
| `{ user_id: 1, consultation_date: -1 }` | Compound | A user's consultation history |
| `{ status: 1, consultation_date: -1 }` | Compound | Review queue |
| `{ session_code: 1 }` | Non-unique | Trace a consultation back to its transcript |
| `{ is_delete: 1, consultation_date: -1 }` | Compound | Soft-delete filtering |

**7.2 `consultation_field_values`**

| Index | Type | Purpose |
|---|---|---|
| `{ consultation_id: 1, field_code: 1, is_current: 1 }` | Compound | Render one consultation |
| `{ field_id: 1, is_current: 1 }` | Compound | Analytics across consultations |
| `{ consultation_id: 1, section_code: 1, is_current: 1 }` | Compound | Per-section progress |
| `{ value_text: "text" }` | Text | Free-text search |
| `{ value_num: 1, field_id: 1, is_current: 1 }` | Compound | Range queries, e.g. "underweight patients" |
| `{ review_status: 1, confidence: 1 }` | Compound | Low-confidence review queue |
| `{ created_at: -1 }` | Single | Recency |

**7.3 The one-current-value-per-field constraint**

```javascript
db.consultation_field_values.createIndex(
  { consultation_id: 1, field_code: 1 },
  { unique: true,
    partialFilterExpression: { is_current: true } }
)
```

This is the only way to make "exactly one current value" a database guarantee
rather than an application convention. It also makes concurrent extraction safe:
when a late transcript segment and a nutritionist edit race, one insert wins and
the other is rejected, instead of silently producing two current rows.

**7.4 `field_capture_events`**

| Index | Type | Purpose |
|---|---|---|
| `{ consultation_id: 1, created_at: 1 }` | Compound | Per-consultation extraction log |
| `{ session_code: 1, created_at: 1 }` | Compound | Per-session diagnostics |
| `{ prompt_version: 1, created_at: -1 }` | Compound | Extraction quality over time |
| `{ created_at: -1 }` | Single | Recency, TTL candidate |

**7.5 TTL.** If transcript retention is agreed, add
`{ created_at: 1 }` with `expireAfterSeconds` on `field_capture_events` and
`extraction_reviews`. These are diagnostic records; the clinical value is in
`consultations` and `consultation_field_values`, which should not expire.

---

## 8. Worked example

A consultation captured from a 14-minute voice session. The bot heard a weight and
a preference; the nutritionist supplied the clinical judgement.

```json
// consultation_field_values — the bot's capture
{
  "_id": ObjectId("68cbf0200000000000000001"),
  "consultation_id": ObjectId("68cbf0100000000000000001"),
  "field_id": ObjectId("68cbf0020000000000000003"),
  "field_code": "current_weight_kg",
  "section_code": "ANTHROPOMETRIC",
  "value_num": 68.4,
  "unit": "kg",
  "raw_utterance": "I currently weigh about sixty eight kilos",
  "confidence": 0.91,
  "source": "VOICE_BOT",
  "transcript_start_ms": 412000,
  "transcript_end_ms": 412410,
  "supersedes_id": null,
  "is_current": true,
  "review_status": "PENDING",
  "created_at": ISODate("2026-09-28T15:49:02Z")
}

// consultation_field_values — nutritionist correction, original preserved
{
  "_id": ObjectId("68cbf0200000000000000002"),
  "consultation_id": ObjectId("68cbf0100000000000000001"),
  "field_id": ObjectId("68cbf0020000000000000003"),
  "field_code": "current_weight_kg",
  "section_code": "ANTHROPOMETRIC",
  "value_num": 68.0,
  "unit": "kg",
  "source": "NUTRITIONIST",
  "captured_by": ObjectId("68cbe0000000000000000003"),
  "supersedes_id": ObjectId("68cbf0200000000000000001"),
  "is_current": true,
  "review_status": "ACCEPTED",
  "reviewed_by": ObjectId("68cbe0000000000000000003"),
  "reviewed_at": ISODate("2026-09-28T16:04:00Z"),
  "created_at": ISODate("2026-09-28T16:04:00Z")
}

// consultation_field_values — clinical judgement, never offered to the bot
{
  "_id": ObjectId("68cbf0200000000000000003"),
  "consultation_id": ObjectId("68cbf0100000000000000001"),
  "field_id": ObjectId("68cbf0020000000000000011"),
  "field_code": "nutrition_assessment",
  "section_code": "ASSESSMENT",
  "value_text": "Mildly overweight for height. Diet is largely vegetarian with low protein. Reported low appetite and occasional fatigue; recommend a protein-focused plan with one mid-morning snack.",
  "source": "NUTRITIONIST",
  "captured_by": ObjectId("68cbe0000000000000000003"),
  "supersedes_id": null,
  "is_current": true,
  "review_status": "ACCEPTED",
  "created_at": ISODate("2026-09-28T16:06:30Z")
}

// field_capture_events — what the model actually returned
{
  "_id": ObjectId("68cbf0300000000000000001"),
  "consultation_id": ObjectId("68cbf0100000000000000001"),
  "session_code": "20260928_154205",
  "model": "sarvam-105b",
  "prompt_version": "nutrition-v1",
  "fields_returned": ["current_weight_kg", "dietary_preference", "age"],
  "fields_accepted": ["current_weight_kg", "dietary_preference"],
  "fields_rejected": [
    { "field_code": "age", "reason": "DERIVED_FIELD" },
    { "field_code": "nutrition_assessment", "reason": "HUMAN_ONLY_FIELD" }
  ],
  "latency_ms": 1840,
  "transcript_tail": "I currently weigh about sixty eight kilos, and I'm vegetarian...",
  "is_active": true,
  "is_delete": false,
  "created_at": ISODate("2026-09-28T15:49:01Z")
}
```

The rendered consultation, with derived values computed on read:

```
Patient ID            NUT-20260928-0007
Full name             Priya Sharma
Age                   34                      (derived from date_of_birth)
Gender                FEMALE
Height                162 cm
Current weight        68.0 kg                 (nutritionist-corrected)
BMI                   25.8                    (derived)
Recommended intake    1800 kcal               (HUMAN_ONLY)
```

---

## 9. Open questions for review

1. **Retention of `raw_utterance` and `transcript_tail`.** These contain verbatim
   patient speech. Health data has residency and consent implications that plain
   session logs do not. Confirm whether transcript fragments may be stored at all,
   and for how long.
2. **Does a consultation belong to a `users` record?** A patient may have no
   account, so `user_id` is conditional and `patient_id` is the real key. Confirm
   the nutritionist role owns the account, not the patient.
3. **Multi-select + text fields.** `existing_medical_conditions` is modelled as one
   `MULTI_ENUM` plus free text. Confirm the UI presents a picker *and* a text box
   for that field rather than a single control.
4. **Consent capture.** `Database Design.docx` records the bot joining meetings
   and recording. A health context likely needs an explicit per-consultation
   consent record, which this schema does not yet include.
