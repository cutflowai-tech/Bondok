# P0 — الواقع الحالي ونقطة البداية (2026-10-14)

قراءة فقط. لم يُلمس الإنتاج في هذه الجلسة (لا SSH ولا n8n ولا Monday ولا Slack). الإنتاج مأخوذ من خط الأساس
المقروء يوم 2026-10-13 (`.local-snapshots/2026-10-13/CURRENT_STATE_BASELINE.md`، خارج Git) ويجب إعادة قراءته يوم
الانتقال.

## P0-01 المستودع والإنتاج
| البند | الحالة |
|---|---|
| المستودع | `/Volumes/Zeno/Bondok` → GitHub `cutflowai-tech/Bondok` (عام) |
| فرع البداية | `bondok/r5-monday-authority` @ `8675c93` (R5 كاملة، غير منشورة). فرع العمل الجديد: `bondok/agent-migration` |
| الإنتاج (10-13) | helper و`waset_ops` على n8n = إصدار `452da98` **+ 3 تعديلات يدوية غير مسجلة** (`rules.py` موعد ستوري 16:00 "TEMP"، `sched.py`، `items.py` تجاهل تعديلات الوقت على العناصر المثبتة). بندق = `452da98` بدون تعديل. WF1/2/3 = `452da98` بالضبط |
| أثر ذلك على الخطة | كود الإنتاج ليس كود الفرع؛ الانتقال يستبدل الاثنين معًا ولا يرث التعديلات اليدوية. موعد 16:00 المؤقت يحتاج قرار المالك: يمكن الآن تسجيله كقاعدة (`set_rule story_slots`) بدل تعديل الكود |

## P0-02 مسؤوليات n8n ومقابلها
كل قرار في الـworkflows يمر أصلًا عبر `src/helper.py` → `waset_ops`؛ n8n ينفّذ اتصالات HTTP والتوقيت فقط. الجرد
الكامل (العقد، الاستعلامات، الأعمدة، معالجة الأخطاء) تم من `workflows/build.py` و`workflows/dist`.

| الوظيفة القديمة | المشغّل | اتصالات خارجية | قرار الكود (يُعاد استخدامه) | البديل في بندق |
|---|---|---|---|---|
| WF1 دورة كل 10 د + lease | n8n schedule | — | `run_start/run_check/run_finish` | `waset_runtime` cycle `prepare` |
| قراءة بورد المصادر + البورد الاجتماعي (pagination) | WF1 | Monday `items_page`/`next_items_page` | — | `integrations.monday.board_items` |
| الاستيراد وإنشاء العناصر | WF1 | Monday `create_item` (دفعات 20) | `import_plan`, `import_record` | `runtime.prepare.import_items` |
| ملاحظة تعديلات البورد | WF1 (+WF2 للعنصر المستحق) | — | `observe`, `board_missing` | نفس الاستدعاءات |
| اختيار ملف Dropbox (قواعد المجلد/الإصدار) | WF1 Code nodes | Dropbox shared-link metadata, list_folder(+continue), get_metadata, create_folder, shared links | `prep_source` | `runtime.selection` (نقل حرفي للـregex والترتيب) |
| فحص/تجهيز الفيديو ffprobe/ffmpeg | helper (عامل منفصل) | تنزيل Dropbox raw | `media.preflight/prepare`, `prep_*` | كما هو (عملية منفصلة، فتحتان) |
| رفع الملف الجاهز ومشاركته | WF1 | Dropbox `files/upload`, get_metadata, shared links | `prep_delivered` | `runtime.prepare.deliver` |
| مسودة الكابشن | WF1 LLM node | Monday updates (brief), OpenRouter | `caption_needed`, `caption_draft` | `runtime.captions` |
| مهام المونتير (subitems) | WF1 | Monday subitems/create_update/change_column_value | outbox `editor` | `runtime.sync.editor` |
| مزامنة عرض Monday (compare-before-write) | WF2 كل دقيقة | Monday read-before-write + `change_multiple_column_values` + `move_item_to_group` | outbox `monday`, `source_monday` | `runtime.sync.display` |
| النشر: due → claim → container → poll → commit → intent → media_publish → result → permalink | WF2 | Instagram `/media`, container status, `/media_publish`, permalink; Dropbox تحقق النسخ | `due/claim/container/renew/commit/intent_ack/result/evidence/precommit_failure` | `runtime.publish` |
| مصالحة النتائج الغامضة + فحص الوصول | WF2 | Instagram container status, `/{ig}?fields=id` | `reconcile`, `provider_probe` | `runtime.publish` |
| المراقب: إصلاح الجدولة، المواعيد الفائتة، المقترحات، القرص | WF3 كل 30 د | — | `repair`, `inspect` | `runtime.supervise` |
| تنظيف الملفات | WF1 | — | `media.maintenance` | `runtime.prepare` |
| تنبيهات Slack | **بندق بالفعل** (outbox `slack`) | Slack | `notify` | كما هو |
| مراقبة الأعطال | بندق watchdog + `errors.log` | — | heartbeats | يمتد ليشمل دورات `waset_runtime` |

