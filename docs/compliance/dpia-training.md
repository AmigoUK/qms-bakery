# Data Protection Impact Assessment — Worker training & certification

**Status:** Template DPIA. The deployer (controller) must adopt this as
their own, fill in the entity-specific blanks (organisation name, DPO,
deployment-specific risk treatments) and run it past the controller's
DPO / legal counsel before live trainee enrolment.

**Reviewed:** 2026-04-30 · **Next review:** 2027-04-30

---

## 1. Why a DPIA is required

UK GDPR Art. 35(1) and the ICO's "When do we need to do a DPIA?" guide
trigger a DPIA where processing is likely to result in a high risk to
the rights and freedoms of natural persons. The training feature
combines several DPIA-mandatory factors:

- **Systematic monitoring of employees** — the dashboard renders a
  per-trainee × per-course matrix of pass/fail/cert status (employee
  monitoring → ICO list).
- **Biometric-adjacent data** — handwritten signatures captured on a
  drawing canvas. Signatures are stored as PNG; while not used for
  uniquely identifying the subject (so not "biometric data" under UK
  GDPR Art. 9(1)) the ICO treats handwritten signatures as a
  borderline case worth a DPIA.
- **Innovative use of an established technology** — SMS magic-link
  authentication for compliance-evidence collection is non-trivial
  and warrants documented risk treatment.
- **Vulnerable data subjects** — agency / temporary food-production
  workers who cannot easily refuse to participate without consequence
  for their employment.

This DPIA evaluates whether the processing is necessary and
proportionate, what risks remain, and how the controller mitigates them.

---

## 2. The processing — what, who, why, how

### What is processed
- Trainee phone number (PII)
- Trainee full name (PII)
- Role code, line assignment, language preference
- SMS magic-link tokens (HMAC-SHA256-signed, 7-day default lifetime)
- Exam answer set (per-question selected option ids)
- Score, pass/fail, timestamps
- Declaration: typed name, drawn signature (PNG), declaration consent
  text snapshot, IP address, user agent
- Certification: issuance date, validity_until, version/course pinned,
  revocation timestamp (if any)

### Who is processed
- Floor / production workers in a UK food production environment
  (operators, contractors).
- Workers are likely 16+ (UK Children (Protection at Work) Regulations);
  the system holds no DOB and processes no children's data by design.

### Why
- **Statutory duty**: Regulation (EC) No 852/2004 Art. 4(2) and Annex II
  Chapter XII paragraph 1 require food handlers to be supervised /
  instructed / trained "commensurate with their work activity";
  inspections by the FSA / local authority require evidence of this
  duty being met.
- **Retailer audit**: SALSA / BRC / customer-supplier contracts require
  per-worker training records.
- **Risk control**: poor training on critical control points (e.g.
  chilled-chain temperature tolerances) is a leading contributor to
  food-safety incidents.

### How
1. Operator-side admin (compliance officer / line manager) creates a
   `Trainee` row keyed by phone number.
2. The recurrence scheduler or a HACCP/SALSA trigger creates a
   `TrainingEnrolment`, signs an HMAC magic-link token, queues an SMS
   via the ClickSend processor.
3. The trainee receives the SMS, taps the link, lands on a public
   `/training/take/<token>` route — no password, identity bound to
   the token + the phone number that received it.
4. They read modules, take an exam, sign a declaration (typed name +
   canvas signature). The page POSTs to the application; signature is
   base64-encoded to PNG and stored as a `LargeBinary` blob.
5. Pass → `TrainingCertification` row issued.
6. Audit chain captures every state change with actor, time, IP, UA.

---

## 3. Necessity & proportionality

| Test | Assessment |
|---|---|
| Lawful basis | Art. 6(1)(c) — legal obligation. Documented in `docs/compliance/lawful-basis.md`. |
| Specified, explicit purpose | Compliance with FSA / EC 852/2004 Art. 4(2) and retailer-audit obligations. |
| Adequate / relevant / limited | Phone is the minimum identifier necessary to deliver SMS-based training. Signature + typed name is the minimum evidence the auditor will accept. |
| Accuracy | Trainees can request rectification via line manager; admin UI exists. |
| Storage limitation | 7 years from cert valid_until (matches FSA / BRC lookback). Enforced by `flask retention-sweep`. |
| Integrity / confidentiality | Bcrypt passwords (where applicable), Fernet-encrypted TOTP secrets, HMAC magic-link tokens, hash-chained audit log, strict CSP, HSTS, encrypted in transit (HTTPS), encrypted at rest at the disk level. |
| Lawful basis to share | ClickSend (Art. 28 processor); FSA / SALSA / BRC inspectors (legal obligation); no other recipients. |
| Subject information | `/privacy` notice rendered at login + training landing + declaration. |
| Subject rights | DSAR via `flask dsar-export`; redaction via `flask gdpr-redact`; rectification via admin UI. |

### Less-intrusive alternatives considered

- **Paper-based training records.** Rejected: paper is not realistic at
  scale, BRC v9 explicitly favours digital evidence with tamper-resistant
  audit trails, and paper signatures are themselves a biometric-adjacent
  PII record stored insecurely.
- **Email instead of SMS.** Rejected: many floor workers do not have a
  reliable work email; SMS is the channel they actually check.
