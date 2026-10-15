"""Build the owner-approved acceptance script (bondok/acceptance.py) from the fixtures file (server only).

    python3 tests/acceptance_script.py <fixtures.json> <out dir> <script id> <not_before UTC ISO>

Prints the sha256 prefix the owner puts in the trigger message. Steps are natural owner commands, one Slack message
each, grouped in threads so "this video" has the context a real conversation gives it.
"""
import hashlib
import json
import os
import sys
from pathlib import Path


def steps(fx):
    ids = {int(k): v for k, v in fx['items'].items()}
    root = fx['root']
    url9 = ids[9]['folder_url']
    alt7 = fx['alt7']['url']
    all13 = ' و'.join(f'ACC{n}' for n in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14))
    q = 'ACC15'
    d1, d2 = '19 نوفمبر 2026', '26 نوفمبر 2026'
    owner_plan = [   # the owner's own 14-step scenario (2026-10-16), verbatim with the item and dates filled in
        {'thread': 'Q', 'wait_item': q, 'timeout': 1500,
         'text': f'هنختبر على العنصر {q} فقط. متنفذش نشر فعلي له خلال الاختبار، حتى لو حجزناله موعد. سجّل ده كتوجيه مني.'},
        {'thread': 'Q', 'text': 'قولّي القواعد اللي بتمنع تجهيز أو جدولة العنصر ده، ومصدر كل قاعدة، وإيه اللي أقدر أغيّره.'},
        {'thread': 'Q', 'text': f'أنا عملت Topaz للنسخة الحالية من فيديو {q}. سجّل تأكيدي حتى لو اسم الملف مفيهوش كلمة Topaz.'},
        {'thread': 'Q', 'text': 'اعرض تأكيد المعالجة اللي سجلته: على أنهي نسخة، ومين أكده، وإمتى؟'},
        {'thread': 'Q', 'text': f'استثني {q} من شرط Topaz. الاستثناء للعنصر ده بس، متغيّرش القاعدة العامة.'},
        {'thread': 'Q', 'text': 'ورّيني الاستثناء والقاعدة العامة، وأكد إن باقي العناصر متأثرتش.'},
        {'thread': 'Q', 'text': 'الغي الاستثناء اللي عملناه، لكن احتفظ بتأكيدي إن المعالجة اتعملت.'},
        {'thread': 'Q', 'wait_ready': q, 'timeout': 1500,
         'text': f'احجز لـ{q} يوم {d1} الساعة 4 العصر بتوقيت القاهرة. ده موعد صريح مني، وتوجيه عدم النشر الفعلي أثناء '
                 'الاختبار لسه قائم. لو فيه تعارض قولّي، متختارش موعد تاني من نفسك.'},
        {'thread': 'Q', 'text': 'اعرض الموعد المطلوب والموعد المحجوز ومصدر القرار، وقولّي هل منع النشر التجريبي لسه قائم.'},
        {'thread': 'Q', 'wait_seconds': 90,
         'text': 'حدّث بيانات العنصر من Monday دلوقتي، وبعد التحديث اعرض نفس المعلومات وقولّي لو أي حاجة اتغيرت وليه.'},
        {'thread': 'Q', 'text': f'غيّر الموعد إلى يوم {d2} الساعة 6 مساءً بتوقيت القاهرة. ده بيستبدل طلبي السابق للموعد فقط، '
                                'وباقي تعليماتي زي ما هي.'},
        {'thread': 'Q', 'wait_seconds': 90,
         'text': 'اقرأ البورد تاني، وأكد إن الموعد الجديد هو الساري وإن القديم مش هيرجع من المزامنة.'},
        {'thread': 'Q', 'text': f'الغي حجز {q}، وخليه من غير موعد لحد ما أطلب منك جدولة جديدة. ده إلغاء صريح مني.'},
        {'thread': 'Q', 'wait_seconds': 90,
         'text': 'بعد تحديث البورد، قولّي ليه العنصر مش متجدول، واعرض مصدر الإلغاء وآخر أمر مني. متحجزلوش تلقائيًا.'},
    ]
    s = owner_plan + [
        # safety first: none of the test items may be scheduled automatically (bulk > 10, no confirmation)
        {'thread': 'S', 'wait_item': 'ACC14', 'timeout': 1500,
         'text': f'الغي جدولة عناصر الاختبار {all13}، ومتتنشرش لحد ما أقول'},
        # Topaz confirmation on a file whose name has no Topaz (+ Slack redelivery of the same message)
        {'thread': 'T1', 'wait_item': 'ACC1', 'text': 'ايه وضع ACC1؟'},
        {'thread': 'T1', 'text': 'أنا عملت توباز للفيديو ده', 'redeliver': True},
        # why is it blocked
        {'thread': 'T12', 'text': 'ايه وضع ACC12؟'},
        {'thread': 'T12', 'text': 'ليه الفيديو ده واقف؟'},
        # instructions inside notes / a file name are not commands; a plan is not a statement
        {'thread': 'T13', 'text': 'ايه وضع ACC13؟'},
        {'thread': 'T13', 'text': 'هعمل توباز للفيديو ده بكرة'},
        # exemption: publish without Topaz, its effect on readiness, a question, then stop it
        {'thread': 'T2', 'text': 'ايه وضع ACC2؟'},
        {'thread': 'T2', 'text': 'انشره من غير توباز'},
        {'thread': 'T2', 'text': 'هو اتنشر؟'},
        {'thread': 'T2', 'wait_ready': 'ACC2', 'timeout': 1500, 'text': 'ايه وضعه دلوقتي؟'},
        {'thread': 'T2', 'text': 'وقفه'},
        # an exemption for this item only
        {'thread': 'T3', 'text': 'ايه وضع ACC3؟'},
        {'thread': 'T3', 'text': 'خلّي توباز مش شرط للعنصر ده بس'},
        # the rule for a named set of test items
        {'thread': 'TG', 'text': 'خلّي توباز مش شرط لعناصر الاختبار ACC4 وACC5 وACC6'},
        # a lasting global rule change and its restoration (a rule with no effect on live items now)
        {'thread': 'TR', 'text': 'من النهارده خلي التأخير المسموح في النشر 3 ساعات بدل ساعتين'},
        {'thread': 'TR', 'text': 'رجّع التأخير المسموح في النشر ساعتين زي ما كان'},
        # scheduling at a Cairo time, change, cancel, pause
        {'thread': 'T1', 'wait_ready': 'ACC1', 'timeout': 1500, 'text': 'جدوله بكرة الساعة 9 مساءً'},
        {'thread': 'T1', 'text': 'غيّر معاده لبكرة الساعة 11 بالليل'},
        {'thread': 'T1', 'text': 'الغي الجدولة'},
        {'thread': 'T1', 'text': 'وقفه'},
        # caption, notes, code (+ redelivery)
        {'thread': 'T8', 'text': 'ايه وضع ACC8؟'},
        {'thread': 'T8', 'text': 'خلي الكابشن بتاعه: Bondok acceptance caption 2026 — not for publishing'},
        {'thread': 'T8', 'text': 'حط في الملاحظات بتاعته: مراجعة قبول بندق', 'redeliver': True},
        {'thread': 'T8', 'text': 'غيّر الكود بتاعه لـ ACC80'},
        {'thread': 'T8', 'text': 'اكتب له كابشن مقترح قصير'},
        {'thread': 'T8', 'text': 'اعتمد الكابشن المقترح'},
        # create, then archive
        {'thread': 'T9', 'text': f'اعمل ستوري جديدة اسمها Acceptance 9 Bondok check بكود ACC9 وفولدرها {url9}'},
        {'thread': 'T9', 'wait_item': 'ACC9', 'timeout': 1500, 'text': 'أرشف Acceptance 9 Bondok check'},
        # permanent delete
        {'thread': 'T10', 'text': 'امسح ACC10 من البورد نهائي'},
        # files: list, rename + move, delete
        {'thread': 'T11', 'text': 'اعرض ملفات ACC11'},
        {'thread': 'T11', 'text': f'غيّر اسم الملف Acceptance 11 Bondok check v0.mp4 لـ Acceptance 11 spare v0.mp4 '
                                  f'وانقله لفولدر {root}/Moved'},
        {'thread': 'T11', 'wait_seconds': 90,
         'text': f'امسح الملف {root}/Moved/Acceptance 11 spare v0.mp4'},
        # publish now on an item that is not ready (it stays blocked: never sent), ambiguity, delete
        {'thread': 'T14', 'text': 'انشر ACC14 دلوقتي'},
        {'thread': 'T14', 'text': 'شيل ACC14'},
        {'thread': 'T14', 'text': 'امسحه نهائي'},
        # read tools
        {'thread': 'TX', 'text': 'ورّيني اللي اتنشر وآخر نتيجة'},
        {'thread': 'TX', 'text': 'ايه جدول النشر بتاع اليومين الجايين؟'},
        {'thread': 'TX', 'text': 'النظام شغال كويس؟'},
        {'thread': 'TX', 'text': 'ايه القواعد الحالية؟'},
        {'thread': 'TX', 'text': 'دور على عناصر Acceptance'},
        {'thread': 'TX', 'text': 'ايه اللي حصل لـ ACC10؟'},
        # format, recheck, window, skip, resume
        {'thread': 'T5', 'text': 'ايه وضع ACC5؟'},
        {'thread': 'T5', 'text': 'حوّله لبوست'},
        {'thread': 'T5', 'text': 'رجّعه ستوري'},
        {'thread': 'T5', 'text': 'اعمل فحص تاني للفيديو ده'},
        {'thread': 'T5', 'text': 'متنزلوش قبل بعد بكرة'},
        {'thread': 'T5', 'text': 'تخطاه'},
        {'thread': 'T5', 'text': 'رجّعه تاني'},
        {'thread': 'T5', 'text': 'وقفه'},
        # source replacement, owner reports, rework
        {'thread': 'T7', 'text': f'استخدم الملف ده لـ ACC7 بدل اللي عليه: {alt7}'},
        {'thread': 'T7', 'text': 'شيكت على انستجرام وACC7 ما اتنشرش'},
        {'thread': 'T7', 'text': 'رجّع ACC7 للمونتير يعدله، الصوت عالي'},
        {'thread': 'T12', 'text': 'أنا نزلت ACC12 بنفسي على حساب الاستوديو'},
        {'thread': 'T12', 'text': 'ورّيني الكابشنات القديمة اللي محتاجة اعتماد'},
        # a real item held after the correction: the reason, its source, the policy and the options (read only)
        {'thread': 'TW', 'text': 'ليه Thai 2 Will Filing A Claim واقف؟'},
        # stop everything left (bulk > 10)
        {'thread': 'S', 'text': 'وقف ACC1 وACC2 وACC3 وACC4 وACC5 وACC6 وACC7 وACC8 وACC11 وACC12 وACC13'},
    ]
    return s


def main():
    fx = json.loads(Path(sys.argv[1]).read_text())
    out_dir, sid, not_before = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    spec = {'id': sid, 'not_before': not_before, 'quiet_minutes': 45,
            'test_items': [v['item_id'] for v in fx['items'].values() if v.get('item_id')], 'steps': steps(fx)}
    raw = json.dumps(spec, ensure_ascii=False, indent=1).encode()
    out_dir.mkdir(mode=0o755, exist_ok=True)
    path = out_dir / f'{sid}.json'
    path.write_bytes(raw)
    os.chmod(path, 0o644)
    print(len(spec['steps']), 'steps', hashlib.sha256(raw).hexdigest()[:12])


if __name__ == '__main__':
    main()