اعتمادات n8n الخاصة ببندق: Monday API، Dropbox OAuth2 (اثنان)، Instagram custom auth، OpenRouter. يجب أن تنتقل
كأسرار في `.env` الخاصة بالخدمة الجديدة ثم تُحذف من n8n (P6-06).

## P0-03 احتفظ / عدّل / احذف
* **احتفظ**: `waset_ops` كاملة (المعالج الوحيد، idempotency، leases/fences، outbox، record، media worker،
  تصنيف أخطاء Instagram). هي جوهر سلامة التنفيذ ومغطاة بـ504 اختبار.
* **عدّل**: `rules.py` (قيم ثابتة → سياسات مالك ذات إصدارات)، `bondok/policy.txt` و`bridge.py` و`language.py`
  (موانع الإيجنت)، `monday_read.py` (قراءة فقط → عميل تنفيذ عبر الأدوات).
* **أضف**: `src/waset_runtime/` (اتصالات HTTP + دورات بديلة لـWF1/2/3 + مؤقت دائم)، مفتاح ملكية التشغيل.
* **احذف لاحقًا (P6 فقط)**: `workflows/build.py` و`dist`، تمرير base64/inbox في `helper.py`، سكربتات الاستيراد في
  `deploy/` الخاصة بـn8n.

## P0-04 القواعد وموانع الصلاحيات
| القاعدة/المانع | المصدر قبل الهجرة | من يغيّرها بعد P1 |
|---|---|---|
| Topaz مطلوب لنسخة الملف | `items.evaluate`, `prep_source` | المالك: `confirm_topaz` / `waive_rule topaz_required` / `set_rule topaz_required` |
| 1080 px | `rules.MIN_SHORT_EDGE`, `media._source_policy` | `min_short_edge` (قاعدة + استثناء) |
| < 300,000,000 بايت | `rules.BUSINESS_MAX_BYTES` | `max_bytes` (حد المنصة للريلز 300 MB) |
| ستوري < 60 ث، قص 60–65 | `rules.STORY_*` | `story_max_seconds` (≤ 60 حد المنصة)، `story_trim_max_seconds` |
| مواعيد الستوري/البوست | `rules.STORY_SLOTS/POST_SLOTS` | `story_slots`, `post_slots` (لا تحرك الحجوزات القائمة) |
| نافذة التأخير ساعتان | `rules.LATE_WINDOW` | `late_window_minutes` |
| اعتماد كابشن البوست | `sched.eligible` | `post_caption_approval` |
| منع نشر نفس الفيديو مرتين | `sched.duplicate_of` | `duplicate_video_block` (قاعدة + استثناء) |
| **مانع**: "Rules (enforced in code; explain them, do not override them)" | `bondok/policy.txt` | أزيل؛ التعليمات الجديدة تمنح السلطة صراحة |
| **مانع**: "Never … confirm Topaz …" بدون تمييز تأكيد المالك | `policy.txt` | أعيدت كتابته: تأكيد المالك تنفيذ مباشر |
| **مانع**: "Bondok has no Instagram path / explains, never repairs / Monday read-only" | `ARCHITECTURE_AND_OWNERSHIP.md`, `monday_read.py` | يُستبدل في P2–P4 بأدوات تنفيذ |
| **مضلل**: سبب الحجب "the filename is not proof" | `items.prep_source` | استُبدل بـ"the file name does not matter" |
| حدود منصة حقيقية (ليست قواعد) | `media.platform_failure` | لا أحد؛ تُعرض كحد منصة: ستوري ≤ 60 ث و**≤ 100 MB**، ريل ≤ 300 MB و3 ث–15 د، ≤ 1920 عمود، 400 container/24 س، 100 منشور/24 س |
| حد مسار النقل 145 MB | `rules.TRANSFER_MAX_BYTES` (رفع Dropbox بطلب واحد) | يرتفع عند تنفيذ upload session في البديل |

السبب المرجح لمشكلة «أنا عملت توباز»: في كود الإنتاج `452da98` التعليمات تقول للموديل ألا يتجاوز القواعد ولا يؤكد
Topaz بمبادرته، ورسالة الحجب نفسها تتحدث عن اسم الملف، ولا توجد أداة استثناء أصلًا، فـ«انشر من غير Topaz» لا
تُنفذ. على كود الفرع، مسار `confirm_topaz` يعمل لصيغ مثل «أنا عملت توباز للفيديو ده» حين يقدّم الموديل المعنى، لكن
«توبزته» كانت تُرفض (لا توجد في القاموس). لم يُتحقق من سلوك الموديل الحي (يحتاج تفويضًا).

