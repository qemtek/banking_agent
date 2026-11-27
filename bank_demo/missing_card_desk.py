"""Fictional issuer, carrier, instruction and investigation services for missing cards."""
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from functools import partial
from uuid import uuid4

from harness import Tool


POLICY = """Missing-card service policy
Use the authenticated card history to identify the affected attempt and any earlier investigation.
A timeout and an empty courier scan leave the result uncertain. Either a current issuer decision or
its certified decision archive can establish dispatch or final failure for that exact attempt.
'No record yet' is provisional. If one route is inconclusive, check the other issuer route.
Dispatch fixes the order and its original destination. The issuer gives the first trace-eligible
calendar date. Before then, give the date. On or after it, open one trace and give its reference.
A final failure means the previous attempt ended without an order. Check the latest effective
signed app instruction for this attempt. A signed replacement instruction authorizes one card at
its named destination. A move, an old instruction or consent in chat cannot change that destination.
If consent is absent or withdrawn, invite a fresh instruction and await the customer's response.
When the customer returns after an invitation, check the current app instruction for that attempt
before giving a status update. If it is signed, complete the authorized replacement. If it is
pending or withdrawn, leave the card unissued and explain the next step.
If both status routes are inconclusive, save an investigation using the attempt, the named owner,
next action and next update time on the service clock. The desk attaches the evidence actually checked.
For an existing investigation, inspect its saved evidence and check the current issuer result.
If uncertainty persists at or beyond the promised update, use the duty supervisor on the service clock
and give the new promised time. If still awaiting an app signature, retain ownership and invite it.
When dispatch or a replacement is resolved, close that attempt's open investigation.
Give the customer the trace date or the saved public reference and next step. A saved investigation
needs both a case reference and an update time. You can attach a service update to the conversation.
For a completed card replacement, the saved public reference to give the customer is the receipt field returned by the issuer.
"""

_MENU = {
    'read_card_history': ('Read card and adviser history, prior dialogue, open cases and the service clock.', ()),
    'read_card_issuer': ('Look up the current issuer decision for the previous attempt.', ('attempt',)),
    'read_card_archive': ('Read the independent issuer-certified decision archive for the attempt.', ('attempt',)),
    'read_card_courier': ('Look up carrier scans linked to the attempt.', ('attempt',)),
    'read_card_profile': ('Look up the current destination for future mail.', ()),
    'read_card_instruction': ('Read the signed app instruction history and latest effective decision.', ('attempt',)),
    'read_card_investigation': ('Read saved investigation evidence, ownership, deadline and status.', ('attempt',)),
    'invite_card_instruction': ('Invite the customer to sign a replacement instruction in the app.', ('attempt',)),
    'issue_card_replacement': ('Issue once against a current signed instruction and its destination.',
                               ('attempt', 'instruction_id', 'destination')),
    'open_card_trace': ('Save one missing-card trace against a dispatched order.', ('attempt', 'order_id')),
    'save_card_investigation': ('Save or update one investigation using the named owner, next_action '
                                'and followup_by from the service clock.', ('attempt', 'owner', 'next_action', 'followup_at')),
    'close_card_investigation': ('Close the resolved investigation matching the attempt and reference.',
                                 ('attempt', 'case_ref')),
    'deliver_card_update': ('Attach a customer-visible service update. kind accepts trace, trace_date, '
                            'replacement, investigation or consent.', ('attempt', 'kind')),
}
_STATUS_ROUTES = ('read_card_issuer', 'read_card_archive')
_DECISIONS = {'dispatched', 'final_failure'}
_STATE_TABLES = ('session', 'orders', 'traces', 'investigations', 'signatures', 'invitations', 'artifacts', 'journal')
_JSON_FIELDS = {'args', 'result', 'details', 'evidence'}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _row(row):
    if row is None:
        return None
    return {key: json.loads(row[key]) if key in _JSON_FIELDS else row[key] for key in row.keys()}


