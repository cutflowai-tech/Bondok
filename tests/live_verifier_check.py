"""Live check of the meaning verifier (not part of the offline suite): the REAL model rules on the 155 owner
sentences of tests/language/cases.py. Costs ~155 short model calls.

    OPENROUTER_API_KEY=... python3 tests/live_verifier_check.py [--out results.json]

Each case names the action the acting model would propose (as Bondok's bridge describes it) and whether the
owner's words should execute it. Prints the misses; exit status 1 if any.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'bondok'), str(ROOT / 'src'), str(Path(__file__).parent / 'language')]
from agent import Model  # noqa: E402
from verifier import Verifier  # noqa: E402

import cases  # noqa: E402

TOMORROW_9PM = 'tomorrow 21:00 Cairo'
ACTION = {
    'pause': 'Pause item {it} (it is not published until resumed)',
    'skip': 'Skip item {it} (it is not published)',
    'resume': 'Resume item {it} (it is scheduled again)',
    'request_publish': 'Schedule item {it} at the next free slot',
    'request_reschedule': 'Publish item {it} at {time}',
    'cancel_time': 'Remove the publication time of item {it} (it stays unscheduled)',
    'confirm_topaz': 'Record that Topaz processing is done for the selected file of item {it}',
    'waive_rule': 'Exempt item {it} from the rule "Topaz confirmation is required" (this item only, the rule '
                  'stays)',
    'restore_rule': 'Apply the rule "Topaz confirmation is required" to item {it} again (remove its exemption)',
    'set_policy': 'Change the rule {rule} to {value} for every item from now on',
    'publish_block': 'Never publish item {it} until the owner allows it (it can stay scheduled)',
    'publish_allow': 'Allow item {it} to be published again',
    'generic': '{generic}',
    'report_published': 'Record that the owner posted item {it} on the studio Instagram account',
}


# The action the acting model would propose for these sentences (tool + arguments), where the op's generic
# description does not fit: what is checked is the ruling on the right proposal.
OVERRIDE = {
    'resume LIP12 but not before tomorrow': 'Resume item LIP12, not before tomorrow (Cairo)',
    'انشر LIP12': 'Schedule item LIP12 at the next free slot',
    'انشر LIP12 بكرة': 'Schedule item LIP12 at the next free slot tomorrow (Cairo)',
    'لا انشره دلوقتي': 'Publish item LIP12 now (in about two minutes)',
    'مش عايز استنى انشر LIP12': 'Publish item LIP12 now (in about two minutes)',
    'مستني ايه؟ انشر LIP12': 'Publish item LIP12 now (in about two minutes)',
    'As I said, publish LIP12 tomorrow': 'Schedule item LIP12 at the next free slot tomorrow (Cairo)',
    'publish LIP12, no need to wait': 'Publish item LIP12 now (in about two minutes)',
    'انشر LIP12 ومتغيرش الكابشن': 'Schedule item LIP12 at the next free slot (caption unchanged)',
    'كنت عايز انشر LIP12 بكرة': 'Schedule item LIP12 at the next free slot tomorrow (Cairo)',
    'انقل LIP12 للساعة 10 مش 9': 'Publish item LIP12 at 22:00 Cairo today',
    'خليه 10 بالليل LIP12': 'Publish item LIP12 at 22:00 Cairo today',
    'انشره الساعة تسعة': 'Publish item LIP12 at 21:00 Cairo today',
    'ممكن تنقل LIP12 للساعة 10': 'Publish item LIP12 at 22:00 Cairo today',
    'انشره حتى لو مش 1080': 'Exempt item LIP12 from the rule "minimum short edge 1080 px" (this item only)',
    'ممكن تنشر LIP12 دلوقتي': 'Publish item LIP12 now (in about two minutes)',
    'انشر LIP12 ٩ مساءً': 'Publish item LIP12 at 21:00 Cairo today',
    'نزلته على حسابنا': 'Record that the owner posted item LIP12 on the studio Instagram account',
    'I posted it, no need to publish again': 'Record that the owner posted item LIP12 on the studio Instagram '
                                             'account',
}
CONTEXT = {   # the previous reply of the thread, where the sentence relies on it ("it")
    'نزلته على حسابنا': 'LIP12 (Calli 3) is ready and scheduled for tomorrow 21:00 Cairo.',
    'I posted it, no need to publish again': 'LIP12 (Calli 3) is ready and scheduled for tomorrow 21:00 Cairo.',
}
EITHER = {'انشر LIP12 ولا ايه',      # rhetorical or real question: refusing (or asking) is acceptable
          '> وقف LIP12'}             # Slack quote: refused by the bridge's deterministic rule before the verifier


def describe(op, text, args, note):
    if text in OVERRIDE:
        return OVERRIDE[text]
    it = 'ACC1' if 'ACC1' in text and 'LIP12' not in text else 'LIP12'
    time = args.get('time') or TOMORROW_9PM
    for h, said in (('21:30', 'today 21:30 Cairo'), ('4:17', 'today 16:17 Cairo'), ('الساعة 12 بالليل',
                                                                                    'tonight 00:00 Cairo')):
        if h in text and op == 'request_reschedule':
            time = said
    generic = args.get('generic') or ('Publish item LIP12 now' if any(w in text.lower() for w in (
        'انشر', 'publish', 'post', 'شوف', 'عامل')) else 'Delete item LIP12 from the board')
    return ACTION[op].format(it=it, time=time, rule=args.get('rule', 'topaz_required'),
                             value=args.get('value', 'false'), generic=generic)


def main():
    v = Verifier(Model(os.environ['OPENROUTER_API_KEY'], os.environ.get('OPENROUTER_MODEL') or 'openai/gpt-6.1-sol'))

    def one(c):
        op, text, quote, args, expected, note = c
        action = describe(op, text, args, note)
        try:
            r = v.check(text, CONTEXT.get(text, ''), action, args)
        except Exception as e:  # noqa: BLE001
            r = {'decision': 'error', 'reason': str(e)[:120]}
        got = 'exec' if r['decision'] == 'execute' else 'block'
        return {'op': op, 'text': text, 'action': action, 'expected': expected, 'got': got,
                'decision': r['decision'], 'reason': r.get('reason'), 'question': r.get('question'), 'note': note}
    with ThreadPoolExecutor(8) as pool:
        out = list(pool.map(one, cases.CASES))
    miss = [r for r in out if r['expected'] != r['got'] and r['text'] not in EITHER]
    if '--out' in sys.argv:
        Path(sys.argv[sys.argv.index('--out') + 1]).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for r in miss:
        print(f"MISS {r['op']:18} exp={r['expected']} got={r['decision']:8} | {r['text']} | {r['reason']}")
    print(f'{len(out) - len(miss)}/{len(out)} correct; asks: {sum(r["decision"] == "ask" for r in out)}')
    return 1 if miss else 0


if __name__ == '__main__':
    sys.exit(main())
