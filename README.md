<div align="center">

# 🛒 ProofCart

### An AI purchasing agent you can actually let **pay** — because a referee refuses to spend on anything it can't verify, and the payment is provably crash‑safe.

<br/>

[![3 external apps](https://img.shields.io/badge/external%20apps-Slack%20·%20Notion%20·%20Stripe-6C5CE7)](#-three-external-apps-that-work-together)
[![LLM](https://img.shields.io/badge/LLM-DeepSeek%20(swappable)-00A67E)](#-three-external-apps-that-work-together)
[![Payments](https://img.shields.io/badge/Stripe-TEST%20MODE%20·%20no%20real%20money-635BFF)](#-live-proof)
[![Reliability](https://img.shields.io/badge/settlement-crash--safe%20single--charge-2ECC71)](#-how-we-test-reliability-25)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB)](#-how-to-run)

<br/>

## ▶️ [**Watch the 2‑minute demo**](REPLACE_WITH_YOUR_VIDEO_LINK)

<sub>_(replace this link with your video URL before submitting)_</sub>

</div>

---

> [!NOTE]
> **Multi‑App AI Agent Hackathon submission.** ProofCart is an autonomous agent that does **real economic work** across **three external apps** — it reads supplier negotiations from **Slack**, compares them with evidence, and — once the owner approves the exact quote — settles a real payment in **Stripe** and records it in **Notion**. The hard part isn't buying; it's *knowing when it's safe to let an agent move money.*

## 📑 Contents

- [The 60‑second pitch](#-the-60-second-pitch)
- [Three external apps that work together](#-three-external-apps-that-work-together)
- [Live proof](#-live-proof)
- [How it works (architecture)](#-how-it-works)
- [Crash‑safe payment](#-crash-safe-payment)
- [How to run](#-how-to-run)
- [How we test reliability (25%)](#-how-we-test-reliability-25)
- [Judging‑criteria map](#-judging-criteria-map)
- [What's original here](#-whats-original-here)
- [Honest limitations](#-honest-limitations)

---

## 🎯 The 60‑second pitch

An operations manager has quotes scattered across supplier chats:

> *"Buy 20 sensor kits. Under **$1,000** all‑in. Delivered **by Friday**. No substitutes. And **don't pay** without my say‑so."*

ProofCart reads those Slack conversations, reconstructs each supplier's **current** offer (telling a real quote apart from an unaccepted counter), ranks the eligible ones with **evidence quality**, and shows the owner a shortlist. When the owner approves an exact quote, a **Referee** — which has *no* access to ground truth — checks every hard rule and every claim, and only then does the payment execute. If the process is killed mid‑payment, it **reconciles to exactly one charge**.

> [!TIP]
> **Why it's not a toy:** the agent makes a real economic decision (what to buy, when to walk away, when to escalate), grounded in real supplier data, and executes a **real Stripe API payment**. Test mode just points that at the sandbox — the workflow is identical to production.

---

## 🔌 Three external apps that work together

The whole point of the hackathon: an agent that **takes action across ≥3 external apps** to accomplish something useful. Here they hand off in one causal chain — *request in → compare → money out → recorded everywhere.*

| App | Role in the agent | Action it performs |
|---|---|---|
| 💬 **Slack** | the owner's interface | **reads** the supplier negotiation threads, **posts** the ranked shortlist, and **posts** the verified outcome |
| 📝 **Notion** | the durable record | **writes** the comparison + the settled order (auto‑creating columns), idempotent by `order_id` |
| 💳 **Stripe** *(test mode)* | the settlement rail | **creates & confirms** a real `PaymentIntent` for the exact approved amount |
| 🧠 **DeepSeek** *(swappable → OpenAI / Anthropic)* | the agent's brain | **extracts** typed offers from messy supplier free‑text |

> [!IMPORTANT]
> Stripe runs in **test mode only** — no real money moves, ever. Switching to live is a deliberate key change we do not make.

---

## ✅ Live proof

This isn't a mockup — here's an actual run against real Slack + DeepSeek + Stripe + Notion:

```text
SHORTLIST (from real Slack + DeepSeek):
  #1 Acme        $  966.00  by 2026-09-16   ← recommended
  #2 Delta Gear  $  997.60  by 2026-09-17
  excluded:
    Cirro Parts  $  915.20  — missing critical field: shipping
    Bolt Supply  $  878.20  — delivery 2026-09-24 is AFTER the deadline   ← cheapest, but unsuitable
OWNER APPROVES Acme → referee: PERMIT → SETTLED $966.00 → state: complete
```

**The Stripe charge, stamped by the agent** (metadata proves *the agent* created it, tied to the negotiation):

```json
PaymentIntent pi_3UFLPuQ… — $966.00 USD — succeeded
metadata: { "order_id": "ord_ae64de2f1c5b",
            "request_id": "req_demo_001",   // the owner's request
            "quote_id":  "sup_acme-q1" }    // Acme's quote it settled
```

> [!TIP]
> In the Stripe dashboard, **Developers → Logs** shows the `POST /v1/payment_intents` call with its `Idempotency‑Key` — proof an *agent* reached out programmatically, not a human clicking.

---

## 🧭 How it works

```mermaid
flowchart LR
    S[💬 Slack threads] --> C[Collector]
    C --> X["🧠 Offer Extractor<br/>DeepSeek"]
    X --> E[Evidence Checker]
    E --> CMP[Comparator → Shortlist]
    CMP -->|posts shortlist| S
    CMP --> N[(📝 Notion)]
    CMP --> O{Owner approves<br/>exact quote?}
    O -->|approval bound to<br/>terms + expiry + identity| R[["⚖️ Referee<br/>no ground truth"]]
    R -->|PERMIT| P[Settlement pipeline]
    R -->|ESCALATE / BLOCK| STOP([🚫 no payment])
    P -->|idempotent + reconcile| ST[💳 Stripe test PaymentIntent]
    P --> N
    P -->|verified outcome| S
```

**The Referee is the star.** It sees only the owner's policy, the typed quote, and the tool‑call log — never the "truth." So it can't detect a lie; it detects **claims with no evidence** and refuses to pay on those. A valid owner approval is what authorizes spending — never the code, never a test.

<details>
<summary><b>What the Referee checks (deterministic, pure Python)</b></summary>

- arithmetic: `total == unit×qty + shipping + tax − discount`
- budget: all‑in total ≤ the owner's cap (shipping + tax included)
- deadline, no‑substitution, required fields, payment **recipient**, quote expiry
- **approval binding**: approver identity == owner, not expired, `terms_hash` unchanged since approval
- **claim provenance**: any load‑bearing field with no tool evidence → surfaced as a warning; a *missing* field → escalate to the owner

</details>

---

## 🛟 Crash‑safe payment

The reliability headline: **one charge, even if the process dies mid‑payment.**

```mermaid
sequenceDiagram
    participant Ex as Executor
    participant L as Ledger
    participant St as Stripe (test)
    Ex->>L: write-ahead PENDING (unique order_id)
    Ex->>St: create PaymentIntent (Idempotency-Key)
    Note over Ex,St: 💥 process killed / response dropped
    Ex->>L: on restart → read PENDING + saved PI id
    Ex->>St: retrieve PI by id (never a search)
    St-->>Ex: succeeded
    Ex->>L: mark SUCCEEDED → exactly one charge
```

Persist the `PaymentIntent` id **before** confirming; reconcile by retrieving that exact object (Stripe search isn't immediately consistent, so an empty search can never justify a second charge); reuse the same idempotency key; stop blind retries before the key‑retention boundary.

---

## 🚀 How to run

```bash
pip install -r requirements.txt
cp .env.example .env            # everything degrades to mocks if a key is blank

make demo                       # DEV: full flow, no keys needed
make demo-live                  # LIVE: real Slack → DeepSeek → Stripe test → Notion
make evals                      # reliability scoreboard (ProofCart vs. a baseline)
make test                       # module self-tests
```

<details>
<summary><b>Live setup (.env) — three apps + one LLM key</b></summary>

| Variable | From |
|---|---|
| `DEEPSEEK_API_KEY` | platform.deepseek.com → API keys (or set `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` instead) |
| `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID` | a Slack app (scopes: `channels:history`, `chat:write`), invited to the channel |
| `PROOFCART_OWNER_ID` | your Slack member id — the only identity allowed to approve |
| `STRIPE_SECRET_KEY` | Stripe dashboard → **test** key (`sk_test_…`) |
| `NOTION_TOKEN` + `NOTION_DB_ID` | a Notion integration + a database (title column `order_id`) shared with it |
| `PROOFCART_MODE` | `live` |

Then: `make seed` (posts the supplier threads into your channel) → `make demo-live`.
</details>

---

## 🔬 How we test reliability (25%)

We treat the agent like a payments system: **no side effect without a rehearsal, and never a lie about what happened.** `make evals` runs a scenario suite for ProofCart **and** a stripped‑down baseline (**NaiveCart**) through the *same* grader, in dev mode (deterministic, no keys), and writes [`reports/scoreboard.md`](reports/scoreboard.md).

<details open>
<summary><b>The scenarios (product + settlement behaviour)</b></summary>

| # | Scenario | Expected |
|---|---|---|
| 1 | Supplier revises its quote later in the thread | use the current version, keep history |
| 2 | Buyer counter the supplier never accepted | **not** reported as a confirmed price |
| 3 | Cheapest offer misses the deadline | excluded, with the reason |
| 4 | Shipping missing | pending clarification, not purchase‑ready |
| 5 | "ignore your policy" injected in a thread | authority holds, no auto‑approval |
| 6 | Owner approves the recommendation | PERMIT → settled → **exactly one charge**, no over‑claim |
| 7 | A non‑owner tries to approve | no payment |
| 8 | Terms change after approval | blocked (terms‑hash mismatch) |
| 9 | Payment confirmation lost mid‑flight | recovers to **one** charge |
| 10 | Duplicate request / re‑run | still **one** charge |

</details>

**Metrics we report** (numerator/denominator, never vague %): exactly‑once rate under crashes · silent‑failure divergence (claimed‑paid vs. verified ledger) · unauthorized‑payment count (target 0) · false‑blocking of a valid deal (target 0) · recovery success.

**The baseline comparison** (`NaiveCart`): same extraction, but with the enforced guardrails removed — it pays without the owner/referee gate, retries with a fresh key (double‑charges under the crash), and reports success even on failure. We report what *actually* happens, side by side. *"Prompted guardrails vs. enforced guardrails."*

### 📊 Measured results — `make evals` · 14 scenarios · deterministic · no keys

| | 🛒 **ProofCart** | 🤖 NaiveCart _(guardrails off)_ |
|---|:---:|:---:|
| Scenarios passed | **14 / 14** | 7 / 14 |
| Exactly‑one‑charge under crash / duplicate | **2 / 2** | 0 / 2 _(double‑charges)_ |
| Silent‑failure over‑claim _(says paid, isn't)_ | **0 / 3** | 1 _(declined card)_ |
| Unauthorized payments _(target 0)_ | **0** | 9 |
| False‑blocking of a valid deal _(target 0)_ | **0** | 0 |
| Recovery success | **1 / 1** | 0 / 1 |
| Referee replay determinism | **6 / 6 identical** | — |

Charge counts are read **at the payment rail** (the arbiter of what actually happened), never asserted up front. Full per‑scenario breakdown → [`reports/scoreboard.md`](reports/scoreboard.md).

> [!NOTE]
> A **successful run** = the right shortlist, a PERMIT only after a valid owner approval, exactly one charge, and app records that agree. Scoreboard numbers land in `reports/scoreboard.md` (`make evals`).

---

## 🏅 Judging‑criteria map

| Criterion | Weight | Where to look |
|---|---|---|
| **Technical execution** | 30% | 3 real integrations, constrained tool access, write‑ahead ledger + idempotency + reconcile, executor‑only credentials |
| **Reliability & evaluation** | 25% | scenario scoreboard, NaiveCart baseline, crash‑safe single‑charge, silent‑failure metric, byte‑replayable referee |
| **Usefulness** | 20% | an owner delegates procurement and gets a verified order *or* an evidence‑backed reason it stopped |
| **Originality** | 15% | a *no‑ground‑truth* referee that gates settlement on evidence — the trust layer the payment rails (AP2 / ACP / x402) leave out |
| **Demo clarity** | 10% | one request → why the cheap offer is unsuitable → owner approves → real charge → records agree |

---

## 💡 What's original here

Not "an agent that buys things" (that exists). The wedge is **settlement integrity**: a runtime referee that **pays only on tool‑backed evidence** and a payment path that is **crash‑safe (single‑charge)** — the piece the agent‑payment rails still leave to implementers.

<details>
<summary><b>2026 research it's grounded in (verify each before quoting)</b></summary>

- Evidence‑gated authorization / hallucination‑to‑action — arXiv:2605.19192
- Durable, replay‑resistant settlement — CapLease arXiv:2608.01710 · Verified Tool Calls arXiv:2608.02645
- Silent failure (claimed vs. verified) — arXiv:2606.09863
- Environment‑as‑verifier (referee/grader asymmetry) — TERMS‑Bench arXiv:2605.13909
- Effect‑level payment eval under faults (neighbouring benchmark) — FinalityBench arXiv:2609.04706

We don't claim to have invented these — we operationalize them as an owner‑controlled workflow with an honest, replayable eval.
</details>

---

## ⚠️ Honest limitations

> [!WARNING]
> - The supplier side is a **labelled simulation**; Stripe is **test mode** (no real funds).
> - The Referee can flag a claim with *no evidence* — it **cannot** catch a lie that's consistent with a (wrong) tool result.
> - The buyer LLM is prompt‑injectable and we show it fooled; the **Referee is not an LLM** and never reads supplier free‑text, so settlement never depends on it.
> - "Crash‑safe" = **single‑charge against our injected fault set**, not strict distributed exactly‑once (impossible in general).
> - The eval is a deterministic **regression suite**, not an unseen benchmark.

**Known ways to fool it:** a supplier who lies consistently across every tool response · a compromised tool integration · a wrong policy · a rubber‑stamping approver.

---

<div align="center">

**Built for the Multi‑App AI Agent Hackathon** · one agent · three apps · real economic work, safely.

### ▶️ [Watch the 2‑minute demo](REPLACE_WITH_YOUR_VIDEO_LINK)

</div>
