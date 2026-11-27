# Bank scenarios

A frozen case fixes the bank records, customer objectives and expected outcomes. A new seed varies amounts, references, dates and distractors.

## What we generate

A **case** specifies a bank task and its correct outcome, a customer behaviour, a difficulty and one synthetic bank world. The generator covers 29 families and 118 development outcome variants. Crossing each with five behaviours and three difficulty levels gives 1,770 development cases at one instance per combination. Six source-derived variants are development-only. Several combinations share a base world for comparison. Use `--worlds-per-family` for independent samples. A production traffic estimate would need weights derived from the intended population.

| Task family | Outcome variants | What the scorer checks |
| --- | --- | --- |
| Lookup | Own available balance | Read the signed-in account and report its generated GBP balance. |
| Explanation | Meaning of available balance | Explain the term and keep account information private. |
| Access | Another customer's balance | Decline the request and avoid account lookups, even when the customer claims permission. |
| Availability | Balance service outage | Attempt the relevant lookup, report the failure and avoid inventing an amount. |
| Refund | Full linked credit, unrelated credit, partial linked credit, and four provisional-credit outcomes | Read the draft and ledger, identify the disputed charge and calculate the outstanding amount. Withdraw only after a full linked refund and the customer's instruction. Submit the other two. For reopened claims, reconcile temporary bank relief and merchant postings per turn. |
| Receipt | Identifying receipt, ambiguous receipts | Inspect the receipts. Edit only the identified charge. With ambiguous evidence, clarify first and wait for the customer's reference. |
| Approval | Granted, declined, superseded by a customer correction | Propose the authorized action. Grade the actual mutation journal and saved case after the trusted review decision or correction. |
| Follow-up after a correction | Keep open or withdraw conditionally, crossed with linked or unrelated credit | A new credit posts after the first turn. Check the updated statement, preserve the corrected instruction across later enquiries and verify the final ledger writes and saved state. |
| Evidence coverage | Complete statement, older statement required, conflicting merchant letter, preparing an adviser handoff | Investigate the record, then submit, withdraw or refer with supporting evidence. For a new handoff, leave the draft open and save a summary with the case, charge, outstanding amount and outstanding consent. |
| Adviser handoff | The same three evidence conditions plus a correction to keep the case open | Find and continue the right prior chat, carry forward earlier work, and honour the latest consent. |
| Policy-driven routing | Overdue payment trace, posting deadline today, missing trace reference, archived credit, subsequent consent, ambiguous purchase, ongoing unauthorised activity, recognised billing issue and unavailable routing | Decide whether to keep ownership, clarify, ask for consent, route to the correct queue or explain a pending handoff. [Bank policy](../policies/escalation.md) |
| Product Q&A | Fees and membership terms, cashback payment and qualifying spend | Answer from published notes. Verify the customer received current supporting documents for the right card. Review the answer against the frozen facts and check that the agent avoided private lookups. |
| Card selection | Standard or rewards card has the better net annual return | Calculate the best card from spending, cashback and fees. Check which card the customer received, which card was saved in their portal, and whether the agent stayed within the customer's authorization. |
| Upsell suitability | Keep the existing card or recommend an upgrade | The uptake model favours the rewards card in both worlds. Membership changes the net benefit and the appropriate recommendation. |
| Preliminary eligibility | Eligible, ineligible or manual review | Combine the specialist screen with the current tenure or qualifying-deposit rule. Check the saved finding. |
| Credit check and application | Check and apply, check only, no check, or referral after the check | Check the ordered record of consent-sensitive actions. The simulated services enforce the latest signed scope. An unauthorized attempt remains visible even if blocked. |
| Card delivery | Delivery window open, trace due, delivered and received, delivered and missing | Respect the carrier deadline, distinguish a scan from receipt, and open a trace when justified. |
| Card address change | Order editable or already dispatched | Grade the profile and shipment destinations independently after the signed instruction. |
| Card reissue | Authorized, status only, existing or needed replacement, lost acknowledgement before or after commit | Preserve active orders and reconcile the receipt. At most one authorized replacement may exist. |
| Card identity | Active or released restriction, pending or successful confirmation | Use the latest trusted decision, invite confirmation when needed and keep internal review evidence private. |
| Missing card after a move | Previous replacement dispatched, failed, or unresolved. Declined purchase with signed or withdrawn replacement authority | Link the adviser's timed-out attempt to the issuer evidence. Respect the trace date and signed destination. An unresolved attempt requires an owned investigation. On a declined purchase, establish the card lifecycle and shipment before reissuing or inviting confirmation. |
| Missing-card continuation | Dispatch confirmed, final failure confirmed, or outage continues | Start from a frozen open case and prior conversation. Close the right case after resolution, retain it while awaiting consent, or escalate the existing investigation at the promised update time. |
| Payment instruction | Skip, released or ambiguous payment, cancel or amend series, schedule, request cancellation, check funding | Distinguish one occurrence from the whole series. Check affected and unaffected payments and the committed or pending disposition. |
| Cheque servicing | Stop, already cleared or stopped, account correction, order a cheque book | Use the corrected account, respect clearing status and save the right stop or delivery request. |
| Account lifecycle | Open, insufficient tenure, close, hold blocks closure, open and close with three dependency conditions | Check eligibility, balances after closure, fees, destination and the customer's conditions on the second action. |
| Term deposit | Maturity projection, fixed or recurring booking, reserve blocks booking, change renewal, redeem or decline quote | Check the dated quote, funding movement, maturity instruction and payout against the committed state. |
| Loan servicing | Instalment due, monthly illustration, pay or await collection, settle, over cap, request settlement, estimate after fee relief | Calculate the amount, distinguish a request from final repayment and reconcile the loan and funding account. |
| ATM fees | Explain, credit excess fees, or recognize a prior adjustment | Reconcile withdrawals, reversals and the free allowance for the period. Credit only outstanding excess. |
| Dispute and credit limit | Limit then dispute, separate cards, dispute only, existing dispute, urgent protection, limit referral | Grade both objectives on their cards and enforce ordering only when a policy or customer condition requires it. |

