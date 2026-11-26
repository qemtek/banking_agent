# Designing generated bank servicing cases

The generator ships 43 outcome variants in seven families. Each can run with the five customer behaviours at easy, medium or hard difficulty. The bank is fictional and its rates and rules are synthetic. The coverage table describes the executable release. The later family designs also describe extensions and illustrative values.

## Runnable coverage

| Family selector | Implemented outcomes | Further fixtures in the design |
| --- | --- | --- |
| `payment_instruction` | Skip one occurrence, handle a released payment, clarify duplicate occurrences, cancel or amend a series, schedule a new one-off, raise a cancellation request, or check a payee's funding shortfall. | Generate cut-offs from a timestamp and calendar, and explicit operation-version conflicts. |
| `cheque_service` | Stop an issued cheque, recognise a cleared or previously stopped one, correct the account for a duplicated serial, or order a book. Clearing can post before confirmation. | Address correction followed by book dispatch and a configurable stop fee. |
| `account_lifecycle` | Open savings or decline for insufficient tenure, close with a fee and balance sweep, retain a held account, open before closing, accept either order, or retain the old account after failed eligibility. | Product comparisons and a customer-selected alternative sweep destination. |
| `term_deposit` | Quote a fixed maturity, book a fixed or recurring deposit, retain the minimum balance, change renewal, redeem above a minimum payout, or keep the deposit open below it. | Dated contribution rows, quote expiries, new rate editions during the conversation and reinvestment after early exit. |
| `loan_service` | Report a due instalment, illustrate a level monthly payment, pay once or detect pending collection, settle within a cap, decline settlement over the cap, or record a request-only settlement. | Full amortisation and final-payment schedule, partial prepayments and a funding shortfall variant. |
| `atm_fee` | Audit one account and period, credit an outstanding overcharge, or recognise a previous full adjustment. The notice includes the covered withdrawals and the charged, due and outstanding amounts. | Multiple fee tiers in one statement, changes of tariff within a period, percentage caps and multi-account netting by account. |
| `dispute_credit_limit` | Same-card dependency, two independent cards, withdrawn limit consent, an existing blocking dispute, urgent card protection, or credit review. | A prior dispute on the identical transaction and multiple conflicting priority changes. |

Generate the seven families from the project root. This command freezes one cooperative world per variant and level, giving 129 cases. Replace the family list with any single selector above for a narrower casebook. Each family has positive and no-write, restricted-scope or pending outcomes.

```sh
python -m bank_demo.casebook generate /tmp/bank-servicing-cases.json --seed 429 --behaviour cooperative --per-cell 1 --family payment_instruction cheque_service account_lifecycle term_deposit loan_service atm_fee dispute_credit_limit
python -m unittest tests.test_servicing_tasks
```

Easy provides full details in the owned record directory. Medium provides references and labels, so the bank must inspect linked records. Hard adds a near match and an expired policy edition. Hard payment release, cheque clearing and settlement-over-cap also change the record between turns. Read-only enquiries take one turn. Changes require an estimate or clarification followed by the customer's scoped instruction. SQLite holds business records, receipts and an ordered event journal for each isolated run. The bank checks access, evidence, the permitted changes and the notices delivered to the customer. The prose explanation goes to communication review. [World generator](../../bank_demo/servicing_worlds.py) · [Tools and checks](../../bank_demo/servicing_tasks.py) · [Bank operations](../../bank_demo/servicing_rules.py) · [Expected states](../../bank_demo/servicing_oracle.py)

The first development pilot exposed three distinctions in the release contract. A bank may deliver a conclusive existing status immediately and acknowledge the customer on the next turn. A maturity quote can precede the funding check required for booking. For a two-part request, the notice can identify either action target. Hard cheque distractors have distinct account-and-serial pairs. After the customer returns, `read_servicing_permissions` exposes the current signed actions and references used by the write service. An enquiry, a foreign session and the initial turn expose an empty list.

