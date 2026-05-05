# Lawful basis register — UK GDPR Article 6

This document names the lawful basis under UK GDPR Art. 6 (and where
relevant Art. 9 / Art. 10) for every category of personal data
processed by the QMS. It is the controller's record under Art. 30 and
should be cited from the public privacy notice (`/privacy`).

**Controller:** the operator of this QMS deployment (UK food production
business — typically the bakery's parent company). The deployer is the
data controller for all personal data below; this document is a
template each deployer must adopt and amend with their entity name.

---

## Datasets and bases

### 1. User accounts (`users`)

- **Data:** email, full name, language preference, password hash,
  TOTP secret (encrypted), failed-login counter, last-login timestamp.
- **Subjects:** factory staff with QMS accounts (operators, QA,
  line managers, compliance, plant managers, admins).
- **Lawful basis:** **Art. 6(1)(b) — Performance of contract** (the
  employment contract). User accounts are necessary to perform the
  employee's role within the food-production employer. Authentication
  data (password hash, TOTP secret) additionally rests on
  **Art. 6(1)(f) — Legitimate interests** of the controller in
  protecting confidentiality of regulated production data.
- **Special category?** No.
- **Retention:** for the duration of employment plus the audit retention
  window (see §Retention below).

### 2. Trainees (`trainees`)

- **Data:** mobile phone number (PII), full name, role code, line
  assignment, language preference.
- **Subjects:** factory floor workers who do not otherwise log into the
  QMS (operators, contractors).
- **Lawful basis:** **Art. 6(1)(c) — Legal obligation.** Specifically:
  - **Regulation (EC) No 852/2004 Art. 4(2)** and Annex II Chapter XII
    paragraph 1 require food businesses to ensure food handlers are
    "supervised and instructed and/or trained in food hygiene matters
    commensurate with their work activity."
  - **The Food Safety and Hygiene (England) Regulations 2013** /
    **Food Hygiene (Wales/Scotland/NI) Regulations** locally enforce 852/2004
    and grant the FSA / local authorities inspection powers including
    sight of training records.
  - **SALSA scheme rules** and **BRC Global Standard for Food Safety**
    (the retailer-audit standards most UK bakeries operate under)
    additionally require auditable per-worker training records.
- **Phone number specifically** is the identifier we use to deliver
  training via SMS magic link; less invasive than holding a personal
  email or maintaining a per-worker QMS account.
- **Special category?** No.
- **Retention:** while the trainee is active on payroll plus the audit
  retention window. Trainees who have not been active for 24 months and
  have no live certifications are eligible for redaction (see
  `flask gdpr-redact`).

### 3. Training declarations and signatures (`training_declarations`)

- **Data:** typed name, drawn signature (PNG), declaration consent text
  snapshot, IP address, user agent, timestamp.
- **Subjects:** trainees who completed an exam attempt that passed.
- **Lawful basis:** **Art. 6(1)(c) — Legal obligation** (same FSA /
  retailer audit grounds as §2). The signature element is the documented
  evidence the auditor will request to verify the worker personally
  attested to understanding the training, and that the legal entity
  has discharged its 852/2004 Art. 4(2) duty.
- **Special category?** A handwritten signature may be **biometric data**
  in the broad sense, but UK GDPR Art. 9(1) only catches biometric data
  when "processed for the purpose of uniquely identifying a natural
  person." We do **not** use signatures for identification — the
  identity is already established by the SMS-bound enrolment token and
  the typed name. Signatures here are evidence of attestation, not
  biometrics. Document this analysis in the per-deployment DPIA.
- **IP address + User-Agent** are processed under
  **Art. 6(1)(f) — Legitimate interests** in fraud / impersonation
  detection (matching the recorded IP against expected geographies for
  retailer audits).
- **Retention:** 7 years from the issued certification's
  `valid_until` — matches FSA / retailer audit lookback.

### 4. Audit log (`audit_log`)

- **Data:** user_id (or null), entity type/id, action, before/after
  diff (JSON), IP, user agent, occurred_at, chain checksum.
- **Subjects:** every authenticated user; trainees indirectly (their
  enrolment events).
- **Lawful basis:** **Art. 6(1)(c) — Legal obligation** (HACCP / food
  traceability evidence under EC 178/2002 Art. 18 + EC 852/2004 Annex II)
  combined with **Art. 6(1)(f) — Legitimate interests** of the controller
  in detecting and investigating security or compliance incidents.
- **Special category?** No.
- **Erasure carve-out:** UK GDPR Art. 17(3)(b) and (e) permit retention
  for compliance with a legal obligation and for the establishment,
  exercise, or defence of legal claims. The audit chain itself is
  therefore retained, but the JSON `diff` payload may be redacted to
  remove personal-data fields at the data subject's request — see
  `flask gdpr-redact` and §Erasure procedure.
- **Retention:** 7 years (HACCP / FSA inspection horizon).

### 5. CCP measurements, SALSA responses, tickets

- **Data:** measurement values, status transitions, comments.
- **Subjects:** indirectly identifies the operator who took the action
  via the linked `user_id` foreign key.
- **Lawful basis:** **Art. 6(1)(c) — Legal obligation** (HACCP / food
  safety evidence) and **Art. 6(1)(f) — Legitimate interests**
  (production tracking).
- **Retention:** 7 years (HACCP / FSA).

### 6. SMS / email transmission metadata

- **Data:** recipient phone or email, message body, delivery status,
  timestamps; held by the third-party transmission processor (ClickSend
  for SMS; the configured SMTP relay for email).
- **Lawful basis:** **Art. 6(1)(c)** for training-related SMS (legal
  obligation for staff training delivery); **Art. 6(1)(f)** for
  operational notifications.
- **International transfer:** ClickSend is an Australian processor; an
  ICO-approved International Data Transfer Agreement (IDTA) or UK
  Addendum to the EU SCCs is required. See
  `docs/compliance/subprocessors.md`.
- **Retention:** at the processor's documented retention; the QMS
  retains only the local job record (one row in the RQ queue) which
  is purged on success (or held in DLQ for ops review).

---

## Categories NOT processed

To narrow the DPIA scope, the QMS does **not** process:

- Special-category data under UK GDPR Art. 9 (race, religion, health,
  trade union, political opinions, sex life, genetic, identifying
  biometric — see §3 for the signature analysis).
- Criminal-conviction data under UK GDPR Art. 10.
- Data about children under 18. UK food production employs only
  workers aged 16+ under Health and Safety at Work etc. Act 1974 +
  the Children (Protection at Work) Regulations; the QMS holds no
  date-of-birth field that could relate to a child subject.
- Marketing / advertising profiles. No analytics or tracking pixels.

---

## Retention

| Dataset | Retention |
|---|---|
| `users` (active) | duration of employment + 7 years |
| `trainees` (inactive ≥24 months, no live cert) | redactable on schedule |
| `training_declarations` | 7 years from cert `valid_until` |
| `training_certifications` | 7 years from `valid_until` |
| `audit_log` chain rows | 7 years; chain row stays, `diff` redactable |
| `measurements`, `tickets`, `salsa_responses` | 7 years |
| SMS / email job rows | until success or DLQ disposal |

The 7-year horizon is set to match the longest-lookback retailer audit
(BRC) and FSA inspection horizon. Where local regulation requires
longer (e.g. specific allergen-management evidence under Natasha's Law),
the operator extends accordingly.

Retention is enforced by the operator via scheduled
`flask retention-sweep` (planned — see W-GDPR-2 in the audit backlog).

---

## Erasure procedure

For `audit_log` rows the chain hash is preserved, but the `diff` JSON
is rewritten in-place to `{"redacted": true, "redacted_at": "..."}`.
This leaves the chain mathematically verifiable while removing the
personal-data payload. For all other tables the row is deleted via
`flask gdpr-redact` or by the operator running parameterised SQL,
which is itself audited.

---

## Subject rights mapping

| Right | UK GDPR Art. | Mechanism |
|---|---|---|
| Information | 13 / 14 | `/privacy` notice rendered at login + before SMS-magic-link click |
| Access (DSAR) | 15 | `flask dsar-export --subject ...` (planned — K-GDPR-2) |
| Rectification | 16 | Admin UI (`/admin/users/<id>`, `/admin/training/trainees/<id>`); subject contacts the controller |
| Erasure | 17 | `flask gdpr-redact --subject ...` (planned — K-GDPR-3) |
| Restriction | 18 | Set `is_active = False`; account-level mute |
| Portability | 20 | DSAR export emits machine-readable JSON |
| Object | 21 | N/A — no Art. 6(1)(e)/(f) processing where opt-out applies |
| Automated decisions | 22 | N/A — no solely-automated decisions made about subjects |

---

## Versioning

This document is the controller's evidence under Art. 30. Update it
whenever a new dataset or processor is added. Reviewed at least
annually as part of the deployer's compliance cycle.