## P0-05 قدرات APIs (توثيق رسمي، قُرئ 2026-10-14)
| المنصة | العمليات المستخدمة/المطلوبة | قيود مؤكدة | غير مدعوم/غير متحقق |
|---|---|---|---|
| Instagram Graph (v26.0) | `POST /{ig}/media` (REELS/STORIES)، `GET /{container}?fields=status_code,status`، `POST /{ig}/media_publish`، `GET /{media}?fields=permalink`، `GET /{ig}/content_publishing_limit` | حالات container: EXPIRED/ERROR/FINISHED/IN_PROGRESS/PUBLISHED؛ ينتهي بعد 24 س؛ 400 container/24 س؛ 100 منشور API/24 س؛ ستوري 60 ث و100 MB؛ ريل 300 MB، 3 ث–15 د؛ `collaborators` مدعوم للريلز (حتى 3) وغير مدعوم للستوري؛ الصلاحيات `instagram_business_content_publish` أو `instagram_content_publish` | idempotency لـ`media_publish` غير موثقة (لذلك المصالحة إلزامية)؛ تعديل الكابشن/حذف المنشور غير موثق في صفحات النشر → `instagram.manage` يعرضه كغير مدعوم |
| Monday GraphQL | `items_page`/`next_items_page`، `items(ids)`، `create_item`، `change_multiple_column_values`، `move_item_to_group`، `create_subitem`، `create_update`، `change_column_value`، `archive_item`، `delete_item` | pagination بالمؤشر (500)، حدود complexity/معدل → إعادة محاولة بتأخير | webhooks (بديل الاستطلاع) غير مفعلة؛ الاستطلاع كل 10 د يبقى + مراجعة دورية |
| Dropbox | `sharing/get_shared_link_metadata`، `files/list_folder(+continue)`، `files/get_metadata`، `files/create_folder_v2`، `sharing/create_shared_link_with_settings`، `sharing/list_shared_links`، `files/upload` | `files/upload` ≤ 150 MiB لكل طلب (سبب حد 145 MB)؛ الأكبر يحتاج `upload_session` | `move_v2`/`delete_v2` غير مستخدمة اليوم (تُضاف كأدوات بأمر المالك) |
| Slack | Socket Mode، `chat.postMessage` | — | — |

لم يُتحقق من صلاحيات الحسابات الفعلية (لا وصول حي في هذه الجلسة) → يُتحقق في P6-01 بقراءة فقط.

## P0-06 البيانات الجارية (10-13، قراءة فقط)
13 منشورًا منذ 10-10، محاولة واحدة لكل عنصر، صفر محاولات غامضة؛ 32 حجزًا مستقبليًا (كلها ستوري، مثبتة من المالك)؛
زوج عناصر بنفس الملف (ErZ#3) محجوب على Topaz فقط → خطر نشر مكرر لو أُكد Topaz على الاثنين (`duplicate_video_block`
يحميه في الفرع). **العائق**: نسخة متسقة من `state.sqlite` وتجربة استعادة معزولة تحتاج قراءة من السيرفر (`sqlite3 .backup`
بصلاحية القراءة) — لم تُنفذ في هذه الجلسة.

## P0-07 الاختبارات وأوامر التشغيل
`cd tests && python3 -m unittest discover -s . -p 'test_*.py'` → **504 جمعت/شغلت، 0 متخطاة، OK** على Python 3.12
(166 ث) قبل أي تعديل. Python 3.14 متاح محليًا (`/opt/homebrew/bin/python3.14`).

## فروق بين الخطة والمستودع
1. الخطة تقول «قاعدة بندق مصدر الحقيقة، Monday واجهة». قرار المالك 2026-10-12 (ADR) جعل Monday سجل قرارات العمل
   والقاعدة المحلية سجل التنفيذ. التنفيذ يحافظ على الاثنين: القاعدة تملك التنفيذ (attempts، leases، outbox)،
   وقرارات المالك (بما فيها الاستثناءات الجديدة `wv`) تُكتب في عمود `bondok_record` لتعود عند إعادة البناء.
   سياسات القواعد العامة في القاعدة المحلية + تنبيه Slack لكل تغيير. لا حاجة لقرار جديد إلا لو أراد المالك نقل
   السلطة كاملة.
2. الخطة تفترض «اشتراط اسم ملف» في الكود؛ الكود لا يشترطه (الاسم يؤثر فقط على ترتيب اختيار الملف). المشكلة كانت في
   التعليمات وغياب أداة الاستثناء ورسالة الحجب.
3. n8n لا تحمل إلا الاتصالات والتوقيت؛ القرارات أصلًا في `waset_ops`. لذلك البديل نقل للاتصالات وليس إعادة كتابة منطق.
