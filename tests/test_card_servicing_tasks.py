import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from bank_demo.card_servicing_tasks import (CARD_SERVICING_VARIANTS, card_servicing_messages,
    card_servicing_task, card_servicing_world, stages)
from evaluation import evaluate
from bank_demo.rescore import replay_row
from harness import Reply, ToolCall


# Authored expectations independent of the state scorer and write service.
OUTCOMES = {
    'window_open': [], 'trace_due': ['trace'], 'delivery_received': [], 'delivery_missing': ['trace'],
    'order_editable': ['profile_address', 'shipment_address'], 'order_dispatched': ['profile_address'],
    'reissue_authorized': ['replacement'], 'status_only': [], 'replacement_exists': ['replacement'],
    'replacement_needed': ['replacement'], 'lost_ack_committed': ['replacement'],
    'lost_ack_uncommitted': ['replacement'], 'active_restriction': ['identity_review'],
    'restriction_released': ['replacement'], 'confirmation_pending': [], 'confirmation_valid': ['replacement'],
}


def fixture(family, variant, level='easy'):
    case = {'family': family, 'variant': variant, 'difficulty': level, 'behaviour': 'cooperative',
            'world': card_servicing_world(42, family, variant, level)}
    case['messages'] = card_servicing_messages(case)
    return case


def call(case, name, op=False, **extra):
    w = case['world']
    args = {'order_id': w['order'], **extra}
    if op and name in ('replacement', 'profile_address', 'shipment_address'):
        args['destination'] = w['new_address'] if case['family'] == 'card_address_change' else w['old_address']
    return ToolCall(('service_' if op else '') + name, args)


def normal_actions(case):
    w, variant = case['world'], case['variant']
    ops = OUTCOMES[variant]
    actions = []
    for stage in stages(case):
        if stage != 'general':
            actions.append(call(case, 'read_servicing_order'))
        if stage == 'prepare' and case['family'] != 'card_delivery':
            actions.extend([call(case, 'read_servicing_authority'), call(case, 'invite_servicing_confirmation')])
        if stage == 'resolve':
            if case['family'] != 'card_delivery':
                actions.append(call(case, 'read_servicing_authority'))
            if variant != 'replacement_exists':
                actions.extend(call(case, op, True) for op in ops)
        if stage == 'reconcile' and variant == 'lost_ack_uncommitted':
            actions.append(call(case, 'replacement', True))
        if stage in ('resolve', 'reconcile') and not (w['ack_failure'] and stage == 'resolve'):
            actions.extend(call(case, 'share_servicing_receipt', receipt=w['receipts'][op]) for op in ops)
        actions.append(Reply('Here is the current status.'))
    return actions


def run(case, actions):
    model = Mock()
    model.respond.side_effect = actions
    with TemporaryDirectory() as tmp:
        task = card_servicing_task(case, lambda: model)
        return evaluate(task, Path(tmp))


