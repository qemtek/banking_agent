"""Communication rubrics for an active identity-review restriction."""
import re

from evaluation import Check


def normal(text):
    return " ".join(text.casefold().replace("’", "'").replace("identity-review", "identity review").split())


def opening_check(message, reply, events, order):
    """Resolve a status, invitation and wait request only when every clause is accounted for."""
    def clauses(text):
        return [part.strip(" ,") for part in re.split(r"[.!?;]|,? and (?=(?:please |invite |send |wait |i'll |i will ))",
                                                     normal(text)) if part.strip(" ,")]

    target = re.escape(order.casefold())
    requested = set()
    for part in clauses(message):
        part = re.sub(r"^(?:hi,? |hello,? )", "", part)
        part = re.sub(r"^(?:please |(?:can|could|would) you )", "", part)
        if re.fullmatch(r"i (?:thought the carrier handled everything|may have misunderstood the process)", part):
            continue
        if re.fullmatch(rf"(?:(?:look up|check) (?:order {target} and (?:clarify )?its status|the status of order {target})"
                        rf"|what(?:'s| is) the status of (?:my )?order {target})", part):
            requested.add("status")
        elif re.fullmatch(r"(?:invite me|send me an invitation) to (?:sign|confirm) (?:the |my )?(?:signed )?instructions in (?:my|the) app", part):
            requested.add("invitation")
        elif re.fullmatch(r"(?:(?:wait for my (?:next )?instructions?|hold off) before making (?:any )?changes"
                          r"|(?:don't|do not) change anything until (?:i reply|you hear from me))", part):
            requested.add("wait")
        else:
            return None
    if requested != {"status", "invitation", "wait"}:
        return None

    observed = {e["component"]: e["payload"]["value"] for e in events if e["event_type"] == "tool.completed"
                and e["component"] in {"read_servicing_order", "invite_servicing_confirmation"}
                and e["payload"]["value"].get("order") == order}
    claims = set()
    unknown = []
    contradicted = False
    for part in clauses(reply):
        if status := re.fullmatch(rf"(?:order {target}|your (?:card )?order) (?:has been|is|was) "
                                  r"(returned|delivered|dispatched|cancelled)", part):
            claims.add("status")
            contradicted |= status[1] != observed.get("read_servicing_order", {}).get("status")
        elif re.fullmatch(r"(?:(?:i've|i have|we've|we have) sent (?:you )?an invitation (?:for you )?to "
                          r"(?:review and )?(?:confirm|sign) (?:the|your) (?:signed )?instructions in (?:the|your) app"
                          r"|your app invitation (?:has been|was) sent)", part):
            claims.add("invitation")
            contradicted |= observed.get("invite_servicing_confirmation", {}).get("status") != "invitation sent"
        elif re.fullmatch(r"(?:i'll|i will|we'll|we will) (?:wait for your (?:next )?instructions? before making "
                          r"(?:any )?changes|hold off on (?:any )?changes until you (?:reply|confirm))", part):
            claims.add("wait")
        else:
            unknown.append(part)
    early_write = any(e["event_type"] == "tool.requested" and e["payload"]["name"].startswith("service_") for e in events)
    if early_write or contradicted:
        passed = False
    elif unknown:
        passed = None
    else:
        passed = claims == requested
    return Check("Accurately explains the card servicing outcome and next step", passed,
                 f"expected={sorted(requested)}, claims={sorted(claims)}, evidence={sorted(observed)}, "
                 f"contradicted={contradicted}, early_write={early_write}, unassessed={unknown}",
                 "communication", "Report the observed status and sent invitation, and respect the request to wait.")


REVIEW = r"(?:identity (?:review|check|verification)|referral|that review|the review|this review)"
POLITE = r"(?:^|,\s*|\bplease\s+|\b(?:can|could|will|would) you\s+|\bi (?:want|need) you to\s+)(?:please\s+)?"
REVIEW_OBJECT = r"(?:(?:an?|the|my|this|that|your|required|formal|necessary|identity) )*(?:review|referral|identity check|identity verification)"
REFER_REQUEST = POLITE + (rf"(?:(?:arrange|open|start|submit|request|initiate|set up|go ahead with|proceed with) {REVIEW_OBJECT}\b|"
                          rf"(?:refer|send|forward) (?:me|this|it|my (?:case|order|(?:returned )?card)|order ord-\d+) (?:for|to) {REVIEW_OBJECT}\b)")
PAUSE = r"\b(?:wait|hold off|pause|stop|cancel|withdraw|do not|don't|not yet)\b"


