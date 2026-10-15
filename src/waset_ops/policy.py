"""Owner policy: business rules as versioned preferences, item exemptions and processing attestations.

Authority model (bondok-agent-migration/01-authority.md): the owner decides; rules are the owner's preferences. A
rule changes only by an owner command (``set_policy``: a new version, the previous value kept); one item is exempted
by ``waive_rule`` (the rule itself is unchanged) and the exemption is withdrawn by ``restore_rule``. An exemption is
not an attestation: waiving Topaz never records that Topaz was applied (``topaz_asset`` stays as it was), and the
board and Slack say "exempted by the owner".

Platform limits (``rules.PLATFORM``) are not policy and are reported as such.
"""
from __future__ import annotations

import sqlite3

from . import rules
from .core import OWNER, Command, Rejected
from .db import OPS_POLICY, audit, dumps, loads, next_counter

# Rules whose change can make waiting items ready (or not): their items are evaluated again in the same transaction.
READINESS_RULES = ('topaz_required', 'min_short_edge', 'max_bytes', 'story_max_seconds', 'story_trim_max_seconds',
                   'post_caption_approval', 'duplicate_video_block')
SLOT_RULES = ('story_slots', 'post_slots', 'late_window_minutes')


def _ensure(c):
    for stmt in OPS_POLICY.split(';'):
        if stmt.strip():
            c.execute(stmt)


