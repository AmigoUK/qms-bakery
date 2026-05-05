# Help & documentation system

The canonical user-facing help content is **served live by the app** at `/help/*` so it stays bilingual (PL/EN) and in sync with the running translations. This file is the developer's pointer to where each piece lives.

## Live routes (public, no auth required)

| Route | Audience | Source template |
|---|---|---|
| `/help` | Everyone — routing hub | `app/templates/help/index.html` |
| `/help/training` | Trainees / line workers | `app/templates/help/training.html` |
| `/help/admin` | Compliance officer, plant manager | `app/templates/help/admin.html` |
| `/help/auditor` | External SALSA / BRC auditor | `app/templates/help/auditor.html` |
| `/help/ops` | Devops / sysadmin | `app/templates/help/ops.html` |
| `/help/trainee-card.pdf` | Printable A4 quick-reference card (PL) | `app/templates/reports/trainee_quick_card.html` (WeasyPrint) |

All routes are registered without authentication — they contain no PII or secrets. The TOTP enforcement gate skips them via the allowlist in `app/__init__.py::_TOTP_GATE_ALLOWLIST`.

## Editing help content

Each `app/templates/help/*.html` is a Jinja template that branches on `current_lang` (PL or EN). Add, edit, or remove sections directly. Section headings inside the template are plain HTML; the page title and inter-page navigation use translation keys (`help.training.title`, `help.back_to_index`, etc).

After editing a template, **restart the Flask app** — Jinja caches in memory in non-debug mode (this is documented in CLAUDE.md memory, not specific to help pages).

## Editing the trainee quick-card PDF

`app/templates/reports/trainee_quick_card.html` is a print-shaped HTML rendered via WeasyPrint. A4 portrait, single page. CSS in `<style>` tag inside the template (no shared stylesheet — print rendering is sandboxed).

Test the rendering locally:
```bash
curl -o /tmp/card.pdf http://localhost:5000/help/trainee-card.pdf
```

## Adding new translation keys for help pages

Help-page navigation strings (titles, "back to index" links, audience labels) live in `app/translations/{en,pl}.json` under the `help.*` namespace. The translation-drift CI test (`tests/test_translation_keys.py`) catches any key referenced in a template but missing from a catalog.

Body prose (paragraphs, FAQ answers) lives **inline in the templates** — keeping these in JSON would balloon the catalogs without benefit, since the prose is page-specific and never reused.

## Audit-action constants for help routes

Help routes don't write to the audit log — they're read-only public reads with no state change. If you add a help-page interaction that does mutate state (e.g., a contact form), add a new `AuditAction.*` constant in `app/audit_actions.py`.