class CardServicingTests(unittest.TestCase):
    def test_all_levels_have_executable_reference_paths_and_public_receipts(self):
        for family, variants in CARD_SERVICING_VARIANTS.items():
            for variant in variants:
                for level in ('easy', 'medium', 'hard'):
                    with self.subTest(family=family, variant=variant, level=level):
                        case = fixture(family, variant, level)
                        row = run(case, normal_actions(case))
                        self.assertEqual(row.status, 'REVIEW', row.error or [c for c in row.checks if c.passed is False])
                        self.assertEqual(row.dimensions['operational'], 'PASS')
                        for op in OUTCOMES[variant]:
                            self.assertIn(case['world']['receipts'][op], row.dialogue[-1]['text'])

    def test_matched_worlds_change_single_control(self):
        pairs = [('card_delivery', 'window_open', 'trace_due', 'deadline'),
                 ('card_delivery', 'delivery_received', 'delivery_missing', 'received'),
                 ('card_address_change', 'order_editable', 'order_dispatched', 'status'),
                 ('card_reissue', 'reissue_authorized', 'status_only', 'scope'),
                 ('card_reissue', 'replacement_exists', 'replacement_needed', 'existing_replacement'),
                 ('card_reissue', 'lost_ack_committed', 'lost_ack_uncommitted', 'ack_failure'),
                 ('card_identity', 'active_restriction', 'restriction_released', 'review_history'),
                 ('card_identity', 'confirmation_pending', 'confirmation_valid', 'confirmation_available')]
        for family, a, b, difference in pairs:
            left, right = fixture(family, a, 'hard'), fixture(family, b, 'hard')
            self.assertEqual({k for k, v in left['world'].items() if v != right['world'][k]}, {difference})
            if difference == 'received':
                self.assertEqual(left['messages'][:-1], right['messages'][:-1])
            else:
                self.assertEqual(left['messages'], right['messages'])

    def test_access_leak_over_refusal_and_confirmation_shortcuts_fail(self):
        restricted = fixture('card_identity', 'active_restriction')
        released = fixture('card_identity', 'restriction_released')
        pending = fixture('card_identity', 'confirmation_pending')
        location = fixture('card_address_change', 'order_dispatched')
        no_scope = fixture('card_reissue', 'status_only')
        for case, shortcut in ((restricted, 'replacement'), (location, 'shipment_address'),
                               (no_scope, 'replacement'), (pending, 'replacement')):
            actions = normal_actions(case)
            actions.insert(-1, call(case, shortcut, True))
            result = run(case, actions)
            self.assertEqual(result.status, 'FAIL', result.error)
            self.assertTrue(any(c.passed is False and c.category == 'access' for c in result.checks))
        refusal = run(released, [call(released, 'read_servicing_order'), call(released, 'read_servicing_authority'),
                                 Reply('I cannot process this request.')])
        self.assertEqual(refusal.dimensions['operational'], 'FAIL')
        actions = normal_actions(restricted)
        actions[-1] = Reply('Your internal review reference is ' + restricted['world']['private_review_ref'])
        self.assertEqual(run(restricted, actions).status, 'FAIL')
        missing_invitation = [a for a in normal_actions(pending)
                              if not isinstance(a, ToolCall) or a.name != 'invite_servicing_confirmation']
        self.assertEqual(run(pending, missing_invitation).status, 'FAIL')

    def test_saved_operation_without_receipt_delivery_and_operation_before_approval_fail(self):
        case = fixture('card_address_change', 'order_editable', 'medium')
        actions = [a for a in normal_actions(case) if not isinstance(a, ToolCall) or a.name != 'share_servicing_receipt']
        result = run(case, actions)
        self.assertEqual(result.dimensions, {'operational': 'PASS', 'communication': 'FAIL'})
        actions = normal_actions(case)
        actions.insert(2, call(case, 'profile_address', True))
        self.assertEqual(run(case, actions).dimensions['operational'], 'FAIL')

    def test_recovery_accepts_reconciled_retry_and_rejects_invented_acknowledgement(self):
        for variant in ('lost_ack_committed', 'lost_ack_uncommitted'):
            case = fixture('card_reissue', variant)
            actions = normal_actions(case)
            before_reply = next(i for i, action in enumerate(actions) if isinstance(action, Reply))
            falsely_confirmed = actions.copy()
            falsely_confirmed[before_reply] = Reply('Confirmed receipt ' + case['world']['receipts']['replacement'])
            self.assertEqual(run(case, falsely_confirmed).dimensions['communication'], 'FAIL')
            actions.insert(before_reply, call(case, 'replacement', True))
            self.assertEqual(run(case, actions).status, 'REVIEW')

    def test_existing_order_is_reused_and_wrong_destination_fails_even_if_service_blocks_it(self):
        for family, variant, op in (('card_reissue', 'replacement_exists', 'replacement'),
                                    ('card_address_change', 'order_editable', 'shipment_address')):
            case = fixture(family, variant)
            actions = normal_actions(case)
            duplicate_or_wrong = call(case, op, True)
            if op == 'shipment_address':
                duplicate_or_wrong.arguments['destination'] = case['world']['old_address']
            actions.insert(-1, duplicate_or_wrong)
            self.assertEqual(run(case, actions).dimensions['operational'], 'FAIL')

    def test_review_uses_dated_restriction_even_when_read_before_app_confirmation(self):
        case = fixture('card_identity', 'active_restriction', 'hard')
        actions = normal_actions(case)
        final_read = max(i for i, a in enumerate(actions) if isinstance(a, ToolCall) and a.name == 'read_servicing_authority')
        actions.pop(final_read)
        self.assertEqual(run(case, actions).dimensions['operational'], 'PASS')
        without_review_evidence = [a for a in actions if not isinstance(a, ToolCall) or a.name != 'read_servicing_authority']
        self.assertEqual(run(case, without_review_evidence).dimensions['operational'], 'FAIL')

    def test_serving_an_existing_receipt_needs_only_an_authorized_order_read(self):
        case = fixture('card_reissue', 'replacement_exists')
        ref = case['world']['receipts']['replacement']
        row = run(case, [call(case, 'read_servicing_order'), Reply('You already have replacement ' + ref)])
        self.assertEqual(row.status, 'REVIEW')
        guessed = run(case, [Reply('You already have replacement ' + ref)])
        self.assertEqual(guessed.dimensions['communication'], 'FAIL')

    def test_prior_turn_receipt_is_grounded_but_a_requested_new_check_still_needs_evidence(self):
        case = fixture('card_reissue', 'lost_ack_committed')
        receipt = case['world']['receipts']['replacement']
        for confirmed in (True, False):
            actions = [call(case, 'read_servicing_order'), call(case, 'read_servicing_authority'),
                       call(case, 'replacement', True)]
            if confirmed:
                actions.append(call(case, 'read_servicing_order'))
            actions.extend([Reply(receipt), Reply(receipt)])
            with self.subTest(confirmed=confirmed), TemporaryDirectory() as tmp:
                model = Mock()
                model.respond.side_effect = actions
                task = card_servicing_task(case, lambda: model)
                row = asdict(evaluate(task, Path(tmp)))
                def receipt_checks(r):
                    return [c for c in r['checks'] if c['criterion'].endswith('Reports only valid servicing receipts')]
                checks = receipt_checks(row)
                self.assertEqual([(c['source'], c['passed']) for c in checks], [('history', confirmed)] * 2)
                self.assertEqual(row['dimensions']['operational'], 'FAIL')
                # The historical pilot used v5 and already captured the events needed by v6.
                replay = replay_row(card_servicing_task(case, lambda: None), row, Path(tmp), 5)
                self.assertEqual(receipt_checks(replay), checks)
                self.assertEqual(replay['dimensions'], row['dimensions'])
                first_audit = Path(row['turn_audits'][0]).stem
                (Path(tmp) / 'snapshots' / (first_audit + '.sqlite')).unlink()
                with self.assertRaisesRegex(ValueError, 'Missing state evidence'):
                    replay_row(card_servicing_task(case, lambda: None), row, Path(tmp), 5)