For independent card requests, a charge record proves the identity of its associated card. The agent also needs the current record of the card whose limit the customer wants changed. For a closure explicitly conditional on opening savings, failed tenure eligibility supports a zero-value `ineligible` notice for the product or a zero-value `blocked` notice for the closure account. The evidence and mutation checks still require the actual tenure failure and preservation of both accounts. Communication review checks the explanation of the unmet condition. The case's turn snapshots contain the account and card relationships, delivered notices and write journal needed to replay these rules.

The scripted controls run every variant at all three levels through the harness. They check order-sensitive changes, valid alternative orderings, withheld or fabricated actions, stale quotes, monetary errors and effects on other customers. A passing script establishes that a valid interaction can pass the operational grader. Measuring the bank model requires running with a model-backed bank and customer. [Controls](../../tests/test_servicing_tasks.py) · [Evaluation guide](evaluation.md)

## What the public environments actually test

| Environment | Released evidence | Design consequence |
| --- | --- | --- |
| Sierra τ-Banking, v1.0.1 | In `task_060`, closing the oldest current account first removes the tenure needed to open savings. In `task_053`, a newly opened dispute blocks a credit-limit request. In `task_074`, the fee audit reconciles four accounts separately using policy, transaction order and missing charges. The agent can retrieve bank documents and use procedures discovered in them. [Account task](https://github.com/sierra-research/tau2-bench/blob/v1.0.1/data/tau2/domains/banking_knowledge/tasks/task_060.json) · [Dispute task](https://github.com/sierra-research/tau2-bench/blob/v1.0.1/data/tau2/domains/banking_knowledge/tasks/task_053.json) · [Fee task](https://github.com/sierra-research/tau2-bench/blob/v1.0.1/data/tau2/domains/banking_knowledge/tasks/task_074.json) · [Environment](https://github.com/sierra-research/tau2-bench/tree/v1.0.1/src/tau2/domains/banking_knowledge) | Make actions change later eligibility. Derive fees independently from dated tariffs and underlying withdrawals. |
| NPCI TauIndianBankBench | A fresh synthetic customer database, frozen clock, searchable policy and servicing tools support 1,000 tasks in 50 families. `rl_tlfdmat_0197` requests a maturity projection before booking a fixed deposit and a fresh balance afterwards. Other families cover mandate cancellation, stop-cheque requests, loan instalments and early-settlement quotes. Required-call matching is unordered and one occurrence can match two identical reads. [Released task and scorer description](https://arxiv.org/html/2610.30161v1#App-C) · [Dataset](https://huggingface.co/datasets/NPCI/tau-indian-banking) · [Environment and scorer](https://github.com/npci/TauIndianBankBench) | Grade distinct observation events on the relevant sides of a mutation. Count an unsolicited book order or new service request as an action even when the monetary balance is identical. |
| NPCI IndicBankBench | Each case supplies login context, customer turns, a tool menu, deterministic mocks and grading gates. In `acct.agentic.mandate_account_resolution.001`, the payee's mandate reveals the funding account. In `acct.agentic.mandate_cancel_request.001`, this menu supports raising a cancellation request. In `acct.agentic.mandate_cheque_correction.001`, mandate and transaction records point to different accounts, and the customer confirms the corrected cheque target. Safety, action and response gates contribute to success. Quality is advisory. [Published cases](https://huggingface.co/datasets/NPCI/IndicBankBench) · [Environment and grading](https://arxiv.org/html/2609.29167v1#S3) | Give the agent enough evidence to resolve identifiers, model exactly which operations the service supports, and check that the customer hears the actual outcome. |

## Design for extending these families

Freeze an owner, service timestamp and time zone, product terms with effective dates, initial records and permitted operations. Generate a private customer brief containing their objective, any genuine ambiguity, conditional instructions and what they will confirm or decline. Keep the expected answer, reference actions and future customer decisions outside the agent's tools. Freeze separate streams for business facts, identifiers, row order and wording. A matched pair shares all of these except its declared decisive fact and values derived from it.

On each turn record the evidence actually returned, the scoped confirmation, write attempts and results, database changes, and the reply the customer received. For a retry, reconcile the original operation ID with its receipt. An identical transport retry may return that receipt twice. A second business effect or a newly submitted out-of-scope instruction fails the case. Apply three independent checks.

1. **Evidence and procedure.** Require a successful read of the named object or an equivalent view when policy, timing or the customer's request makes it necessary. A required quote precedes consent to its terms, which precedes execution. Independent account and tariff lookups can occur in either order.
2. **Outcome.** Compare the permitted differences to the starting business records, adjusting for scripted external events at their actual turn. Compare balances in pence, retain ownership and object links, and check neighbouring records for collateral changes. Where IDs are generated at execution, follow the relationships and check that the delivered reference resolves to the new record.
3. **Delivery.** Compare requested amounts, dates, statuses and references with the final customer-visible message or delivered document. For open-ended explanations use the repository's communication review with the expected facts. A tool result visible only to the agent does not establish delivery.

## Payments and standing instructions

**Customer decision.** Find out whether a debit is covered, schedule a one-off payment, skip an occurrence, amend future payments or cancel the series. These scopes need distinct records. Generate a source account, payee, recurrence, mandate status and version, and individual occurrences with amount, due time, cut-off and state. A read returns records owned by the signed-in customer. A schedule/amend/skip/cancel operation applies to an explicit target and version. A request-only service creates a tracked case.

| Level | Generate | Required result |
| --- | --- | --- |
| Easy | One identified scheduled occurrence. Generate the service time on either side of the published cut-off, or a one-off payment with a confirmed source, payee, amount and due date. | Cancel one eligible occurrence or report its released status. For a new payment create one pending instruction with the confirmed fields. Record a debit only at the settlement event. |
| Medium | Two active instructions share a payee. One belongs to a joint bills account and one to a personal account. The customer knows the amount or next due date. A funds enquiry instead names only the payee. | Resolve a unique match from the records or obtain the remaining discriminator. For coverage, use the linked account's available funds, report any shortfall at the service time and leave the instructions intact. |
| Hard | An occurrence locks between enquiry and confirmation. Its parent series remains editable. Other variants provide a request-only channel or a corrected instruction to skip one month. | Re-read or handle the version conflict. Honour the latest scope. A request-only outcome requires one open service request and its receipt. A series cancellation preserves every released occurrence. |

**Matched example.** “Cancel next month's £64 gym payment. Keep the membership payments after that.” With a noon cut-off, 11:59 permits one skipped occurrence. At 12:00 the fixture marks it released and the answer explains that it will proceed. Both futures retain the following £64 payment. Grade this by materialising the next two occurrences. An agent that cancels the parent series fails even if this month's debit disappears.

## Cheques

**Customer decision.** Check whether a cheque cleared, stop an outstanding cheque, or order a book if requested. Key instruments by account and cheque serial. Seed the payee, amount, issue date, status, clearing time and any prior stop. Book orders need their own fulfilment row and confirmed delivery destination. A stopped cheque generates a stop receipt. The fee table specifies whether a charge applies.

| Level | Generate | Required result |
| --- | --- | --- |
| Easy | One valid cheque in `issued`, `cleared` or `stopped` state, with an exact account and serial. | Stop once if still eligible and confirmed. Return the recorded clearance or prior stop reference for the other states. |
| Medium | The same serial exists in two of the customer's cheque books, or the customer misremembers the account. | Use account and instrument evidence to identify candidate amounts and payees. Ask only for a missing discriminator. Bind any subsequent stop confirmation to the corrected account and serial. |
| Hard | The clearing event arrives between lookup and approval, or the customer also requests a book after independently confirming their new delivery address. | Compare the current version at commit. Preserve the cleared debit. Grade the book order, fee and destination independently under the customer's conditions. |

**Matched example.** Two accounts contain cheque 0142. The customer identifies the £180 roofer payment, clearing at 10:05. Confirmation at 10:04 produces one stop and prevents clearing. At 10:05 the fixture applies clearing first. The bank reports it cleared and posts no stop fee. The other account's £80 cheque remains outstanding in both worlds.

## Current and savings account opening and closure

**Customer decision.** Identify an eligible product, open it, close a specified account, or complete both with a permitted balance destination. Generate active product and tenure rules, account opening dates, holds, unsettled entries, balance, closure fee and ownership of the destination. The opening tool checks eligibility at execution. The closure service checks blockers, confirms any charge and sweeps an authorised residual balance atomically with closure.

| Level | Generate | Required result |
| --- | --- | --- |
| Easy | One eligible requested product, or a named empty account with no blocker. Flip one eligibility fact or one pending hold. | Open the selected product once, close the target, or give the specific reason it must stay open. A product enquiry alone permits no opening. |
| Medium | Supply two otherwise suitable products with different minimum funding or fee terms, or one closeable account carrying a residual balance and a dated fee. | Explain the applicable terms. Obtain the customer's selection or destination when needed. Verify the exact fee, sweep and final balance. |
| Hard | Opening savings requires an existing current account of sufficient tenure. The only qualifying account is also the closure target. In the paired world a second account qualifies. | In the first world open before closing. In the second accept either order if both fulfil the authorised objectives. If opening fails and closure was conditional on its success, leave the old account open. |

**Matched example.** The bank requires one active 30-day-old current account at the moment savings opens. The customer asks to close account A only if savings opens. A is 90 days old. B is 29 days old in one world and 30 in its sibling. Both branches retain B and deliver the new savings reference. To exercise either legal order in the second world, separately generate the customer's explicit permission for either order. Test a £125 closing balance with a disclosed £5 fee and an authorised sweep to B: B receives £120 and the fee ledger receives £5.

## Fixed and recurring deposits

**Customer decision.** Project maturity, book a deposit subject to a minimum return, change the maturity instruction, or obtain a payout quote before closing early. Generate deposit type, term, principal or contribution cadence, rate edition, compounding convention, funding-account reserve, renewal choice, early-exit penalty, and payout destination. A quote carries the deposit or proposal, rate version, value date, expiry and a term fingerprint. For recurring deposits, use the fixture's actual future contribution dates in the projection.

| Level | Generate | Required result |
| --- | --- | --- |
| Easy | One valid rate card and a quote-only or explicitly authorised booking. | Report the projected value on its maturity date. Booking has one funding debit and one linked deposit. A recurring booking has only the due contribution posted plus its future schedule. |
| Medium | A return threshold or residual balance reserve changes the permitted action. Alternatively the customer changes renewal from roll-over to pay-out while checking a closure quote. | Use the applicable rate and reserve. Bind confirmation to the quoted terms. For a renewal change, alter only the maturity instruction and preserve the principal. |
| Hard | A term correction or rate-version change expires the first quote. An early-closure request may be conditional on the net payout covering a replacement deposit. | Requote the latest terms. Commit only after the customer's threshold and scope are satisfied. Check each leg of any authorised reinvestment and the residual balance. |

**Matched example.** £2,000 at 4.00% simple annual interest for one year matures at £2,080 under the generated terms. “Book only if it returns at least £2,080 and leaves £500 available.” At £2,500 available, booking leaves £500. Changing the opening available balance to £2,499.99 prevents the debit. A separate early-exit case with £36 accrued interest and a £12 charge quotes £2,024. A quote-only customer receives the amount and keeps the deposit open.

## Loan instalments and early repayment

**Customer decision.** Identify the next scheduled instalment, calculate a hypothetical repayment, make an instalment, or request an early settlement. Generate the loan ID, principal balance, amortisation convention, effective rates, remaining schedule, due rows, any paid/pending entries, payment allocation order, settlement charges and the channel's capabilities. A servicing request creates a ticket. A posted repayment requires the service to accept funds and allocate them.

| Level | Generate | Required result |
| --- | --- | --- |
| Easy | Give a named loan with a schedule, or ask for a fixed-rate monthly illustration. Separate the due date and amount from the outstanding principal. | Report the current scheduled row or projection with its assumptions. For a level-payment illustration, use monthly rate `r`, principal `P` and `n` months: `P × r / (1 − (1 + r)^−n)`, or `P / n` at zero rate. The final instalment absorbs the fixture's penny-rounding residual. |
| Medium | Two loans have similar labels, or a due instalment differs by one penny from available funds. A pending debit or previously paid instalment changes whether a new collection is needed. | Identify the correct loan and debit account. Grade amount, date and allocation of an authorised receipt. With a pending or paid row, report that status and preserve the payment count. |
| Hard | Customer asks to settle if total cost stays within a cap. Generate a dated quote, a new accrual or posted instalment before approval, and direct-settlement or request-only service. | Refresh expired quotes, re-evaluate the cap and consent, and use the available procedure. A request leaves the loan active with one linked request. Successful direct settlement clears the balance and ends future collections exactly once. |

**Matched example.** A £1,200 illustration over 12 months at a nominal 12% with monthly interest gives a regular payment of £106.62. For an existing loan with £1,200 principal, £6 accrued interest and £9 settlement charge, the valid quote totals £1,215. A £1,215 cap permits a confirmed settlement at that amount. Changing accrued interest by one penny exceeds the cap. The customer receives the revised figure and there is no debit. On successful settlement match all three allocation components and the next collection state.

## ATM fee investigation and adjustment

**Customer decision.** Explain a withdrawal fee, check a fee period or request an adjustment. Seed individual withdrawals with UTC timestamps, account, tariff edition, local fee period, network, domestic/foreign classification, base GBP amount and linked bank-fee debits, operator charges, reversals and prior credits. Define the allowance unit and tie-break for simultaneous withdrawals. Model a fee adjustment as a credit linked to the reconciled period and account.

| Level | Generate | Required result |
| --- | --- | --- |
| Easy | One settled domestic withdrawal, one fixed bank fee, one current tariff. Change bank network or charged fee. | Calculate the correct and posted bank fees, then explain the difference. An adjustment requires the customer's specified scope. |
| Medium | Mix allowances, operator surcharges, duplicate debits and reversals. Shuffle statement rows. | Sort qualifying withdrawals by tariff period and time, count each eligible withdrawal once, group fee rows by withdrawal and subtract related reversals. Exclude separately identified operator charges when the tariff only covers bank fees. |
| Hard | Mix account tiers and tariff revisions within history. Include a missing charge and a prior partial adjustment. | Compute the net discrepancy separately per account and period using that case's published offset policy. Cap the new credit at the authorised, still-outstanding positive amount. Reconciliation alone never creates a debit for a negative discrepancy. |

**Matched example.** Two partner withdrawals are free per calendar month, then the bank charges £2 each. Three eligible withdrawals were each charged £2, so £6 was charged against £2 due. After a £1 linked prior adjustment, £3 remains. Move the first withdrawal to the previous month, which had no earlier withdrawals, and audit only the current month: £4 charged, £0 due, £1 already credited, £3 remains. This deliberately equal-total control catches a superficial grader: verify the distinct withdrawal sets and expected per-row fees. For a decision-flipping sibling, change the linked prior adjustment to £4 in the original month. The outstanding credit falls to zero.

## Dispute and credit-limit request in one conversation

**Customer decision.** A customer wants to report an unrecognised transaction and request a credit-limit change. The fictitious policy can make a new dispute block the request on the same card. Generate two independently scoped requests, disputed charge, affected card, current limit, requested limit, eligibility rule, outstanding disputes, urgent-risk flag and a frozen credit-service decision. The customer can accept an explained order, prioritise the dispute or withdraw the limit request.

| Level | Generate | Required result |
| --- | --- | --- |
| Easy | Each request concerns a different card and the eligibility rule applies per card. | Resolve both independently, reporting the dispute reference and actual limit decision. Accept either execution order when the customer's instructions allow it. |
| Medium | Both requests affect the same card. The customer authorises both and accepts processing the eligible limit request before opening the dispute. | Retrieve the applicable rules, explain the consequence, obtain scoped consent and perform the limit request first. An underwriter referral creates a pending request, then the dispute can still be filed if the stated policy allows it. |
| Hard | The customer changes priority after hearing the trade-off, a dispute already exists, or the bank's urgent-risk rule gives protection precedence. | Apply the applicable condition. File the authorised dispute, record any separately required protective action, and defer the now-ineligible increase. Avoid duplicate disputes. A declined or deferred increase preserves the old limit. |

**Matched example.** The customer asks about a £47.30 charge and raising a £2,000 limit to £2,500. If they agree to process the limit first and the service approves, the ledger contains the new £2,500 limit and one open dispute. If their next turn says “File the dispute now and leave the limit alone,” exactly one dispute is opened and even an attempted limit change fails the scope check. A public policy explanation alone authorises neither write.

## Repository fit and acceptance

The original scheduled-payment list remains part of the dispute distractor environment. The servicing generator uses a separate SQLite bank with parent instructions, dated occurrences and linked accounts. The earlier dispute ledger and card desk supplied the design patterns for authenticated access, receipts, saved-state checks and frozen specialist outcomes. [Existing activity records](../../bank_demo/service_records.py) · [Dispute ledger](../../bank_demo/ledger.py) · [Card desk](../../bank_demo/card_tasks.py)

Before admitting a template, replay each valid branch from a fresh world. A second checker derives the expected monetary deltas and terminal status directly from the frozen facts. Then try a plausible faulty trace, such as cancelling the series, paying twice, using a stale quote, or crediting the account total against the wrong withdrawals. Require a specific failing check. Run the same paired worlds across levels, including the start and end instant of each cut-off or quote window. Preserve the existing generator, task and rubric versions for earlier casebooks. [Local generator](../../bank_demo/instances.py) · [Casebook runner](../../bank_demo/casebook.py) · [Existing case design](task-taxonomy.md)

## Exploratory check on a new loan workflow

Freeze a pair with `python3 -m bank_demo.servicing_transfer freeze audits/casebooks/loan-transfer.json --seed 26112619 --pairs 1`. Both sides receive the same three customer turns and the same two quote revisions. The signed release on the final turn either permits settlement within the original cap or withdraws the instruction. Use the casebook runner with `--customer-mode llm --repeats 1` for a paraphrased opening and fixed app turns, or `--customer-mode scripted --repeats 1` for fully fixed dialogue. Then run `python3 -m bank_demo.servicing_transfer score BATCH/report.json`. A pair passes operationally when both sides complete all three turns, read the current quote and signed status before acting, deliver the correct notices, and leave the loan and account records in the expected state. Treat the result as an exploratory check of this workflow and review free-form explanations separately. The two conversations in a pair share one world and belong outside the powered family comparison.

On 26 November 2026, Terra medium completed both sides in **3/3 worlds** with the standard bank and seed 26112619. The six conversations cost an estimated **$0.45**. The original rubric failed all six for omitting a separate loan-detail lookup on a return turn. Each bank had obtained a fresh, dated quote referencing the exact loan and checked the current app status. Scorer v7 accepts that evidence, while still requiring the correct writes, balance, notice and authorization. The v7 replay used the original turn snapshots. Communication review remains open. The local batch is `0155c39d4f294659bb7cfbf7b446f4ba`.
