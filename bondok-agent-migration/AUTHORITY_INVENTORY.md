# جرد القيود وتصنيفها (2026-10-14)

ثلاث فئات فقط:
* **قاعدة عمل (المالك)**: تفضيل يملكه المالك؛ له أداة تغيير دائم (`set_rule`) وغالبًا استثناء لعنصر (`waive_rule` /
  `restore_rule`). القيم الافتراضية = ما كان معمولًا به.
* **حد منصة**: حقيقة من Instagram/Dropbox/Monday؛ لا يتغير بالكلام، ويُذكر سببه للمالك.
* **حماية حقيقة التنفيذ**: تحفظ ما حدث فعلًا وتمنع التكرار غير المقصود أو انتحال أمر المالك. ليست سلطة فوق المالك:
  لا تمنع أمرًا واضحًا، بل تحدد ماذا يعني التنفيذ (ومتى نقول «غير محسوم»).

## قواعد العمل (قابلة للتغيير بأمر المالك)
| القاعدة | الافتراضي | مكان التطبيق | الأدوات |
|---|---|---|---|
| `topaz_required` | true | `items.evaluate`, `prep_source` | `confirm_topaz` (تأكيد، بلا شرط اسم ملف)، `waive_rule` (استثناء لا يسجل معالجة)، `set_rule` |
| `min_short_edge` | 1080 | `rules.media_failure`, `media._source_policy` | set / waive |
| `max_bytes` | 300,000,000 | `rules.media_failure` | set (≤ حد Reel 300 MB) |
| `story_max_seconds` / `story_trim_max_seconds` | 60 / 65 | `rules.story_*` | set (≤ حد Story 60 ث) |
| `story_slots` / `post_slots` | الشبكة الحالية (+16:00 بقرار المالك 2026-10-14) | `rules.grid` | set؛ لا تحرك الحجوزات القائمة |
| `late_window_minutes` | 120 | `rules.late_deadline` | set |
| `post_caption_approval` | true | `sched.eligible` | set (بدون اعتماد يبقى فحص صلاحية النص) |
| `duplicate_video_block` | true | `sched.duplicate_of` | set / waive (إعادة نشر مقصودة = مهمة جديدة) |
| `occupied_slot_needs_approval` | true (قرار option B 2026-10-10) | `sched._propose_swap` | set: false = نقل مباشر |
| `require_collab` | true | `items.evaluate` (Story فقط) | set / waive: نشر الستوري بدون المتعاون |
| `board_freshness_minutes` | 15 | `publish.commit(record='store')`, `cycles.publish_one` | set (0 = قراءة جديدة دائمًا) |
| `allowed_boards` | البوردات الثلاثة | `waset_runtime.monday._scope` | set (نطاق الكتابة على Monday) |
| `dropbox_roots` | `/Social Media` | `admin.op_file_action` | set (نطاق تعديل الملفات) |
| `agent_max_steps` | 6 | `agent.Agent.answer` | set |
| `publish_attempts_per_slot` | 3 | `publish.claim` | set |
| اعتماد مسودات الموديل للكابشن | مسودة حتى يعتمدها المالك | `captions.op_caption_draft` | `approve_caption_draft` أو `set_rule post_caption_approval` |

**أُزيلت** (قيود أضافها المطور بلا اعتماد): حد تأكيد الأوامر لأكثر من 10 عناصر؛ حد 6 إجراءات لكل خطوة موديل
(وُجد في الفحص الحي 2026-10-15)؛ تفسير «حذف» كأرشفة (الآن عمليتان
`board_archive_item` و`board_delete_item`، والسؤال فقط عند «شيل/remove» الغامضة)؛ منع تعديل ملف عنصر مجدول (الآن
يُنفذ: يُحرر الموعد، يُعاد فحص الملف، وتُلغى المحاولة قبل الإرسال إن وجدت)؛ منع IG Collab للبوست (الآن يُنشر مع
المتعاونين)؛ اشتراط كتابة Publishing على Monday قبل النشر (الآن النية في القاعدة، والعرض مزامنة لاحقة)؛ عبارة «explain
them, do not override them».