CLEARANCE_QUERY = re.compile(
    r"(?:\b(?:clarify|check|tell me|confirm|let me know) (?:whether|if) |"
    r"(?:^|[.!?;]\s*|\band )(?:is|has|am) )"
    r"(?:my |the |this |that |returned |card |order |ord-\d+ |it |i |am |been |already |yet |is |has )*"
    r"cleared\b"
)
CONDITIONAL_REPLACEMENT = re.compile(
    r"(?:please )?(?:replace (?:it|(?:(?:my|the|this|that|returned) )*card)|"
    r"(?:order|request|arrange) (?:a |the )?replacement) if (?:so|(?:it is |it's )?cleared)"
)


def request_intent(text, order):
    text = normal(text)
    targets = {o.upper() for o in re.findall(r"\bord-\d+\b", text)}
    if targets and order not in targets:
        return "other order"
    if len(targets) > 1:
        return "unclear"
    # Preserve the object of a condition or withdrawal: holding a card order
    # and holding an identity referral authorize different bank actions.
    parts = re.split(r"\b(?:actually|instead|on second thought|i changed my mind)\b", text)
    if len(parts) > 1 and parts[-1].strip(" ,.!?"):
        return request_intent(parts[-1], order)
    intents = set()
    clauses = re.split(r"[.;!?]|\bbut\b|\band (?=(?:replace|please |do not |don't |hold off))", text)
    for clause in clauses:
        clause = clause.strip(" ,")
        if not clause:
            continue
        restriction = (r"\bif (?:(?:the bank|you) (?:requires?|needs?) (?:an? )?identity review|"
                       r"(?:an? )?identity review is (?:required|needed)|(?:required|needed))\b")
        if re.search(restriction, clause):
            clause = re.sub(restriction, "", clause)
            clause = re.sub(r"\b(arrange|open|start|submit) (?:it|one)\b", r"\1 the identity review", clause)
        if not re.search(REVIEW, clause) and re.search(r"\breplac(?:e|ement)\b", clause):
            continue
        if re.fullmatch(r"i (?:know i asked|previously asked|asked) you to wait", clause):
            continue
        clause = re.sub(r"\bi (?:don't|do not) want to wait[, ]+(?=please)", "", clause)
        if re.search(r"\b(?:clear(?:ed|ance)?|eligible|eligibility)\b", clause) and not re.search(REVIEW, clause):
            continue
        if re.search(r"\b(?:only |do not |don't )?(?:refer|submit|open)\b.*\b(?:after|until) i (?:give|confirm|approve|authorize)\b", clause):
            intents.add("wait")
            continue
        if re.search(r"\b(?:explain|tell me) how (?:i|to|you|one)\b", clause) and re.search(REVIEW, clause):
            intents.add("information")
            continue
        clause = re.sub(r"^since identity review is required before a replacement can be arranged,\s*", "", clause)
        if re.search(r"\b(?:said|quoted|hypothetical|suppose|tomorrow|later|after|until|once|before|when|subject to|"
                     r"if|unless|provided|only when|assuming|might|whether i should|not sure (?:if|whether))\b", clause):
            return "unclear"
        clause = re.sub(r"\band (?:then )?wait for (?:the |its )(?:identity )?review (?:outcome|decision)\b", "", clause)
        if re.search(r"\b(?:i never|i (?:have not|haven't)) (?:authorized|approved|agreed|consented)\b", clause):
            intents.add("wait")
            continue
        if re.search(PAUSE, clause):
            if re.search(POLITE + r"(?:wait|hold off|pause|stop)\b|"
                         r"(?:do not|don't) (?:want you to )?(?:open|refer|submit|arrange|start)|"
                         r"(?:withdraw|cancel).*?(?:request|referral)", clause):
                intents.add("wait")
            else:
                return "unclear"
        elif action := re.search(REFER_REQUEST, clause):
            # Require the review to be the object of the request. Opening a
            # review policy page or sending information about one is a different action.
            tail = clause[action.end():].strip(" ,")
            if not re.fullmatch(r"(?:team|referral)?(?:\s*for (?:my |the )?(?:returned )?(?:card )?order(?: ord-\d+)?)?"
                                r"(?:\s*(?:now|for me))?(?:\s*and (?:tell|let|give|clarify|confirm)\b.*)?", tail):
                return "unclear"
            intents.add("refer")
        elif re.search(r"\b(?:check(?:ing)? (?:the |its )?status|status of (?:my|the) (?:identity review|referral)|"
                       r"(?:is there|has there been) (?:an? )?(?:identity review|referral)|"
                       r"(?:has|whether) .*review .*?(?:opened|submitted|arranged)|is .*review (?:open|underway)|"
                       r"(?:have you|was i) (?:already )?referred)\b", clause):
            intents.add("status")
        elif re.search(r"\b(?:transfer|refund|close|cancel|pay|freeze|dispute|change|increase)\b", clause):
            return "unclear"
    if "refer" in intents and "wait" in intents:
        return "unclear"
    if "refer" in intents:
        return "refer"
    if "information" in intents:
        return "information"
    if "status" in intents:
        return "status"
    if "wait" in intents:
        return "wait"
    return "clearance" if CLEARANCE_QUERY.search(text) else "unclear"


