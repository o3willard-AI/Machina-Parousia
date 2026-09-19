# RAE Conformance Statement — Machina Parousia

**Specification:** [Registered Accountable Entity (RAE)](https://github.com/o3willard-AI/RAE), v1.0.4
**Product:** Machina Parousia (this repository)
**Claim:** *displays an RAE at L0 (declared, unverified)* — **not** *implements RAE*
**Date:** 2026-09-19

Per RAE §4, an L0 display is not an implementation and "publishes no
conformance statement." This file is therefore **not** a §4 conformance
declaration — Machina Parousia makes no claim to implement the RAE practice,
whose attribution machinery (N1, N3, N4, N6a/N6b, N7, N8) begins at L1. It is
published voluntarily, at the conventional location §4 names, to state exactly
what Parousia does and does not assert, so no reader has to infer the boundary.

## The claim, precisely

Machina Parousia displays an RAE at L0 (declared, unverified):

- **What exists:** a human sponsor (`sponsor_id`, `sponsor_contact`) creates
  each one-time invite key, onboarding copies that sponsor onto the agent's
  Account, and recorded write-side actions (email / temporal / spatial) name
  the sponsor — so every agent action is attributed to a *declared* human, not
  only to an agent identity. The invite → account binding is recorded at
  creation and is persisted in the account store.
- **What is missing for L1:** the sponsor is self-declared and not verified —
  no organization-verified identity, and no signing credential bound to the
  sponsor's authorization records. Parousia will not claim L1 until an
  operator-verifiable sponsor identity exists.

## Clause status (informational — L0 declares no enforcement tier)

| Clause | Status in Parousia | Note |
|---|---|---|
| N1 Pre-attribution | Partial, declared | The sponsor is named on the invite before the agent account exists — pre-attribution in *shape*. Not registration-grade: the binding is an unverified declaration. |
| N2 Agents are never RAEs | Honored | Attribution terminates at the human sponsor; agents are subjects, never objects, of accountability. |
| N3 No-RAE invariant | Informational | Every recorded action names a sponsor, so no recorded action is silent about accountability. Parousia declares no enforcement tier at L0; block-vs-flag semantics belong to L1 conformance. Read-only tool calls are not recorded (by design) and carry no action to attribute. |
| N4 Influenced actions | Not applicable | Parousia records agent-executed actions directly; it does not model a human acting on agent output as a separate provenance chain. |
| N5 Natural-person resolution | Honored | The sponsor is a named natural person (`sponsor_id` / `sponsor_contact`), never an organization. |
| N6a/N6b Sponsorship scope | Partial, declared | An invite authorizes one account (one-time key, `max_uses`); it is a coarse standing authorization, not a declared action-class scope evaluated at runtime (N6a) or reviewed for breadth (N6b). |
| N7 Sponsorship lifecycle | Partial | An account can be suspended/deactivated (`set_status`), which severs the agent's ability to act; explicit sponsor expiry, transfer, and in-flight-revocation semantics are not implemented. |
| N8 Agent-to-agent delegation | Not applicable (architectural) | Parousia brokers agent → external-world actions (email/calendar/browse); it does not orchestrate agent-invokes-agent chains in the current version. If it later does, this becomes applicable and must be re-declared. |

## Proof location

- **Sponsor binding (invite layer, present):** the invite store records
  `sponsor_id`, `sponsor_method`, `sponsor_contact`, status, and the consuming
  `account_id` (`src/parousia/auth/invites.py`); admins can list invites and
  their sponsors (`InviteStore.list_invites`).
- **Sponsor on the Account + action records (companion code change):** the
  Account-level sponsor field (copied from the invite at onboarding) and the
  propagation of that sponsor into write-side tool-call recording
  (`MemoryRecorder.record_tool_call(...)`, `src/parousia/memory/recorder.py`)
  are delivered by the parallel code task this documentation describes. This
  PR is docs-only; it documents the authoritative model that change
  implements. Read-only tools and failed calls are intentionally not recorded.
- **Inspecting the chain:** the account store
  (`/var/lib/parousia/accounts.db`) joins invite → account → sponsor once the
  companion change lands.

## What would change the claim

Raising Parousia to *implements RAE 1.0.x L1, \<tier\> tier* requires:
organization-verified sponsor identity; a signing credential bound to the
sponsor's authorization records; declared-scope expressions with N6a runtime
evaluation and N6b breadth review; N7 lifecycle semantics (expiry, transfer,
in-flight revocation); an enforcement-tier declaration for N3; and conversion
of this file into a true §4 conformance statement using the canonical claim
form.
