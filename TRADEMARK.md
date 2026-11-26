# Trademark Policy

**Marks covered:** `TokenSafe`, `OUMV`, `OUMV Certified`, and the TokenSafe and OUMV logos
(the "Marks").

**Owner:** Oleg Savelev. **Contact:** [corp@tokensafe.dev](mailto:corp@tokensafe.dev)

This policy covers the software, specifications and documentation in this organization's
repositories. It exists so that forks, integrations and certifications can use the names
predictably — not to restrict the code, which is licensed separately (Apache-2.0 for
implementations, CC-BY-4.0 for the specification text and fixtures).

## 1. Status of the Marks

- `TokenSafe` and `OUMV` are used as trademarks of the Owner. Registration is being pursued
  in the European Union; until a registration issues in a given jurisdiction, the marks are
  protected by use and by unfair-competition law.
- Use **™** freely next to the marks. Use **®** only where a registration has actually
  issued and only in connection with the registered goods and services.
- `OUMV Certified` is a certification mark used under the program in section 5. It is not
  available to the Owner's own adapters, which are labelled `maintainer-signed` instead.

## 2. You may use the Marks without asking

Nominative, truthful, referential use is allowed:

- **Implementation statements:** "implements the OUMV protocol", "OUMV-compatible",
  "built on OUMV", "OUMV adapter for <framework>", "speaks OUMV v1.0".
- **Compatibility statements:** "works with TokenSafe", "TokenSafe plugin for <editor>",
  "exports traces in the OUMV format".
- **Self-reported conformance:** "passes the OUMV Level 3 conformance fixtures
  (self-certified)" — only if it is true and reproducible by a third party.
- **Editorial use:** articles, talks, tutorials, comparisons, academic work, and repository
  READMEs that describe your project.
- **Forks:** you may state "fork of TokenSafe" or "fork of the OUMV reference
  implementations", and you must keep the copyright and licence notices intact.

Every such use must be truthful, must not suggest endorsement, sponsorship or certification,
and must not make the Mark the most prominent element of your branding.

## 3. You need written permission to

- Use `OUMV Certified`, the certification badge, or any variant ("OUMV-Certified",
  "OUMV Approved", "TokenSafe Certified") — these are granted only through section 5.
- Use a Mark in a product name, company name, trade name, app name, domain name, social
  handle, or npm/PyPI/Go module name, including prefixes such as `tokensafe-*`, `oumv-*`,
  or a package scope named after a Mark.
- Use a Mark in merchandising, or in a way that implies your product *is* TokenSafe or
  *is* the OUMV protocol rather than something that works with them.
- Register a Mark, or a confusingly similar sign, as a trademark, company name or domain.

## 4. Logos

The logos may be reproduced unmodified to refer to the project (for example, in a "works
with" list). Do not recolor, distort, crop, animate or combine them with other marks, and do
not use them as your application icon or favicon.

## 5. `OUMV Certified` program

`OUMV Certified` is granted per adapter, per protocol major version, for 12 months:

1. **Criteria.** The adapter passes the language-neutral conformance fixtures at the claimed
   level (cumulative levels 1–3) over the transport an agent actually uses. A skipped case
   caps the claim; it never counts as a pass.
2. **Evidence.** A conformance report conforming to `schemas/conformance-report.json`,
   signed by the Owner, plus a registry entry recording adapter, profile, level, protocol
   revision and expiry.
3. **Access.** The suite itself is free and open — anyone may run it and publish a
   self-certified result. Certification adds the signed report, the registry listing and the
   right to use the mark.
4. **Non-discrimination.** Any implementation that meets the criteria is eligible on the same
   terms. Fees, if any, cover verification, signing and registry maintenance, not permission
   to compete.
5. **Revocation and appeals.** Certification lapses on expiry, on a failing re-run, or on a
   material change that invalidates the report. Appeals go to the contact address above and
   are answered in writing.

The Owner's first-party adapters are labelled `maintainer-signed` and are not described as
independently certified.

## 6. Reporting misuse

Write to [corp@tokensafe.dev](mailto:corp@tokensafe.dev) with a link and a short description.
We prefer to resolve matters by a rename or a clarification. Where a repository, package or
domain misleads users, we also use the trademark processes of GitHub, npm, PyPI and the
applicable dispute-resolution policies.

## 7. No trademark rights from the code licences

Apache-2.0 section 6 and CC-BY-4.0 section 2(b)(2) do not grant trademark rights. You may
exercise every right those licences give you — use, modify, fork, redistribute, sell — but
not under our names. This policy is the trademark half of that equation.

## 8. Changes

This policy may be updated; the current version is the one in this repository. Material
changes are noted in the repository history. This document states our usage rules and is not
legal advice.