def _blocked(reason):
    return 'blocked', {'error': reason}


def render_reply(result, events):
    """Render only actual customer attachments from this turn. Replay uses the same audit."""
    parts = []
    for event in events:
        if event['event_type'] == 'tool.completed' and event['component'] == 'deliver_card_update':
            line = event['payload']['value'].get('customer_text')
            if line and line not in parts:
                parts.append(line)
    return replace(result, reply='\n\n'.join([*parts, result.reply])) if parts else result


class MissingCardDesk:
    def __init__(self, directory, case):
        self.w = w = case['world']
        self.path = directory / f'ledger-{uuid4().hex}.sqlite'
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE session(turn INTEGER NOT NULL, now TEXT NOT NULL);
                CREATE TABLE source_views(source TEXT, attempt TEXT, payload TEXT NOT NULL,
                    PRIMARY KEY(source,attempt));
                CREATE TABLE orders(attempt TEXT PRIMARY KEY, order_id TEXT UNIQUE, destination TEXT NOT NULL,
                    origin TEXT NOT NULL, receipt TEXT UNIQUE, instruction_id TEXT);
                CREATE TABLE traces(attempt TEXT PRIMARY KEY, order_id TEXT UNIQUE, receipt TEXT UNIQUE);
                CREATE TABLE investigations(attempt TEXT PRIMARY KEY, case_ref TEXT UNIQUE, owner TEXT NOT NULL,
                    next_action TEXT NOT NULL, followup_at TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL);
                CREATE TABLE invitations(attempt TEXT PRIMARY KEY, turn INTEGER NOT NULL);
                CREATE TABLE signatures(turn INTEGER NOT NULL, attempt TEXT NOT NULL, instruction_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL, destination TEXT, at TEXT NOT NULL, card TEXT NOT NULL, action TEXT NOT NULL);
                CREATE TABLE artifacts(turn INTEGER, attempt TEXT, kind TEXT, details TEXT NOT NULL,
                    PRIMARY KEY(turn,attempt,kind));
                CREATE TABLE journal(seq INTEGER PRIMARY KEY, turn INTEGER NOT NULL, operation TEXT NOT NULL,
                    args TEXT NOT NULL, result TEXT NOT NULL, outcome TEXT NOT NULL);
                CREATE TABLE turn_openings(turn INTEGER PRIMARY KEY, records TEXT NOT NULL);
                CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal BEGIN
                    SELECT RAISE(ABORT,'The operation journal is append-only'); END;
                CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal BEGIN
                    SELECT RAISE(ABORT,'The operation journal is append-only'); END;
            """)
            db.execute('INSERT INTO session VALUES(0,?)', (w['now'],))
            for source in ('issuer', 'archive', 'courier'):
                db.executemany('INSERT INTO source_views VALUES(?,?,?)',
                               [(source, attempt, _json(value)) for attempt, value in w[source].items()])
            if w['hidden_commit']:
                db.execute('INSERT INTO orders VALUES(?,?,?,?,NULL,NULL)',
                           (w['attempt'], w['order'], w['old_destination'], 'prior'))
            for inv in w['investigations'].values():
                db.execute('INSERT INTO investigations VALUES(?,?,?,?,?,?,?)',
                           (inv['attempt'], inv['case_ref'], inv['owner'], inv['next_action'],
                            inv['followup_at'], inv['status'], _json(inv['evidence'])))

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def snapshot(self):
        with self.connection() as db:
            return {name: [_row(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                    for name in _STATE_TABLES}

    def advance(self, turn):
        """Release frozen trusted events on their first eligible turn."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE session SET turn=?', (turn,))
            for event in self.w['app']:
                if event['turn'] != turn:
                    continue
                if event['requires_invite'] and not db.execute(
                    'SELECT 1 FROM invitations WHERE attempt=? AND turn<?', (event['attempt'], turn)
                ).fetchone():
                    continue
                db.execute('INSERT OR IGNORE INTO signatures VALUES(?,?,?,?,?,?,?,?)',
                           (turn, event['attempt'], event['id'], event['status'], event['destination'],
                            event['at'], event['card'], event['action']))
                db.execute('UPDATE session SET now=max(now,?)', (event['at'],))
            records = {name: [_row(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                       for name in _STATE_TABLES if name != 'journal'}
            db.execute('INSERT OR IGNORE INTO turn_openings VALUES(?,?)', (turn, _json(records)))

    def tools(self):
        return {name: Tool(description, partial(self._call, name), parameters=fields)
                for name, (description, fields) in _MENU.items()}

    def _call(self, name, session, **args):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if session.customer_id != 'alice':
                outcome, result = _blocked('Card service unavailable in this session.')
            elif 'attempt' in args and args['attempt'] not in self.w['issuer']:
                outcome, result = _blocked('No matching attempt in this customer session.')
            else:
                outcome, result = getattr(self, '_' + name)(db, **args)
            db.execute('INSERT INTO journal(turn,operation,args,result,outcome) SELECT turn,?,?,?,? FROM session',
                       (name, _json(args), _json(result), outcome))
            return result

    def _read_card_history(self, db):
        invs = [_row(r) for r in db.execute("SELECT * FROM investigations WHERE status='open' ORDER BY case_ref")]
        clock = {key: self.w[key] for key in ('now', 'owner', 'supervisor', 'followup_by')}
        clock['now'] = db.execute('SELECT now FROM session').fetchone()[0]
        clock['next_action'] = 'recheck_issuer'
        return 'read', {'history': self.w['history'], 'archived_dialogue': self.w['archived_dialogue'],
                        'investigations': invs, 'service_clock': clock}

    @staticmethod
    def _source(db, source, attempt):
        row = db.execute('SELECT payload FROM source_views WHERE source=? AND attempt=?', (source, attempt)).fetchone()
        return 'read', json.loads(row[0]) if row else {'attempt': attempt, 'status': 'unavailable'}

    def _read_card_issuer(self, db, attempt):
        return self._source(db, 'issuer', attempt)

    def _read_card_archive(self, db, attempt):
        return self._source(db, 'archive', attempt)

    def _read_card_courier(self, db, attempt):
        return self._source(db, 'courier', attempt)

    def _read_card_profile(self, db):
        return 'read', {'destination': self.w['profile_destination'], 'scope': 'future mail'}

    @staticmethod
    def _read_card_instruction(db, attempt):
        rows = db.execute('SELECT attempt,card,instruction_id AS id,status,destination,at,action FROM signatures '
                          'WHERE attempt=? ORDER BY at,rowid', (attempt,))
        events = [dict(row) for row in rows]
        return 'read', {'attempt': attempt, 'instructions': events, 'current': events[-1] if events else None}

    @staticmethod
    def _case(db, attempt):
        return _row(db.execute('SELECT * FROM investigations WHERE attempt=?', (attempt,)).fetchone())

    def _read_card_investigation(self, db, attempt):
        return 'read', {'attempt': attempt, 'investigation': self._case(db, attempt)}

    @staticmethod
    def _evidence(db, attempt):
        rows = db.execute("SELECT operation,result FROM journal WHERE outcome='read' "
                          "AND operation IN ('read_card_issuer','read_card_archive','read_card_courier') "
                          "AND args=? ORDER BY seq", (_json({'attempt': attempt}),))
        return {op: json.loads(result) for op, result in rows}

    def _decision(self, db, attempt):
        evidence = self._evidence(db, attempt)
        for source in _STATUS_ROUTES:
            view = evidence.get(source, {})
            if view.get('status') in _DECISIONS:
                return view
        return {}

    @staticmethod
    def _observed_instruction(db, attempt):
        row = db.execute("SELECT result FROM journal WHERE operation='read_card_instruction' "
                         "AND args=? AND outcome='read' ORDER BY seq DESC LIMIT 1", (_json({'attempt': attempt}),)).fetchone()
        return json.loads(row[0]) if row else {}

    def _authorizes_replacement(self, instruction):
        return (instruction is not None and instruction['status'] == 'signed'
                and instruction['card'] == self.w['card'] and instruction['action'] == 'replacement')

    def _invite_card_instruction(self, db, attempt):
        if self._decision(db, attempt).get('status') != 'final_failure':
            return _blocked('A final issuer failure is required before requesting replacement consent.')
        _, current = self._read_card_instruction(db, attempt)
        if current != self._observed_instruction(db, attempt):
            return _blocked('Check the current app instruction first.')
        if self._authorizes_replacement(current['current']):
            return _blocked('An effective signed instruction is already available.')
        created = db.execute('INSERT OR IGNORE INTO invitations SELECT ?,turn FROM session', (attempt,)).rowcount
        return ('committed' if created else 'replayed'), {'attempt': attempt, 'status': 'invited'}

    def _issue_card_replacement(self, db, attempt, instruction_id, destination):
        if self._decision(db, attempt).get('status') != 'final_failure':
            return _blocked('The issuer has not confirmed final failure for this attempt.')
        _, view = self._read_card_instruction(db, attempt)
        current = view['current']
        if (self._observed_instruction(db, attempt) != view or not self._authorizes_replacement(current)
                or (current['id'], current['destination']) != (instruction_id, destination)):
            return _blocked('No matching current signed replacement instruction.')
        prior = db.execute('SELECT * FROM orders WHERE attempt=?', (attempt,)).fetchone()
        if prior:
            if (prior['origin'], prior['instruction_id'], prior['destination']) != ('replacement', instruction_id, destination):
                return _blocked('An order already exists for this attempt.')
            return 'replayed', _row(prior)
        if attempt != self.w['attempt']:
            return _blocked('No replacement instruction for this card.')
        db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?)', (attempt, self.w['order'], destination,
                   'replacement', self.w['receipts']['replacement'], instruction_id))
        return 'committed', _row(db.execute('SELECT * FROM orders WHERE attempt=?', (attempt,)).fetchone())

    def _open_card_trace(self, db, attempt, order_id):
        decision = self._decision(db, attempt)
        if decision.get('status') != 'dispatched' or decision.get('order_id') != order_id:
            return _blocked('A linked issuer dispatch record is required.')
        if self.w['as_of'] < decision['trace_eligible_on']:
            return _blocked('The delivery window is still open.')
        if attempt != self.w['attempt']:
            return _blocked('This shipment is outside the missing-card enquiry.')
        created = db.execute('INSERT OR IGNORE INTO traces VALUES(?,?,?)',
                             (attempt, order_id, self.w['receipts']['trace'])).rowcount
        row = db.execute('SELECT * FROM traces WHERE attempt=?', (attempt,)).fetchone()
        return ('committed' if created else 'replayed'), _row(row)

    def _save_card_investigation(self, db, attempt, owner, next_action, followup_at):
        evidence = self._evidence(db, attempt)
        if not all(op in evidence for op in _STATUS_ROUTES) or self._decision(db, attempt):
            return _blocked('Check both issuer routes. A confirmed result has its own service procedure.')
        inv = self._case(db, attempt)
        if attempt != self.w['attempt'] or (inv and inv['status'] != 'open'):
            return _blocked('Investigation is unavailable for this attempt.')
        required_owner = self.w['owner']
        if inv:
            now = db.execute('SELECT now FROM session').fetchone()[0]
            required_owner = self.w['supervisor'] if now >= inv['followup_at'] else inv['owner']
        if (owner, next_action, followup_at) != (required_owner, 'recheck_issuer', self.w['followup_by']):
            return _blocked('Use the applicable named owner, issuer recheck action and update slot on the service clock.')
        checked = [{'source': source, 'status': value['status']} for source, value in sorted(evidence.items())]
        if inv and (inv['owner'], inv['next_action'], inv['followup_at'], inv['evidence']) == (
                owner, next_action, followup_at, checked):
            return 'replayed', inv
        db.execute("""INSERT INTO investigations VALUES(?,?,?,?,?,'open',?) ON CONFLICT(attempt) DO UPDATE SET
                      owner=excluded.owner,next_action=excluded.next_action,followup_at=excluded.followup_at,
                      evidence=excluded.evidence""", (attempt, self.w['case_ref'], owner, next_action,
                                                     followup_at, _json(checked)))
        return 'committed', self._case(db, attempt)

    def _close_card_investigation(self, db, attempt, case_ref):
        inv = self._case(db, attempt)
        if not inv or inv['case_ref'] != case_ref:
            return _blocked('No matching investigation.')
        decision = self._decision(db, attempt)
        status = decision.get('status')
        if status == 'dispatched':
            resolved = self.w['as_of'] < decision['trace_eligible_on'] or db.execute(
                'SELECT 1 FROM traces WHERE attempt=?', (attempt,)).fetchone()
        else:
            resolved = status == 'final_failure' and db.execute(
                "SELECT 1 FROM orders WHERE attempt=? AND origin='replacement'", (attempt,)).fetchone()
        if not resolved:
            return _blocked('The investigation still needs a confirmed service outcome.')
        if inv['status'] == 'closed':
            return 'replayed', inv
        db.execute("UPDATE investigations SET status='closed' WHERE attempt=?", (attempt,))
        return 'committed', self._case(db, attempt)

    def _deliver_card_update(self, db, attempt, kind):
        details = {'kind': kind, 'attempt': attempt}
        if kind in ('trace', 'replacement'):
            table = 'traces' if kind == 'trace' else 'orders'
            row = db.execute(f'SELECT * FROM {table} WHERE attempt=? AND receipt IS NOT NULL', (attempt,)).fetchone()
            if not row:
                return _blocked('No saved receipt matching this update.')
            details.update(reference=row['receipt'], order_id=row['order_id'])
            if kind == 'trace':
                message = f"Missing-card trace reference {row['receipt']} for order {row['order_id']}."
            else:
                details['destination'] = row['destination']
                message = f"Replacement order {row['order_id']} to {row['destination']}. Receipt {row['receipt']}."
        elif kind == 'investigation':
            inv = self._case(db, attempt)
            if not inv or inv['status'] != 'open':
                return _blocked('No open investigation matching this update.')
            details.update(reference=inv['case_ref'], followup_at=inv['followup_at'], owner=inv['owner'])
            message = f"Case {inv['case_ref']}. {inv['owner']} will update you by {inv['followup_at']}."
        elif kind == 'trace_date':
            decision = self._decision(db, attempt)
            if decision.get('status') != 'dispatched' or self.w['as_of'] >= decision['trace_eligible_on']:
                return _blocked('No future trace date matching this update.')
            details.update(order_id=decision['order_id'], destination=decision['destination'],
                           trace_eligible_on=decision['trace_eligible_on'])
            message = (f"Order {details['order_id']} was dispatched to {details['destination']}. "
                       f"A trace can be opened from {details['trace_eligible_on']}.")
        elif kind == 'consent':
            if not db.execute('SELECT 1 FROM invitations WHERE attempt=?', (attempt,)).fetchone():
                return _blocked('No app invitation matching this update.')
            _, app = self._read_card_instruction(db, attempt)
            if self._authorizes_replacement(app['current']):
                return _blocked('The app has received a signed instruction.')
            message = 'Please confirm your replacement and its delivery destination in the app.'
        else:
            return _blocked('Unknown update kind.')
        details['customer_text'] = message
        db.execute('INSERT INTO artifacts SELECT turn,?,?,? FROM session WHERE true '
                   'ON CONFLICT(turn,attempt,kind) DO UPDATE SET details=excluded.details',
                   (attempt, kind, _json(details)))
        return 'delivered', details