- **In-person training only.** Rejected: would not solve the
  per-worker auditable record requirement; still need an attestation.
- **No phone number — full-name only.** Rejected: a name is not unique
  enough for an inspector's record reconciliation; phone is the most
  minimal stable identifier for an irregular workforce.
- **Photo-of-signature on personal phone, sent as MMS.** Rejected:
  introduces a server-side image-handling attack surface and leaks
  signature outside the QMS storage perimeter.

---

## 4. Risks identified and treatments

### R1. Magic-link interception or sharing
**Risk:** A worker forwards the SMS to a colleague who completes the
training on their behalf, or the SMS is intercepted at the operator
layer (eSIM swap, lost handset).
**Likelihood:** Low–Medium (single-shot 7-day TTL token).
**Severity:** Low — an inauthentic completion mostly harms the
controller's own audit defence rather than the data subject.
**Treatment:**
- 7-day default TTL bound to a single enrolment id.
- Token is HMAC-SHA256-bound to enrolment_id+expiry; not guessable.
- Declaration captures typed_name + signature + IP + User-Agent so
  fraud is forensically detectable post-hoc.
- Documented in the controller's training-fraud policy that any
  certified worker who later fails an in-person spot-check is
  re-enrolled and the original cert revoked.

### R2. Signature blob exfiltration
**Risk:** The signature PNG is a personal artefact a subject could
object to seeing exfiltrated.
**Likelihood:** Low (DB-level access required; no public route returns
the blob).
**Severity:** Medium — signatures are personally distinctive.
**Treatment:**
- Stored in `LargeBinary` (Postgres TOAST); not exposed via any
  authenticated route except the trainee's own certificate PDF.
- The controller's deployment runs Postgres on encrypted-at-rest disk.
- Right-to-erasure path nulls the column (`flask gdpr-redact`).
- 600 × 200 canvas → ~30KB; not large enough to embed steganographic
  channels in practice.

### R3. Training-scheduler clock skew → duplicate SMS
**Risk:** A scheduler restart re-issues SMS for already-enrolled
trainees, increasing the volume of phone-number processing without
necessity.
**Likelihood:** Medium (operational events).
**Severity:** Low.
**Treatment:**
- `has_open_enrolment()` idempotency check before issuing.
- Test coverage in `tests/test_training_scheduler.py`.

### R4. Cross-border transfer to ClickSend (AU)
**Risk:** Transfer of phone number + SMS body to an Australian
processor.
**Likelihood:** Certain (every SMS).
**Severity:** Medium — third-country transfer requires a transfer
mechanism.
**Treatment:**
- ICO-approved International Data Transfer Agreement (IDTA) or UK
  Addendum to EU SCCs in place between controller and ClickSend.
- Transfer Impact Assessment (TIA) documented in
  `docs/compliance/transfers.md`.
- Phone is the only data shared; the message body itself contains a
  link only (no name, no role, no inferred PII).
- Controller documents in their procurement policy a re-evaluation
  if Australia's adequacy status changes.

### R5. Retention drift
**Risk:** Without enforcement, signatures and PII outlive their
purpose and accumulate.
**Likelihood:** Certain over time.
**Severity:** Medium — accumulating PII is the most common compliance
finding under UK GDPR Art. 5(1)(e).
**Treatment:**
- Documented retention schedule in `docs/compliance/lawful-basis.md`.
- `flask retention-sweep` runs on a cron; logs each redaction.

### R6. Audit chain rewrite
**Risk:** A controller-side actor with DB write access could rewrite
audit history to cover up a training shortfall.
**Likelihood:** Low (requires DB-admin compromise).
**Severity:** High — would invalidate compliance evidence.
**Treatment:**
- Hash-chained `audit_log`; `verify_chain()` runs in CI tests.
- Postgres BEFORE UPDATE/DELETE trigger blocks in-place rewrites at
  the DB level (`docs/security/owasp-self-audit.md` and migration).
- `gdpr-redact` is the *only* sanctioned mutation; it appends a
  redaction marker rather than deleting and emits its own audit row.
- Controller's external pen-test brief flags audit-chain tampering
  as an explicit test target.

### R7. Service worker poisoning
**Risk:** A poisoned cache could serve a manipulated version of the
training UI to a trainee on next launch.
**Likelihood:** Low (strict CSP, same-origin only, no third-party
scripts, vendored signature pad library).
**Severity:** Medium.
**Treatment:**
- CSP `'self'` only, no inline.
- Service worker scope limited to GET responses; mutations bypass.
- External pen-test target.

---

## 5. DPO / external consultation

The controller's DPO (or the senior compliance officer, where no DPO
is statutorily required) reviews this DPIA before live rollout, and at
least annually.

External consultation with the ICO is **not** triggered: residual risk
after treatments above is assessed Low–Medium and the processing has
specific legal-obligation grounding under EC 852/2004.

---

## 6. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Data Protection Officer (or proxy) | _to be filled_ | _date_ | |
| IT / engineering lead | _to be filled_ | _date_ | |
| Operations / compliance lead | _to be filled_ | _date_ | |

---

## 7. Change log

| Date | Change | Author |
|---|---|---|
| 2026-04-30 | Initial DPIA template shipped with the QMS. | QMS engineering team |
