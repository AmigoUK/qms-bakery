# Sub-processor register

UK GDPR Art. 13(1)(e) requires the controller to disclose recipients
or categories of recipients of personal data. UK GDPR Art. 28 requires
a written contract (DPA) with every sub-processor. This document is
the controller's record of which third parties may receive personal
data via the QMS.

The deployer (controller) maintains and publishes this register, and
links to it from `/privacy`. Update before adding any new external
service.

---

## Active sub-processors

### 1. ClickSend Pty Ltd

- **Role:** SMS delivery for training magic links and operational
  notifications.
- **Data shared:** trainee phone number (E.164), SMS message body
  (course title + magic-link URL).
- **Location of processing:** Australia (Sydney, Melbourne).
- **Transfer mechanism:** ICO-approved International Data Transfer
  Agreement (IDTA) or UK Addendum to the EU SCCs — see
  `docs/compliance/transfers.md`.
- **DPA:** ClickSend's standard "Data Processing Addendum" must be
  countersigned by the controller as part of the procurement process.
  Available at https://www.clicksend.com/legal — the controller keeps
  a signed copy on file.
- **Retention at processor:** ClickSend retains delivery logs for 12
  months; message bodies for ~30 days for delivery troubleshooting.
- **Security:** TLS 1.2+ in transit, encryption at rest, ISO 27001
  certified.
- **Configurable in QMS via:** `CLICKSEND_USERNAME`, `CLICKSEND_API_KEY`,
  `CLICKSEND_SOURCE`, `CLICKSEND_BASE_URL`.
- **Replacement plan:** ClickSend can be swapped for any SMS API by
  re-implementing `app/jobs/sms.py::deliver_sms`. The phone-number
  data flow stays identical.

### 2. SMTP relay

- **Role:** Outbound email for ticket assignment / system
  notifications.
- **Data shared:** recipient email address, subject, message body
  (which may reference user names or ticket details).
- **Location of processing:** depends on the deployer's configured
  relay. The controller documents the chosen relay here and the
  transfer mechanism if it sits outside the UK / EEA.
- **DPA:** required from the relay vendor (e.g. Mailgun, SendGrid,
  Microsoft 365, Google Workspace, on-prem Postfix).
- **Retention at processor:** vendor-dependent; document it.
- **Configurable in QMS via:** `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
  `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_FROM`.

### 3. Self-hosted infrastructure (controller-operated)

- **PostgreSQL** — primary data store. Hosted by the controller on
  controller-owned or controller-managed infrastructure (e.g. a
  hetzner / OVH / on-prem VM); not a third party for GDPR purposes.
- **Redis** — RQ job queue + rate-limit buckets + MQTT stream. Same
  hosting as Postgres.
- **Mosquitto MQTT broker** — sensor ingestion. Same hosting.

These are controller-operated and therefore not sub-processors. If
the controller chooses to use a managed service (RDS, Aiven, Upstash)
that *does* become a sub-processor and must be added here.

---

## Inactive / not-in-scope

The following appear in the QMS but are NOT sub-processors:

- **GitHub** — source-code hosting, not a runtime data path. Touches
  no production personal data.
- **PyPI / Docker Hub** — package and image distribution; build-time
  only.
- **Information Commissioner's Office (ICO)** — regulator, not a
  processor.
- **FSA / local authorities / SALSA / BRC inspectors** — recipients
  under legal obligation, not sub-processors.

---

## Adding a new sub-processor

1. Engineering opens an issue describing the data flow.
2. Compliance verifies a UK-GDPR-conformant DPA is in place.
3. If the processor is outside the UK / EEA, a TIA is added to
   `docs/compliance/transfers.md`.
4. Privacy notice (`/privacy`) is updated.
5. This register is updated.
6. Operational change is rolled out only after the four prior steps.

---

## Change log

| Date | Change | Author |
|---|---|---|
| 2026-04-30 | Initial register. | QMS engineering team |