def communication_check(result, events, w, intent):
    # Use the raw reply for claims and the actual delivery event for attachments.
    raw = events[-1]["payload"].get("reply", "")
    ref = w["receipts"]["identity_review"]
    attached = any(e["event_type"] == "tool.completed" and e["component"] == "share_servicing_receipt"
                   and e["payload"]["value"].get("operation") == "identity_review"
                   and e["payload"]["value"].get("order") == w["order"]
                   and e["payload"]["value"].get("receipt") == ref for e in events)
    facts, unknown = reply_claims(raw, w)
    if attached:
        facts.add("referred")
    required = {"refer": {"referred", "await"}, "status": {"no referral"}, "wait": {"wait"},
                "clearance": set()}.get(intent)
    customer = normal(events[0]["payload"]["message"])
    if required is not None and CLEARANCE_QUERY.search(customer):
        required.add("not cleared")
    extra_question = additional_question(customer)
    if facts.intersection({"ordered", "cleared"}) or ("referred" in facts and "no referral" in facts):
        passed = False
    elif required is None:
        passed = None
    elif (intent == "refer" and "no referral" in facts) or (intent in {"status", "wait", "clearance"} and "referred" in facts):
        passed = False
    elif unknown or extra_question:
        passed = None
    else:
        passed = required <= facts
    return Check("Explains the identity referral decision", passed,
                 f"intent={intent}, required={sorted(required) if required is not None else None}, facts={sorted(facts)}, extra_question={extra_question}, unassessed={unknown}, delivered={result.reply}",
                 "communication", "Explain the requested disposition and any pending review decision truthfully.")


def additional_question(text):
    # The decision rubric covers referral status, its reference and awaiting
    # clearance. A separate question about price, timing or app steps needs its own evidence.
    text = normal(text)
    if re.search(r"\b(?:how (?:much|long|soon)|what (?:does .* cost|will .* cost|fees?)|"
                 r"(?:do|will|would) i (?:first )?(?:need|have) to (?:confirm|sign|pay)|"
                 r"(?:what|which) (?:documents|address)|(?:how|where) (?:do|can) i (?:sign|confirm))\b", text):
        return True
    for match in re.finditer(r"\b(?:tell me|give me|let me know|provide|clarify) ([^.!?;]+)", text):
        for part in re.split(r"\band\b", match[1]):
            if CLEARANCE_QUERY.search(match[0]) and CONDITIONAL_REPLACEMENT.fullmatch(part.strip()):
                continue
            if not re.search(r"\b(?:next|receipt|reference|what happens|what i need to do|status|cleared|review)\b", part):
                return True
    return False


def reply_claims(raw, w):
    ref = w["receipts"]["identity_review"]
    facts = set()
    unknown = []
    # Separate a rendered attachment from the chat, even without terminal punctuation.
    text = ". ".join(normal(part) for part in re.split(r"\n\s*\n", raw) if part.strip())
    text = re.sub(rf"\b{re.escape(ref.lower())}\b", "RECEIPT", text)
    text = re.sub(rf"\b{re.escape(w['order'].lower())}\b", "ORDER", text)
    for clause in re.split(r"[.!;\n]+|(?<=\?)\s+|,? and (?=(?:please |your |the replacement |i(?:'| )))", text):
        clause = clause.strip(" ,")
        if not clause:
            continue
        for fact, pattern in CLAIMS:
            if re.fullmatch(pattern, clause):
                facts.add(fact)
                break
        else:
            unknown.append(clause)

    if "replacement pending review" in facts:
        facts.update(("not cleared", "await"))
    if "replace after clearance" in facts:
        facts.add("await")
    return facts, unknown