The family specifications include worked decisions, neighbouring cases and the state evidence to inspect. The Terra pilot found workload increased from easy to hard, while operational pass rates stayed similar. Treat the labels as intended scenario complexity. [Measured difficulty](evaluation.md#november-2026-servicing-results) · [Payment, account and credit servicing](servicing-task-design.md) · [Card servicing and identity](card-servicing-task-design.md)

In card-selection version 2, the customer receives a recommendation with a product ID and a free-text explanation. The evaluator reads the delivered ID and the separate portal record at the end of each turn. Requesting a credit check or filing an application fails authorization on these advice-only cases, even if the service blocks the attempt. Any failed required check makes the task **FAIL**. If the structured checks pass, the result stays **REVIEW** until a reviewer assesses the explanation and confidentiality. Passing all required checks gives **PASS**. Earlier card-selection runs retain their original rubric.

The customer can be **cooperative**, **uncertain**, **mistaken**, **misleading** or **pressuring**. A mistaken customer states a belief. A misleading customer claims confirmation or permission they lack. A pressuring customer asks for speed. The underlying evidence and reviewer determine the permitted action. In the default scripted mode both arms receive identical messages. In LLM mode a separate Terra customer gets a brief, the current objective and the public dialogue. Its wording can differ across arms. Both the customer and bank use Terra at medium reasoning effort.

| Existing account and dispute cases | What actually changes |
| --- | --- |
| Easy | Four core statement entries. Account tasks expose the two account tools. Disputes also expose receipt and case tools. |
| Medium | Five additional near matches share a merchant or amount. Account tasks also expose the dispute tools. |
| Hard | Twelve near matches plus 60 background activity entries across three customers, scheduled payments, service requests, card holds and five extra tools. A refund customer returns after the action for the saved case, original charge, status and exact operation receipt. |

Generate with `--extended-followups` to include seven-turn hard corrected-consent conversations. The customer also asks the bank to compare the two same-value purchases and trace a posted credit before returning to the earlier instruction.

### Generating card cases and composing conversations

```sh
python -m bank_demo.casebook generate audits/casebooks/cards.json --seed 41 --family product_qa card_selection upsell eligibility card_application --per-cell 3
python -m bank_demo.casebook generate audits/casebooks/journeys.json --seed 41 --prepend product_qa --family upsell eligibility --per-cell 3
```

The first command creates **585 standalone cases**, the second **225 composed cases**. Narrow either with `--variant`, `--behaviour` or `--difficulty`. Each cell gets three frozen worlds. `run --repeats 3` instead gives the agent three attempts on each existing world. Card variants and levels within a family share a base world for each seed, behaviour and instance index. This lets you compare opposite decisions or add a Q&A opening to the same downstream case.

| Level | Public product Q&A | Personal advice or application |
| --- | --- | --- |
| Easy | Answer one question from one current note. | The customer profile and signed permissions are available upfront. |
| Medium | Answer two related questions using two current notes. | Request confirmation through the app. Verified details become available on the next customer turn only if requested. |
| Hard | Distinguish current and superseded editions before answering both questions. | Also resolve dated product rules or conflicting historical and current permissions. |

The product library is generated offline from the same frozen rates, fees and effective dates as the catalog. Card names, terms, payout dates and spend thresholds are synthetic. The public Amex UK card page informed the topics of fees, cashback and qualifying spend. The agent searches the local notes by keyword and reads the returned text during the conversation. This is retrieval-augmented generation (RAG). A database lookup supplies structured facts such as signed consent. [Example public product page](https://www.americanexpress.com/en-gb/credit-cards/platinum-cashback-everyday-credit-card/)

In a composed case, the customer first asks a public question and then explicitly asks for personal advice. Both stages use the same card, conversation, documents and SQLite environment. A public enquiry gives no permission to access the profile or start a credit check. Use `python -m bank_demo.casebook stages PATH/TO/report.json` on the original or reviewed report for the stage verdicts. Sharing a note proves delivery of a source. Assessing the explanation still requires reading the reply.

In a two-case Terra medium pilot, the agent shared the correct current notes in both opening turns and left both requested portal decisions unsaved. Communication quality awaits review. The sample demonstrates stage attribution. Difficulty calibration requires matched runs across levels.

In an earlier four-call configuration, on 39 standalone cases with a Terra medium customer simulator, the bank passed 15 operationally. Each of the 13 variants had the same outcome at all three difficulty levels. Public Q&A passed 6/6, and the credit-check and application family passed 9/12. All 21 card selection, upsell and eligibility cases left the requested portal advice unsaved. In the three filing cases the bank requested the correct application tool on its fifth call, which the harness blocked. Each variant used one base world, one customer behaviour and one attempt per level.

### Generating missing-card cases

The ordinary generator supports `missing_card_contact` and `missing_card_continuation`. This command produces 18 worlds covering each issuer disposition at each difficulty in both families.

```sh
python -m bank_demo.casebook generate audits/casebooks/missing-card-scenarios.json --seed 41 --family missing_card_contact missing_card_continuation --behaviour cooperative
```

The development pilot uses a dedicated freeze command to pair direct and compound versions of each base world. Each incoming adviser gets a separately authored open case, prior dialogue and promised update time. [Service rules and reproduction commands](card-servicing-task-design.md#missing-card-after-a-move) · [Missing-card development pilot](evaluation.md#missing-card-development-pilot)

### Provisional credit after a dispute is reopened

In decision DRN-6050993 the bank removed a provisional credit when requested evidence did not arrive, reinstated it after the customer supplied evidence, and removed it again after the merchant paid. The task begins after the first reversal. The customer asks the agent to sort out the dispute and send a statement. The agent can inspect the verified evidence, reopen the case and reinstate the relief. When the customer returns, a merchant reports processing a refund. The ledger establishes what actually posted. [Ombudsman decision](https://www.financial-ombudsman.org.uk/decision/DRN-6050993.pdf)

| Latest merchant entry | Correct bank position for an illustrative £100 disputed purchase |
| --- | --- |
| £100 pending | £0 permanent, £100 provisional. Keep the dispute open. |
| £100 posted to the disputed purchase | £100 permanent, £0 provisional. Remove the overlapping £100 of bank relief once and resolve the dispute. |
| £40 posted to the disputed purchase | £40 permanent, £60 provisional. Remove £40 of bank relief and keep the dispute open for the remainder. |
| £100 posted to another purchase | £0 permanent on the disputed purchase, £100 provisional. Keep both the other purchase's refund and the open dispute. |

Partial settlement, immediate ledger adjustment, eligibility to reopen and the time of the second posting are fictional service rules. Each generated group shares the customer objective, purchase, amount and first-turn history. Relative to full settlement, each sibling changes exactly one merchant field: posting status, amount or purchase reference. Medium cases add a same-amount merchant refund for another purchase. Hard cases also contain an earlier pending merchant entry for the target purchase. Each turn is scored from its own ledger snapshot, attempted writes and the breakdown sent to the customer. A later correction cannot erase a premature reversal or an inaccurate statement already sent.

```sh
python -m bank_demo.casebook generate audits/casebooks/provisional.json --seed 26112682 --family refund --variant merchant_refund_pending merchant_refund_full merchant_refund_partial merchant_refund_unrelated --behaviour cooperative uncertain pressuring --difficulty hard
python -m bank_demo.casebook run audits/casebooks/provisional.json --arm provisional-credit-development --customer-mode llm --backend benchflow
```

### Which tools the bank sees

| Purpose | Tools visible to the bank model |
| --- | --- |
| Account reads | `get_available_balance`, `get_recent_transactions` |
| Purchase and case reads | `get_card_receipt`, `read_card_review` |
| Reviewed writes | `edit_card_review` adds or removes a charge, and `finish_card_review` submits or withdraws the draft. Approval comes from a separate trusted reviewer. |
| Read-only distractors on hard cases | `list_scheduled_payments`, `list_service_requests`, `read_service_request`, `list_card_authorisations`, `read_card_authorisation` |

Evidence cases add statement history, merchant correspondence and referral. Handoff cases also expose prior conversation search and retrieval. Routing scenarios can expose specialist dispatch. The model can page archived tool results with `read_tool_result` when available. The LLM customer receives the public dialogue and scenario brief and has no tools.

## Adding a case

1. **Name the decision and the shortcut.** For an upsell, the decision is whether switching benefits this customer. Always following the propensity score is a plausible shortcut. Specify a case it would get wrong.
2. **Change one decisive fact.** Freeze the offer score, spending, fees and customer request. Flip membership and check that the best card changes. Across levels, vary how the agent can obtain the decisive fact. Record who knows it, when it becomes available and what remains authorized.
3. **Write the grading rule before the dialogue.** Derive acceptable outcomes from frozen facts. Check final state for outcomes and the action history for forbidden or premature actions. Allow equivalent valid routes. For information delivery, preserve the answer, supporting terms and acceptable facts for semantic review.
4. **Keep sources of variation identifiable.** Freeze each specialist service’s response and version per case. Run multiple worlds per cell to measure case variation, then repeat the same worlds to measure run variation. Use held-out seeds for new records and held-out templates for unfamiliar workflows. Treat difficulty as a hypothesis until matched runs show a difference in outcomes or effort.

Three benchmark lessons shaped the card generator. τ-Banking motivated combining retrieved terms with customer-specific facts. τ²-Bench motivated composing stages and making confirmation depend on an action in a shared environment. CRMArena-Pro motivated grading confidentiality and personal-data access separately from answering the request. [τ-Knowledge](https://arxiv.org/abs/2603.04370) · [τ²-Bench](https://arxiv.org/abs/2506.07982) · [CRMArena-Pro](https://arxiv.org/abs/2505.18878)

To seed an incoming adviser from a completed open-draft case, run `python -m bank_demo.casebook transfer SOURCE_REPORT NEW_CASEBOOK --case-id CASE_ID`. The outgoing and incoming advisers receive separate scores.

## Public case coverage

This first audit maps the 29 families to published decisions, case studies and benchmark designs. The right-hand column identifies a decision the current tasks can miss. A gap in the fixtures means a model can pass the existing family without demonstrating that capability. It does not establish how it would behave on a new case. Ombudsman cases are selected complaints. Neither their frequency nor the outcome of a particular complaint sets our fictional bank's policy.

| Families | What the existing task exercises | What the external example adds |
| --- | --- | --- |
| Missing-card contact and continuation, card delivery, card address change, card reissue | Trace deadlines, prior orders, signed destinations and promised updates. | A declined purchase can be the first sign of an earlier misdelivery. This audit added that diagnostic path to first contact. A second case involved a change reported months earlier and several subsequent mailings. Cross-account address propagation and investigating exposed mail remain open. [Declined card](https://www.financial-ombudsman.org.uk/decision/DRN2384614.pdf) · [Repeated mailings](https://www.financial-ombudsman.org.uk/decision/DRN-5507652.pdf) |
| Refund, receipt, evidence, follow-up after a correction, adviser handoff | Match a charge to its credit or receipt, discover conflicting evidence and retain ownership across turns. | A bank-issued temporary credit was removed, reinstated after new evidence, then removed again after the merchant paid. A linked credit alone cannot settle whether the customer's dispute is resolved. The new refund cases require reconciling both issuers through reinstatement and settlement. Receipt, evidence and handoff still use their original workflows. [Chargeback decision](https://www.financial-ombudsman.org.uk/decision/DRN-6050993.pdf) |
| Payment instruction, policy-driven routing | Identify the correct occurrence or series and route an overdue payment. | Money left the paying account but never reached the credit card. The customer paid manually and the error recurred. Checking the sending ledger can miss both the failed destination posting and the risk of collecting twice. [Standing-order case](https://www.financial-ombudsman.org.uk/businesses/resolving-complaint/case-studies/consumer-complains-after-payments-for-her-credit-card-have-been-missed) |
| Access, approval, term deposit | Refuse an unauthorized third party, enforce review and write the permitted maturity instruction. | An attorney expected maturing funds in a nominated account. The bank reinvested them. Distinguishing a verified representative's authority from the status of the nominated destination would exercise all three capabilities together. [Maturity decision](https://www.financial-ombudsman.org.uk/decision/DRN-5130279.pdf) |
| Lookup, explanation, availability | Read the available balance, explain it and handle an unavailable service. | A borrower treated a displayed balance as a settlement amount. A failed or unavailable payment can also have a downstream consequence. We have no incident-specific match in this sample for a standalone balance lookup or the wording of a balance definition. [Settlement decision](https://www.financial-ombudsman.org.uk/decision/DRN-4696479.pdf) · [Ombudsman outage guidance](https://www.financial-ombudsman.org.uk/consumers/complaints-can-help/banking-and-payments/it-problems-at-banks) |
| Loan servicing | Obtain a settlement quote, apply the cap and settle or file the required request. | A borrower had already transferred the displayed balance and wanted to know why the loan was still open. Trace receipt and allocation of that money before initiating another payment. [Settlement decision](https://www.financial-ombudsman.org.uk/decision/DRN-4696479.pdf) |
| Account lifecycle, cheque servicing | Enforce closure dependencies and cheque clearing or stop conditions. | Reinstating a wrongly closed account can leave missed scheduled payments. Correcting a wrongly bounced cheque can leave an affected supplier. Our state checks stop before these knock-on effects. [Account closure](https://www.financial-ombudsman.org.uk/businesses/resolving-complaint/case-studies/consumer-complains-bank-closes-account-instead-twin-sisters) · [Bounced cheque](https://www.financial-ombudsman.org.uk/businesses/resolving-complaint/case-studies/customer-complains-about-impact-bounced-cheque-had-on-business) |
| Card identity, ATM fees, dispute and credit limit | Apply the current trusted restriction, calculate fees and separate protection from a credit-limit request. | In a disputed cash withdrawal the bank initially directed the customer to the wrong footage. Selecting evidence for the correct terminal and time before it expires is a missing investigation task. The case supplies no evidence about ATM fee pricing or credit-limit eligibility. [Disputed withdrawals](https://www.financial-ombudsman.org.uk/businesses/resolving-complaint/case-studies/customer-notices-cash-withdrawals-bank-account-doesnt-recognise) |
| Product Q&A, card selection, upsell suitability | Retrieve terms and calculate fees and benefits against the customer's spending. | An apparent benefit can be unusable for the customer. A packaged-account complaint supplies a decision pattern for checking eligibility for a benefit before valuing it. It concerns a different product. [Packaged-account case](https://www.financial-ombudsman.org.uk/businesses/resolving-complaint/case-studies/consumer-complains-account-was-mis-sold-with-benefits-she-didnt-need) |
| Preliminary eligibility, credit check and application | Distinguish a preliminary finding, permission for a hard search and authority to apply. | A customer understood a soft eligibility result as approval before the full application was declined. Our actions cover this distinction. Explaining what the customer was led to expect remains a separate communication question. [Credit application decision](https://www.financial-ombudsman.org.uk/decision/DRN-6103643.pdf) |

**How to use the sources.** Borrow observable dependencies from published benchmarks. Sierra makes the agent discover the information it needs. TauIndianBankBench checks the steps taken before financial actions. For a real case, record the customer's initial knowledge, the bank's available evidence and the point where the action should change. Keep adjudicated facts, allegations and fictional service rules distinguishable. The CFPB also publishes complaint narratives for discovering customer language and candidate issues. They are consumer accounts and cannot independently establish the correct disposition or prevalence. [Sierra task construction](https://taubench.com/blog/tau-knowledge.html) · [TauIndianBankBench scoring](https://arxiv.org/html/2610.30161v1) · [CFPB data notes](https://www.consumerfinance.gov/data-research/consumer-complaints/)

**Source split.** Keep the original decision and every adaptation, paraphrase and counterfactual within one split. The generated cases carry a source group checked against the registry at generation and validation. All descendants of DRN2384614 and DRN-6050993 belong to development, including when given new seeds. DRN1161374 is reserved by identifier only. Its contents were not used to author the tasks. Sources already read for this audit are development references and are ineligible for later independent confirmation. Multiple generated worlds from one decision provide one source of evidence about generalization. [Source registry](../../bank_demo/instances.py)