## حدود المنصة (`rules.PLATFORM`, `media.platform_failure`)
Story ≤ 60 ث و< 100 MB؛ Reel 3 ث–15 د و≤ 300 MB؛ ≤ 1920 عمود؛ 23–60 FPS؛ AAC ≤ 48 kHz؛ 25 Mbps؛ Collab غير مدعوم
للستوري وحتى 3 للريل؛ الكابشن ≤ 2200 حرف و30 هاشتاج و20 إشارة؛ 400 container و100 منشور/24 س؛ container ينتهي بعد 24 س؛
حذف/تعديل منشور غير متاح في واجهة النشر (`Instagram.unsupported`)؛ رفع Dropbox بطلب واحد ≤ 150 MiB (حد 145 MB للمسار
الحالي، الجلسات مدعومة)؛ Monday لا يميز كاتب التعديل (حساب واحد).

## حماية حقيقة التنفيذ (تبقى)
| الحماية | لماذا | المكان |
|---|---|---|
| هوية المالك من Slack الموثق فقط؛ نص الرسالة/البورد/الملفات لا يمنح صلاحية | منع الانتحال | `bridge.run`, `core.authorize` |
| أمر من البورد = تعديل مُلاحظ لعمود يملك تلك العملية | منع انتحال أوامر المالك بكتابات آلية | `CoreMixin.BOARD_COLUMNS` |
| الاقتباس يجب أن يكون من رسالة المالك نفسها؛ الأسئلة والشروط والاقتباسات لا تُنفذ؛ الغموض سؤال واحد | التمييز بين أمر وسؤال (أخطاء R5 B5 الحقيقية) | `language.contradiction`, `bridge.run` |
| اقتراح بندق من نفسه = سؤال «اه/لا» | الموديل لا يقرر بدل المالك | `bondok_offer` |
| idempotency بمعرف الأمر؛ نفس المعرف بمحتوى مختلف = تعارض | إعادة تسليم نفس الأمر = نفس المهمة | `core.submit` |
| lease/fence لكل دورة ومحاولة؛ محاولة نشطة واحدة لكل عنصر (فهرس فريد) | لا مساران ينشران نفس المهمة | `run_start`, `ops_attempts_one_active` |
| نقطة الالتزام: الحالة والنسخة والحجز والبصمة ونسخة Dropbox والنافذة | لا يُنشر شيء غير ما فُوض | `publish.commit` |
| بعد الإرسال: نتيجة غامضة = «غير محسوم» ومصالحة من حالة الـcontainer؛ «لم يُنشر» من المالك بتأكيد واحد | لا نشر أعمى ولا ادعاء نجاح | `result`, `reconcile`, `resolve_publication` |
| دليل نشر (media id/permalink) منفصل عن «المالك أكد النشر» | صدق السجل | `_published`, `report_published` |
| سجل المحتوى المنشور (assets journal) | منع النشر المكرر غير المقصود (الاستثناء متاح) | `duplicate_of` |
| تأكيد Topaz مرتبط بنسخة الملف التي رآها المالك | التأكيد يخص ما رآه | `_topaz_needs_question` |
| القاطع عند فشل الاعتماد/التقييد/الحد اليومي | عدم إنشاء containers بلا جدوى | `breaker` |
| مفتاح ملكية التشغيل + سياج النشر | مسار تشغيل واحد | `runtime_owner`, `deploy-fence.json` |
| فجوة الاستعادة | لا نفقد منشورات بعد نسخة قديمة | `restore_gap` |
| حماية الجداول القريبة (10 د) للأتمتة فقط | لا تحرك آلي لموعد على وشك النشر؛ المالك غير مقيد | `monitor.inspect` |
