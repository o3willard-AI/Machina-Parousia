# Accountability: the human behind every agent action

Machina Parousia follows the [Registered Accountable Entity (RAE)](https://github.com/o3willard-AI/RAE)
model: an agent action is not fully accounted for until it is attributable to a
named human being. An agent is the *subject* of attribution, never the object
(RAE N2) — the question Parousia answers is not just "which agent sent that
email," but "which person is answerable for the agent that sent it."

## The accountability chain

**1. An invite is created by a human sponsor (pre-attribution).**

Every agent account begins with a one-time invite key (`po_inv_…`). A key is
created by a human sponsor — by CLI or the admin API — and records that sponsor
at creation time (`sponsor_id`, `sponsor_contact`, `sponsor_method`), before any
agent exists to act. This is the shape RAE N1 requires: the human is named *in
advance*, not reconstructed after the fact. Naming someone after the fact is
post-hoc audit attribution; Parousia's chain runs the other way.

**2. Onboarding binds the account to that sponsor.**

An invite is consumed exactly once, during onboarding, and the sponsoring human
is copied onto the agent's Account. From that point the account is not merely
"an agent on this server"; it is *this agent, under this human*. The binding
travels with the account for its whole life.

**3. Every recorded action names the human.**

Parousia's three capabilities — email (`send_email`, …), temporal (calendar,
timers, journal), and spatial (web browsing) — record write-side tool calls.
Each record names the agent *and* the human sponsor the agent's account is
bound to, so the accountability trail resolves person → agent → action without
leaving the record. Attribution terminates at the human, not the agent.

## Assurance level: L0 (declared, unverified)

The sponsor is **self-declared at invite time and is not verified**. Nothing in
Parousia proves that the person named as `sponsor_id` is who they claim to be,
or that they consented to be named. Under the RAE assurance ladder this places
the claim at **L0 (declared)**:

> L0 — Claimed identity only. Below the registration bar: a seed of the
> practice, not the practice. (RAE §3)

So the honest claim Parousia makes is: it **displays an RAE at L0 (declared,
unverified)**. It does **not** claim to *implement* RAE — the attribution
machinery of the practice (organization-verified identity, signing credentials
bound to authorization records, the rest of N1/N3/N4/N6–N8) begins at L1, and
Parousia is not there. See [RAE-CONFORMANCE.md](../RAE-CONFORMANCE.md) for the
exact claim boundary.

L0 is still worth having: an unverified-but-recorded sponsor is a name an
operator can act on, a trail a reviewer can follow, and — because the invite is
recorded into the account at creation — a durable declaration of who
vouched for an agent and when. A false sponsor declaration is an
invite-trust problem (the human who generated the key vouched for it), not an
audit-integrity problem. Verifying sponsor identity would raise the claim to
L1 and is out of scope for the current version.

## Why this matters here specifically

Parousia gives agents a persistent, sovereign presence in the world — a real
email address, a calendar, a browser. Those are exactly the capabilities that
let an agent act *as if it were a person*: sending mail, booking time, engaging
websites. An agent with an email identity and no accountable human behind it is
an unattributable actor on the open internet. Binding every account to a named
sponsor — even a declared, L0 one — is the difference between "an agent on this
server did this" and "this person is answerable for the agent that did this."