class PolicyMixin:
    # ------------------------------------------------------------------ reading
    def policy(self, c) -> dict:
        """Effective owner policy now: the newest version of each rule whose effective time has come, else the
        default carried over from the rules in force before the migration."""
        out = rules.policy_defaults()
        try:
            rows = c.execute('SELECT p.name, p.value FROM ops_policies p WHERE p.effective <= ? AND p.version = '
                             '(SELECT MAX(version) FROM ops_policies q WHERE q.name = p.name AND q.effective <= ?)',
                             (self.now(), self.now())).fetchall()
        except sqlite3.OperationalError:          # store created by code that predates the policy tables
            return out
        for r in rows:
            if r['name'] in out:
                out[r['name']] = loads(r['value'], out[r['name']])
        return out

    def policy_changed_at(self, c, names) -> float:
        try:
            r = c.execute(f"SELECT MAX(effective) FROM ops_policies WHERE name IN ({','.join('?' * len(names))}) "
                          'AND effective <= ?', (*names, self.now())).fetchone()
        except sqlite3.OperationalError:
            return 0.0
        return float(r[0] or 0.0)

    def policy_value(self, name):
        with self.store.read() as c:
            return self.policy(c)[name]

    def overrides(self, c, item_id) -> list[dict]:
        try:
            rows = c.execute("SELECT * FROM ops_overrides WHERE item_id=? AND state='active' ORDER BY created",
                             (str(item_id),)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def waived(self, c, it: dict) -> set[str]:
        """Rules the owner exempted this item from. An exemption bound to a file version applies to that version
        only; one without a version applies to the item whatever file it has."""
        return {o['rule'] for o in self.overrides(c, it['item_id'])
                if o['rule'] != self.PUBLISH_BLOCK and (not o['asset_key'] or o['asset_key'] == it.get('asset_key'))}

    def topaz_confirmation(self, c, it) -> dict | None:
        """The recorded processing confirmation: which file version, who confirmed it, when, and whether it is the
        version selected now."""
        if not it.get('topaz_asset'):
            return None
        r = c.execute("SELECT id, actor, actor_kind, created FROM ops_commands WHERE item_id=? AND op='confirm_topaz' "
                      "AND state='completed' ORDER BY created DESC LIMIT 1", (it['item_id'],)).fetchone()
        return {'version': it['topaz_asset'], 'is_selected_version': it['topaz_asset'] == it.get('asset_key'),
                'file_name': it.get('file_name') if it['topaz_asset'] == it.get('asset_key') else None,
                'confirmed_by': ('the owner' if r and r['actor_kind'] == OWNER else
                                 'a board edit (Topazed)' if r and r['actor_kind'] == 'monday' else
                                 (r['actor'] if r else 'imported from the board')),
                'confirmed_at': self.iso_ts(r['created']) if r else None, 'command': r['id'] if r else None}

    def exemptions(self, c, it) -> list[dict]:
        return [{'id': o['id'], 'rule': o['rule'], 'file_version': o['asset_key'], 'by': o['set_by'],
                 'at': self.iso_ts(o['created']), 'reason': o['reason']}
                for o in self.overrides(c, it['item_id']) if o['rule'] != self.PUBLISH_BLOCK]

    def topaz_ok(self, c, it: dict, policy=None) -> bool:
        """Topaz is satisfied: confirmed by the owner for the selected version, not required by the rule, or
        waived for this item by the owner."""
        policy = policy or self.policy(c)
        if it.get('asset_key') and it.get('topaz_asset') == it['asset_key']:
            return True
        return not policy['topaz_required'] or 'topaz_required' in self.waived(c, it)

    def topaz_basis(self, c, it: dict) -> str | None:
        """How Topaz is satisfied (for explanations and the board): 'confirmed' | 'waived' | 'not_required'."""
        if it.get('asset_key') and it.get('topaz_asset') == it['asset_key']:
            return 'confirmed'
        if 'topaz_required' in self.waived(c, it):
            return 'waived'
        if not self.policy(c)['topaz_required']:
            return 'not_required'
        return None

    def policy_report(self) -> dict:
        """Every rule: current value, default, version, who set it and when; plus the platform limits."""
        with self.store.read() as c:
            pol = self.policy(c)
            try:
                hist = {r['name']: dict(r) for r in c.execute(
                    'SELECT * FROM ops_policies p WHERE version = (SELECT MAX(version) FROM ops_policies q '
                    'WHERE q.name = p.name)')}
            except sqlite3.OperationalError:
                hist = {}
            try:
                exempt = {}
                for r in c.execute("SELECT rule, item_id FROM ops_overrides WHERE state='active' AND rule!=?",
                                   (self.PUBLISH_BLOCK,)):
                    exempt.setdefault(r['rule'], []).append(r['item_id'])
            except sqlite3.OperationalError:
                exempt = {}
        out = []
        for name, spec in rules.POLICY.items():
            h = hist.get(name)
            out.append({'rule': name, 'meaning': spec['label'], 'value': pol[name], 'default': spec['default'],
                        'waivable_per_item': bool(spec.get('waivable')),
                        'version': h['version'] if h else 0, 'set_by': h['set_by'] if h else 'default',
                        'set_at': self.iso_ts(h['created']) if h else None,
                        'effective': self.iso_ts(h['effective']) if h else None,
                        'reason': h['reason'] if h else None,
                        'exempted_items': exempt.get(name, []),
                        'source': ({'kind': 'set', 'by': h['set_by'], 'reason': h['reason']} if h else
                                   dict(zip(('kind', 'evidence'), rules.SOURCES.get(name, ('unknown', None)))))})
        return {'rules': out, 'platform_limits': dict(rules.PLATFORM),
                'note': 'Rules are the owner\'s preferences: the owner can change any of them or exempt one item. '
                        'Platform limits are Instagram/transfer facts and cannot be changed by an instruction.'}

    # ------------------------------------------------------------------ owner commands
    def op_set_policy(self, c, cmd: Command):
        """Owner changes a rule (new version). The previous value is kept; items waiting on readiness rules are
        evaluated again now. Existing reservations are not moved by a slot-grid change unless the command says so
        (``apply_to_scheduled``); the result lists what the change affects."""
        _ensure(c)
        a = cmd.args
        name = str(a.get('rule') or '')
        try:
            value = rules.coerce_policy(name, a.get('value'))
        except rules.RuleError as e:
            raise Rejected(str(e), 'invalid_policy')
        current = self.policy(c)
        effective = self.now()
        if a.get('effective_at'):
            try:
                effective = rules.instant(a['effective_at']).timestamp()
            except (ValueError, rules.RuleError) as e:
                raise Rejected(f'Effective time is not a valid instant ({e})', 'invalid_policy')
        if current[name] == value and effective <= self.now():
            return {'rule': name, 'value': value, 'unchanged': True,
                    'message': f'{name} is already {value!r}; nothing changed.'}
        version = next_counter(c, 'policy:' + name)
        c.execute('INSERT INTO ops_policies(name,version,value,previous,effective,set_by,source,reason,created) '
                  'VALUES(?,?,?,?,?,?,?,?,?)', (name, version, dumps(value), dumps(current[name]), effective,
                                                cmd.actor, dumps(cmd.auth or {}), a.get('reason'), self.now()))
        audit(c, None, 'policy_set', cmd.actor, {'rule': name, 'version': version, 'value': value,
                                                  'previous': current[name], 'command': cmd.id})
        affected = []
        if effective <= self.now():
            affected = self._reapply(c, name, cmd)
        self.notify(c, f'policy:{name}:{version}', f'📏 Rule changed by the owner: {rules.POLICY[name]["label"]}: '
                    f'{current[name]!r} → {value!r} (version {version}).')
        return {'rule': name, 'version': version, 'value': value, 'previous': current[name],
                'effective': self.iso_ts(effective), 'affected': affected,
                'message': self._effect_text(name, affected, effective)}

    def _reapply(self, c, name, cmd) -> list[dict]:
        out = []
        if name in READINESS_RULES:
            rows = c.execute("SELECT item_id FROM ops_items WHERE publication='not_started' AND "
                             "owner_state='active' AND readiness IN ('blocked','checking','ready')").fetchall()
            for r in rows:
                it = self.item(c, r['item_id'])
                before = (it['readiness'], it.get('block_key'))
                if name == 'topaz_required' and (it.get('block_key') or '').startswith('topaz:'):
                    self._nudge(c, it['item_id'])
                res = self.evaluate(c, it['item_id'], cmd.actor)
                after = self.item(c, it['item_id'])
                if (after['readiness'], after.get('block_key')) != before:
                    out.append({'item_id': it['item_id'], 'name': it['name'], 'before': before[0],
                                'after': after['readiness'], 'reason': after.get('block_reason'),
                                'scheduled': res.get('scheduled')})
        elif name in SLOT_RULES:
            fmt = 'Story' if name == 'story_slots' else 'Post' if name == 'post_slots' else None
            pol = self.policy(c)
            q = 'SELECT r.item_id, r.slot, r.format, r.owner_pinned FROM ops_reservations r'
            for r in c.execute(q).fetchall():
                if fmt and r['format'] != fmt:
                    continue
                if name != 'late_window_minutes' and not r['owner_pinned'] and \
                        not rules.on_grid(r['format'], rules.instant(r['slot']), pol):
                    out.append({'item_id': r['item_id'], 'slot': r['slot'], 'kept': True,
                                'note': 'reserved before the change; kept (say so to move it)'})
        return out

    @staticmethod
    def _effect_text(name, affected, effective) -> str:
        if name in SLOT_RULES:
            kept = [a for a in affected if a.get('kept')]
            return ('New automatic scheduling uses the new value. ' +
                    (f'{len(kept)} existing reservation(s) are off the new grid and were kept as they are.'
                     if kept else 'No existing reservation is affected.'))
        if not affected:
            return 'No waiting item changed state.'
        return f'{len(affected)} item(s) changed state: ' + ', '.join(
            f"{a['name']} ({a['item_id']}) {a['before']}→{a['after']}" for a in affected[:10])

    def _nudge(self, c, item_id):
        obs = loads(self.item(c, item_id)['observed'], {}) or {}
        obs['_nudge'] = self.now()             # prepare it on the next preparation cycle
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), str(item_id)))

    # ------------------------------------------------------------------ owner directive: never publish this item
    PUBLISH_BLOCK = 'publish_block'

    def publish_block(self, c, item_id) -> dict | None:
        """The owner's standing directive that this item is never published (for example a test item that may still
        be scheduled). Enforced when work is listed, at the claim and at the commitment point."""
        if not self._has_overrides(c):
            return None
        r = c.execute("SELECT * FROM ops_overrides WHERE item_id=? AND rule=? AND state='active' ORDER BY created DESC "
                      'LIMIT 1', (str(item_id), self.PUBLISH_BLOCK)).fetchone()
        return {'id': r['id'], 'reason': r['reason'], 'by': r['set_by'], 'at': self.iso_ts(r['created'])} if r else None

    def op_publish_block(self, c, cmd: Command):
        _ensure(c)
        it = self.item(c, cmd.item_id)
        cur = self.publish_block(c, it['item_id'])
        if cur:
            return {'unchanged': True, 'directive': cur, 'message': f"{it['name']} is already never published."}
        oid = 'D-' + str(next_counter(c, 'override'))
        c.execute('INSERT INTO ops_overrides(id,item_id,rule,asset_key,state,reason,set_by,source,created) '
                  "VALUES(?,?,?,NULL,'active',?,?,?,?)", (oid, it['item_id'], self.PUBLISH_BLOCK,
                                                          cmd.args.get('reason'), cmd.actor, dumps(cmd.auth or {}),
                                                          self.now()))
        audit(c, it['item_id'], 'publish_blocked', cmd.actor, {'directive': oid, 'command': cmd.id,
                                                               'reason': cmd.args.get('reason')})
        self.project(c, it['item_id'])
        return {'directive': oid, 'item_id': it['item_id'],
                'message': f"{it['name']} will not be published while this directive stands; it can still be checked "
                           'and scheduled. Tell Bondok to allow publishing again to lift it.'}

    def op_publish_allow(self, c, cmd: Command):
        _ensure(c)
        it = self.item(c, cmd.item_id)
        cur = self.publish_block(c, it['item_id'])
        if not cur:
            return {'unchanged': True, 'message': f"{it['name']} has no do-not-publish directive."}
        c.execute("UPDATE ops_overrides SET state='revoked', revoked=?, revoked_by=? WHERE id=?",
                  (self.now(), cmd.actor, cur['id']))
        audit(c, it['item_id'], 'publish_allowed', cmd.actor, {'directive': cur['id'], 'command': cmd.id})
        self.project(c, it['item_id'])
        return {'revoked': cur['id'], 'message': f"{it['name']} may be published again at its reserved time."}

    def op_waive_rule(self, c, cmd: Command):
        """Owner exempts one item from a rule ("publish it without Topaz"). Recorded as an exemption, never as the
        fact the rule asks for. ``asset_key`` binds it to one file version (default: the item, any version)."""
        _ensure(c)
        it = self.item(c, cmd.item_id)
        rule = str(cmd.args.get('rule') or '')
        if rule not in rules.POLICY:
            raise Rejected(f'Unknown rule "{rule}". Rules that can be waived per item: ' + ', '.join(rules.WAIVABLE),
                           'invalid_policy')
        if rule not in rules.WAIVABLE:
            raise Rejected(f'"{rule}" applies to scheduling as a whole; change the rule itself (set_policy) or give '
                           'this item an explicit time instead', 'not_waivable')
        asset = cmd.args.get('asset_key') or None
        existing = [o for o in self.overrides(c, it['item_id']) if o['rule'] == rule]
        if any(o['asset_key'] in (None, asset) for o in existing):
            return {'rule': rule, 'item_id': it['item_id'], 'unchanged': True,
                    'message': f'{rule} is already waived for this item.'}
        oid = 'W-' + str(next_counter(c, 'override'))
        c.execute('INSERT INTO ops_overrides(id,item_id,rule,asset_key,state,reason,set_by,source,created) '
                  "VALUES(?,?,?,?,'active',?,?,?,?)", (oid, it['item_id'], rule, asset, cmd.args.get('reason'),
                                                       cmd.actor, dumps(cmd.auth or {}), self.now()))
        audit(c, it['item_id'], 'rule_waived', cmd.actor, {'rule': rule, 'override': oid, 'asset': asset,
                                                            'command': cmd.id})
        if rule == 'topaz_required':
            self._nudge(c, it['item_id'])
        if it.get('readiness') == 'blocked' and self._blocked_by(it, rule):
            # Only the block this rule caused is lifted; the facts are evaluated again (another rule still holds)
            self.update_item(c, it['item_id'], cmd.actor, f'{rule} waived by owner', readiness='checking',
                             block_kind=None, block_reason=None, block_key=None)
        res = self.evaluate(c, it['item_id'], cmd.actor)
        if self.item(c, it['item_id'])['readiness'] == 'ready' and not self.reservation(c, it['item_id']):
            res = {**res, **self.try_schedule(c, it['item_id'], cmd.actor)}   # a scheduling rule (caption) is lifted
        self.project(c, it['item_id'])
        return {'rule': rule, 'override_id': oid, 'item_id': it['item_id'], **res,
                'message': f"{rules.POLICY[rule]['label']}: waived for {it['name']} by the owner"
                           + (' (this file version only)' if asset else '') + '. The rule itself is unchanged.'
                           + (' This is an exemption, not a Topaz confirmation.' if rule == 'topaz_required' else '')}

    @staticmethod
    def _blocked_by(it, rule) -> bool:
        key, reason = it.get('block_key') or '', (it.get('block_reason') or '').lower()
        return {'topaz_required': key.startswith('topaz:'), 'duplicate_video_block': key.startswith('duplicate:'),
                'min_short_edge': 'short edge' in reason, 'require_collab': key == 'collab'}.get(rule, False)

    def op_restore_rule(self, c, cmd: Command):
        """Owner withdraws an exemption: the rule applies to the item again."""
        _ensure(c)
        it = self.item(c, cmd.item_id)
        rule = str(cmd.args.get('rule') or '')
        rows = [o for o in self.overrides(c, it['item_id']) if o['rule'] == rule]
        if not rows:
            return {'rule': rule, 'item_id': it['item_id'], 'unchanged': True,
                    'message': f'{rule} was not waived for this item.'}
        att = self.active_attempt(c, it['item_id'])
        c.execute("UPDATE ops_overrides SET state='revoked', revoked=?, revoked_by=? WHERE item_id=? AND rule=? "
                  "AND state='active'", (self.now(), cmd.actor, it['item_id'], rule))
        audit(c, it['item_id'], 'rule_restored', cmd.actor, {'rule': rule, 'command': cmd.id})
        res = self.evaluate(c, it['item_id'], cmd.actor)
        self.project(c, it['item_id'])
        note = ''
        if att and att['stage'] in ('committed', 'outcome_unknown'):
            note = ' A publication was already past the point of sending; it may already be in progress.'
        return {'rule': rule, 'item_id': it['item_id'], **res,
                'message': f"{rules.POLICY[rule]['label']}: applies to {it['name']} again." + note}

    # ------------------------------------------------------------------ explanation
    def explain(self, item_id) -> dict:
        """Why an item is where it is, which rule holds it and how the owner can change that."""
        with self.store.read() as c:
            it = self.item(c, item_id)
            pol = self.policy(c)
            waived = sorted(self.waived(c, it))
            reason = None
            with_rule = None
            if it['owner_state'] != 'active':
                reason, with_rule = f"the owner set it {it['owner_state']}", None
            elif it['readiness'] == 'blocked':
                reason = it['block_reason']
                bk = it.get('block_key') or ''
                with_rule = ('topaz_required' if bk.startswith('topaz:') else
                             'duplicate_video_block' if bk.startswith('duplicate:') else
                             'story_max_seconds' if bk.startswith('story_duration') else
                             'require_collab' if bk == 'collab' else
                             'min_short_edge' if 'short edge' in (reason or '') else None)
            elif it['format'] == 'Post' and it['caption_state'] != 'approved' and pol['post_caption_approval'] and \
                    'post_caption_approval' not in waived:
                reason, with_rule = 'the caption is not approved yet', 'post_caption_approval'
            res = self.reservation(c, it['item_id'])
            hold = self.hold_provenance(c, it) if it['owner_state'] == 'active' else None
            if hold and not reason:
                reason = f"{hold['reason'] or hold['kind']} ({hold['note']}"
                reason += f", {hold['set_at']})" if hold.get('set_at') else ')'
                last = hold.get('owner_last_time_instruction')
                if last and last.get('requested'):
                    reason += f". The owner's last time instruction ({last['at']}) asked for {last['requested']}"
            elif not reason and not res and it['requested_at'] and it['readiness'] == 'ready':
                taken = c.execute('SELECT item_id FROM ops_reservations WHERE account=? AND format=? AND slot=?',
                                  (rules.ACCOUNT, it['format'], it['requested_at'])).fetchone()
                if taken:
                    who = 'the owner' if it['requested_by'] not in ('board', None) else 'the board Publish at'
                    reason = (f"its requested time {it['requested_at']} (from {who}) is reserved by item "
                              f"{taken['item_id']}; moving that item needs the owner's approval of the swap proposal")
                    with_rule = 'occupied_slot_needs_approval'
        how = None
        if with_rule:
            spec = rules.POLICY[with_rule]
            how = (f'Owner options: confirm what the rule asks for, exempt this item ("waive {with_rule}")'
                   if spec.get('waivable') else 'Owner options: confirm what the rule asks for')
            how += f', or change the rule itself (now {pol[with_rule]!r}).'
        with self.store.read() as c:
            block = self.publish_block(c, it['item_id'])
            last_owner = c.execute('SELECT id, op, created FROM ops_commands WHERE item_id=? AND actor_kind=? '
                                   'ORDER BY created DESC LIMIT 1', (it['item_id'], OWNER)).fetchone()
        return {'item_id': it['item_id'], 'name': it['name'], 'readiness': it['readiness'],
                'owner_state': it['owner_state'], 'publication': it['publication'], 'hold': hold,
                'publish_block': block,
                'owner_last_instruction': {'op': last_owner['op'], 'at': self.iso_ts(last_owner['created']),
                                           'command': last_owner['id']} if last_owner else None,
                'scheduled': res['slot'] if res else None, 'reason': reason, 'rule': with_rule,
                'waived_rules': waived, 'how_to_change': how}

    def history(self, item_id, limit=40) -> dict:
        """Decision and execution log of one item (01-authority.md "سجل القرار"), newest first."""
        with self.store.read() as c:
            it = self.item(c, item_id)
            cmds = [{'at': self.iso_ts(r['created']), 'op': r['op'], 'by': r['actor'], 'kind': r['actor_kind'],
                     'state': r['state'], 'result': (loads(r['result'], {}) or {}).get('reason') or
                     (loads(r['result'], {}) or {}).get('message')}
                    for r in c.execute('SELECT * FROM ops_commands WHERE item_id=? ORDER BY created DESC LIMIT ?',
                                       (it['item_id'], limit))]
            attempts = [{'at': self.iso_ts(r['created']), 'stage': r['stage'], 'slot': r['slot'],
                         'media_id': r['media_id'], 'permalink': r['permalink'],
                         'evidence': loads(r['evidence'], {}) or {}}
                        for r in c.execute('SELECT * FROM ops_attempts WHERE item_id=? ORDER BY created DESC',
                                           (it['item_id'],))]
            exemptions = [dict(r) for r in c.execute('SELECT rule, asset_key, state, reason, set_by, created, '
                                                     'revoked, revoked_by FROM ops_overrides WHERE item_id=?',
                                                     (it['item_id'],))] if self._has_overrides(c) else []
        return {'item_id': it['item_id'], 'name': it['name'], 'decisions': cmds, 'attempts': attempts,
                'exemptions': exemptions}

    @staticmethod
    def _has_overrides(c) -> bool:
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='ops_overrides'").fetchone())