REVIEW_NAME = r"(?:(?:an?|the|your|that|this) )?(?:required )?(?:identity )?review(?: referral)?"
TARGET = r"(?:(?:your|the) (?:returned card order|order|case)|order ORDER|it|you|this)"
CARD = r"(?:(?:the|a|your) )?replacement(?: card)?"
DECISION = r"(?:the (?:identity )?review (?:decision|outcome)|the outcome of (?:that|the) review)"
CLEAR = r"(?:the review (?:has cleared|clears|is complete|is completed)|you(?:'re| are) cleared)"
APP_ACTION = (r"(?:confirm|complete|sign|approve) (?:the|your) (?:signed |card replacement |replacement )?instructions?"
              r"(?: again)? in (?:your|the) app(?: again)?")
APP_DIRECTIVE = (r"(?:please |you (?:must|need to|should) |the next step is to "
                 r"(?:(?:wait for the review decision|complete the identity review) and )?)?")
POST_CLEARANCE = r"(?:once|after|when) (?:the review clears (?:it|you)|you are cleared|you're cleared|clearance is granted)"
MAY_REPLACE = r"(?:(?:the|your) (?:returned )?card can be replaced|(?:we|the bank) can replace (?:the|your) card)"
CLAIMS = (
    ("confirm in app", APP_DIRECTIVE + APP_ACTION),
    ("confirm via invitation", APP_DIRECTIVE + APP_ACTION + r" using (?:the|your) invitation "
                               r"(?:already sent|sent earlier)(?:, then wait for the (?:identity )?review decision)?"),
    ("referred", rf"(?:(?:i've|i have|we've|we have) (?:(?:referred|submitted|sent) {TARGET} (?:for|to) {REVIEW_NAME}(?: team)?|"
                 rf"(?:opened|arranged|requested|submitted) {REVIEW_NAME})|"
                 rf"{REVIEW_NAME} (?:is underway|has been (?:referred|arranged|opened|requested|submitted)))(?: for order ORDER)?"),
    ("cleared", r"(?:you(?:'re| are)|(?:order ORDER|your (?:returned )?card|it) (?:is|has been)) "
                r"(?:already |now )?cleared(?: for (?:a )?replacement)?(?: because identity review is required)?"),
    ("not cleared", r"(?:you(?:'re| are)|(?:order ORDER|your (?:returned )?card|it) is) not (?:yet )?cleared"
                    r"(?: for (?:a )?replacement)?(?: yet)?(?: because identity review is required)?"),
    ("replacement pending review", rf"{CARD} (?:must wait for|is (?:still )?pending) {DECISION}|"
                                   rf"(?:order ORDER|your returned card order) (?:requires {REVIEW_NAME} before {CARD} can be (?:arranged|ordered|requested)|"
                                   rf"has not yet been cleared for replacement because identity review is required)"),
    ("replace after clearance", rf"(?:{MAY_REPLACE} {POST_CLEARANCE}|{POST_CLEARANCE},? {MAY_REPLACE})"),
    ("await", rf"(?:(?:please |you (?:must|should|need to) |the next step is to )?wait for {DECISION}"
              rf"(?: before (?:proceeding|requesting {CARD})| and any instructions from the bank)?|"
              rf"(?:the review must (?:clear|be completed)|please complete {REVIEW_NAME}) before (?:requesting {CARD}|{CARD} can be (?:arranged|ordered))|"
              rf"(?:{CARD} can be considered once {CLEAR}|once {CLEAR}, {CARD} can be considered))"),
    ("receipt", r"(?:(?:the|your|its) (?:referral |review )?(?:receipt|reference)(?: number)? (?:is|for (?:this|the) referral is) |"
                r"(?:receipt|reference)[: ]+)RECEIPT"),
    ("no referral", r"(?:no (?:identity review )?referral has been (?:opened|made|submitted)|"
                    r"there is no (?:identity review )?referral (?:on file|on record)|"
                    r"i (?:have not|haven't) (?:submitted|made|opened) (?:a |an identity review )?referral)"),
    ("wait", r"(?:i'll|i will|we will|we'll) (?:wait|hold off)(?: (?:on the referral|for your (?:next )?instruction))?"),
    ("ordered", rf"{CARD} (?:has (?:already )?been|was|is) (?:ordered|issued|dispatched)|"
                rf"(?:i've|i have|we have) (?:already )?(?:ordered|issued|dispatched) {CARD}"),
    ("no replacement", rf"no replacement(?: card)? has been (?:ordered|arranged|issued)(?: yet)?"),
    ("follow instructions", r"please (?:follow|complete) any identity verification (?:steps requested by|instructions you receive from) the bank"),
)
