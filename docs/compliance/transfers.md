# International transfer register & TIAs

UK GDPR Chapter V (Articles 44–49) governs transfer of personal data
outside the UK. The controller must demonstrate a lawful basis for
each transfer plus an appropriate transfer mechanism plus, where
required, a Transfer Impact Assessment (TIA).

This document records every active third-country transfer in the QMS.

---

## TIA framework adopted

The controller follows the EDPB "Recommendations 01/2020" + ICO
"International transfers" guidance:

1. **Know the transfer** — what data, what recipient, what country.
2. **Identify the transfer tool** — adequacy / IDTA / SCCs+UK Addendum
   / BCRs / derogations.
3. **Assess effectiveness** in the destination country (state surveillance
   law, judicial redress, processor track record).
4. **Adopt supplementary measures** if the assessment shows residual
   risk (encryption, pseudonymisation, contractual auditing, etc).
5. **Procedure for documentation and review** — this register, plus an
   annual re-assessment.

---

## Active transfers

### Transfer 1 — ClickSend (Australia)

#### 1.1. Know the transfer

| Field | Value |
|---|---|
| Data exporter | The controller (UK-established food production business) |
| Data importer | ClickSend Pty Ltd, Sydney + Melbourne, AU |
| Categories of subject | Trainees (factory floor workers) |
| Categories of data | E.164 phone number, SMS message body |
| Volume / frequency | One SMS per training enrolment + recurrence (~once per worker per year, plus event-driven incident retraining) |
| Purpose | Compliance training delivery |
| Onward transfers | None — ClickSend uses local Australian carriers; no further third-country onward transfer to data importer's knowledge |

#### 1.2. Transfer tool

**ICO-approved International Data Transfer Agreement (IDTA)** signed
between the controller and ClickSend Pty Ltd, in force as of the
controller's procurement.

(Alternative path: the EU SCCs (Module Two — controller to
processor, 2021/914/EC) + the UK International Data Transfer Addendum
B.1.0. Either is acceptable post-2022.)

#### 1.3. Effectiveness assessment

- **Country adequacy status:** Australia does not have a UK adequacy
  decision. Schrems II-style state-surveillance assessment must
  therefore be performed.
- **Australian state-surveillance landscape:** the Australian
  Telecommunications (Interception and Access) Act 1979 and the
  Assistance and Access Act 2018 ("encryption laws") allow access to
  communications for law-enforcement purposes. The Office of the
  Australian Information Commissioner provides judicial redress. The
  ICO has not flagged Australian transfers as unsuitable for SCCs +
  supplementary measures.
- **Practical risk:** the only data crossing the border is a phone
  number and an SMS body containing a magic-link URL. The token
  itself is HMAC-bound to a specific enrolment and expires within
  7 days. State access to in-flight SMS would yield a phone number +
  a short-lived URL with no persistent value.
- **Conclusion:** residual risk Low. Tool effective.

#### 1.4. Supplementary measures

- **Data minimisation:** only phone + SMS body crosses; no name, role,
  line, or inferred PII.
- **Token expiry:** magic links are 7-day-default-TTL and bound to a
  specific enrolment id; an intercepted SMS yields a short-lived
  artefact, not a long-term identifier.
- **TLS in transit:** controller→ClickSend API uses TLS 1.2+; ClickSend
  →carrier is TLS where the carrier supports it.
- **Vendor security**: ClickSend is ISO 27001 certified.

#### 1.5. Documentation & review

- Re-assessed annually; flagged for immediate re-review on:
  - Any change in Australian surveillance law (track via UK
    government / ICO publications).
  - Any change in ClickSend ownership or DPA.
  - Any reported data breach affecting ClickSend.

---

## Pending / inactive transfers

The QMS does not currently make any *other* third-country transfer.
Adding any would require a new entry above before rollout, plus a
privacy-notice update.

Any change in the SMTP relay configuration that points at a
non-UK / non-EEA endpoint (e.g. a US Mailgun region) requires the same
transfer-tool + TIA flow above.

---

## Change log

| Date | Change | Author |
|---|---|---|
| 2026-04-30 | Initial register; ClickSend TIA shipped as Transfer 1. | QMS engineering team |
