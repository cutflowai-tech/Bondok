"""Deterministic reading of the owner's Slack words (Egyptian Arabic and English).

This module never authorizes anything on its own. The model proposes structured meaning for each requested
action (speech act, polarity, targets as the owner said them, constraints, an evidence quote); trusted code in
bridge.py executes it only when this reader, looking at the owner's actual words, finds nothing that contradicts
it (a question, negation, future/plan, condition, quotation, missed expectation, other destination ...) and the
words support that kind of action at all. A miss here costs one narrow question, never an unrequested action.

Normalisation (Slack markup, zero-width/RLM, Arabic-Indic digits, alef/ya/ta-marbuta forms, diacritics, bold)
is for matching only; stored text (captions) is decoded separately by `decode_slack`.
"""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import date, timedelta

# ---------------------------------------------------------------------------------------------- normalisation
_CONTROLS = re.compile('[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u061c\u00ad\u180e]')
_DIACRITICS = re.compile('[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]')
_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
_SHORTCODES = {'+1': '👍', 'thumbsup': '👍', 'thumbs_up': '👍', 'white_check_mark': '✅', 'heavy_check_mark': '✔️',
               'ok_hand': '👌', 'pray': '🙏', 'fire': '🔥', 'heart': '❤️', 'red_heart': '❤️', 'sparkles': '✨',
               'star': '⭐', 'star2': '🌟', 'camera': '📷', 'camera_with_flash': '📸', 'sunrise': '🌅',
               'sunny': '☀️', 'ocean': '🌊', 'palm_tree': '🌴', 'clap': '👏', 'muscle': '💪', 'tada': '🎉',
               'rocket': '🚀', '100': '💯', 'point_down': '👇', 'point_right': '👉', 'eyes': '👀',
               'smile': '😄', 'blush': '😊', 'heart_eyes': '😍', 'joy': '😂', 'wink': '😉', 'sunglasses': '😎',
               'raised_hands': '🙌', 'movie_camera': '🎥', 'film_frames': '🎞️', 'video_camera': '📹',
               'round_pushpin': '📍', 'pushpin': '📌', 'airplane': '✈️', 'desert': '🏜️', 'camel': '🐪',
               'dromedary_camel': '🐪', 'sun_with_face': '🌞', 'crescent_moon': '🌙', 'zap': '⚡', 'gem': '💎',
               'trophy': '🏆', 'gift': '🎁', 'balloon': '🎈', 'confetti_ball': '🎊', 'thumbsdown': '👎',
               '-1': '👎', 'x': '❌'}
_SLACK_TOKEN = re.compile(r'<([^<>]*)>')


class SlackMarkupError(ValueError):
    """Text contains Slack markup that cannot become plain caption text (e.g. an unresolved user mention)."""


def decode_slack(text: str, *, strict: bool = False) -> str:
    """Slack transport markup -> the text the person typed. `strict` refuses what has no plain-text meaning
    for Instagram (user mentions without a label, broadcasts); used for stored captions (R5 M24)."""
    def link(m):
        body = m.group(1)
        target, _, label = body.partition('|')
        if target.startswith('@'):                       # user mention
            if label:
                return '@' + label
            if strict:
                raise SlackMarkupError('a Slack user mention (@someone) cannot be published; type the Instagram '
                                       'handle as plain text')
            return '@user'
        if target.startswith('#'):                       # channel mention
            if strict and not label:
                raise SlackMarkupError('a Slack channel mention cannot be published')
            return '#' + (label or 'channel')
        if target.startswith('!'):                       # <!here>, <!channel>, <!subteam^..|@team>, <!date..>
            if strict:
                raise SlackMarkupError('a Slack broadcast or special mention cannot be published')
            return label or '@' + target[1:].split('^')[0]
        if label:
            return label
        return target[7:] if target.startswith('mailto:') else target
    out = _SLACK_TOKEN.sub(link, text or '')
    out = html.unescape(out) if re.search(r'&(amp|lt|gt|quot|#\d+);', out) else out

    def emoji(m):
        code = m.group(1).lower()
        if code.startswith('skin-tone'):
            return ''
        if code in _SHORTCODES:
            return _SHORTCODES[code]
        if strict:
            raise SlackMarkupError(f'the emoji :{code}: arrived as text; paste the emoji itself')
        return m.group(0)
    return re.sub(r':([a-z0-9_+\-]{1,40}):', emoji, out)


def norm(text: str) -> str:
    """Matching form: decoded, controls/diacritics removed, letters and digits unified, case folded."""
    t = decode_slack(text)
    t = unicodedata.normalize('NFKC', t)
    t = _CONTROLS.sub('', t)
    t = _DIACRITICS.sub('', t)
    t = t.translate(_DIGITS)
    t = re.sub('[أإآٱ]', 'ا', t).replace('ى', 'ي').replace('ة', 'ه').replace('ؤ', 'و').replace('ئ', 'ي')
    t = t.casefold().replace('؟', '?').replace('،', ',').replace('؛', ';').replace('٫', ':')
    t = re.sub(r"\b(i|you|we|they|it|he|she|that)['\u2019]ll\b", r'\1 will', t)
    t = re.sub(r"\bwon['\u2019]t\b", 'will not', t)
    t = re.sub(r"\bcan['\u2019]t\b", 'cannot', t)
    t = re.sub(r"['\u2019`]", '', t)
    t = re.sub(r'[*~]|(?<![^\W_])_|_(?![^\W_])', ' ', t)       # Slack bold/strike/italic markers
    return re.sub(r'[ \t\r\f\v]+', ' ', t).strip()


def compact(text: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^\w?👍✅ ]+', ' ', norm(text))).strip()


def contains(haystack: str, needle: str) -> bool:
    """Literal containment after normalisation (punctuation and spacing differences ignored)."""
    n = compact(needle)
    return bool(n) and n in compact(haystack)


def tokens(text: str) -> list[str]:
    return re.findall(r'[^\W_]+|\?|👍|✅', norm(text))


# ---------------------------------------------------------------------------------------------- morphology
_NOUN_PRE = ('', 'و', 'ف', 'ب', 'ل', 'ك', 'ال', 'وال', 'بال', 'فال', 'لل', 'ولل')
_VERB_PRE = ('', 'و', 'ف')
_OBJ_SUF = ('', 'ه', 'ها', 'هم', 'هوش', 'هاش', 'همش', 'و', 'وه', 'وها', 'وهم', 'ني', 'نا', 'لي', 'لك', 'له', 'لها',
            'لهم', 'كم', 'ك', 'ي', 'وا', 'ين')


def _forms(tok, pre, suf):
    for p in pre:
        if p and not tok.startswith(p):
            continue
        core = tok[len(p):]
        for s in suf:
            if s and (not core.endswith(s) or len(core) - len(s) < 2):
                continue
            yield (core[:-len(s)] if s else core), s


def match(tok: str, lex, pre=_NOUN_PRE, suf=_OBJ_SUF):
    """The attached object suffix ('' if none) when `tok` is a word of `lex` with Arabic clitics, else None.
    Whole words only: 'postpone' is not 'post', 'ابريل' is not 'ريل'."""
    for core, s in _forms(tok, pre, suf):
        if core in lex:
            return s
    return None


def has(toks, lex, pre=_NOUN_PRE, suf=_OBJ_SUF) -> bool:
    return any(match(t, lex, pre, suf) is not None for t in toks)


def _phrase(toks, words) -> bool:
    n = len(words)
    return any(tuple(toks[i:i + n]) == words for i in range(len(toks) - n + 1))


def _lex(*words):
    return frozenset(norm(w) for w in words)


PUBLISH = _lex('انشر', 'تنشر', 'ينشر', 'ننشر', 'نشر', 'هنشر', 'نزل', 'تنزل', 'ينزل', 'ننزل', 'طلع', 'تطلع', 'يطلع',
               'publish', 'post', 'repost', 'schedule')
COMPLETED_PUBLISH = _lex('اتنشر', 'اتنشرت', 'اتنشرو', 'اتنشروا', 'نزلت', 'نزلنا', 'نشرت', 'نشرنا', 'اتنزل',
                         'اتنزلت', 'منشور', 'منشوره', 'published', 'posted', 'live')
STOP = _lex('وقف', 'اوقف', 'يوقف', 'توقف', 'نوقف', 'ايقاف', 'بطل', 'بطلي', 'استني', 'استنا', 'اصبر', 'كفايه',
            'pause', 'paused', 'hold', 'stop', 'wait', 'freeze', 'halt', 'enough')
SKIP = _lex('تخطي', 'اتخطي', 'تخطا', 'اتخطا', 'تتخطي', 'سكيب', 'اسكب', 'skip', 'skipped')
CANCEL = _lex('الغي', 'cancel', 'شيل', 'امسح')
PUB_NOUN = _lex('نشر', 'النشر', 'بوست', 'ستوري', 'ريل', 'post', 'story', 'reel', 'publication', 'publishing')
RESUME = _lex('كمل', 'كملي', 'استانف', 'رجع', 'شغل', 'resume', 'unpause', 'continue', 'proceed')
CONTINUE = _lex('كمل', 'كملي', 'continue', 'proceed')
REWORK = _lex('مونتير', 'المونتير', 'مونتاج', 'المونتاج', 'ايديتور', 'editor', 'تعديل', 'يتعدل', 'تتعدل', 'عدل',
              'rework', 'reedit', 'edit', 'edits', 'montage')
NON_ITEM_OBJECT = _lex('كابشن', 'كابشين', 'caption', 'وصف', 'معاد', 'ميعاد', 'وقت', 'فحص', 'check', 'recheck',
                       'time', 'تشيك', 'الكلام')
TOPAZ = _lex('توباز', 'topaz', 'topazed')
DONE = _lex('خلص', 'خلصت', 'خلصنا', 'خلصناه', 'خلصو', 'خلصوا', 'خلصان', 'خلاص', 'اتعمل', 'اتعملت', 'عملنا', 'عملناه',
            'عملت', 'عملته', 'عملو', 'عملوا', 'تم', 'اتم', 'اتمت', 'جاهز', 'جاهزه', 'done', 'finished', 'completed',
            'complete', 'ready', 'did', 'topazed')
CHANGE = _lex('حول', 'غير', 'خلي', 'اعمل', 'نخلي', 'change', 'convert', 'switch', 'make', 'turn')
FORMAT_WORDS = {'Post': _lex('بوست', 'ريل', 'ريلز', 'post', 'reel', 'reels'),
                'Story': _lex('ستوري', 'استوري', 'ستوريز', 'story', 'stories')}
CAPTION = _lex('كابشن', 'كابشين', 'caption', 'captions', 'وصف', 'draft', 'مسوده', 'نص')
APPROVE = _lex('اعتمد', 'موافق', 'وافق', 'اوافق', 'approve', 'approved', 'accept', 'accepted')
CHECK = _lex('اتاكد', 'تاكد', 'شوف', 'check', 'verify', 'confirm')
ACK = _lex('شكرا', 'شكر', 'ثانكس', 'thanks', 'thank', 'thx', 'ty', 'مرسي', 'ميرسي', 'تسلم', 'تسلمي', 'تسلمو',
           'تسلموا', 'حلو', 'حلوه', 'جميل', 'جميله', 'مدهش', 'مدهشه', 'عظيم', 'رائع', 'ممتاز', 'great', 'nice',
           'cool', 'wow', 'awesome', 'amazing', 'lovely', '🙏')
AFFIRM = _lex('اه', 'ايوه', 'ايوا', 'ايو', 'اوك', 'اوكي', 'ok', 'okay', 'yes', 'yep', 'yeah', 'ya', 'sure', 'تمام',
              'ماشي', 'موافق', 'اكيد', 'طبعا', 'نفذ', 'كمل', 'اعتمد', 'approve', 'approved', 'go', 'ahead', 'do',
              'اعمل', 'اعملها', 'اعمله', 'اعملهم', 'يلا', 'حاضر', 'اوافق', 'confirm', 'confirmed', 'proceed',
              'continue', 'correct', 'right', 'صح', 'بالظبط', 'exactly', '👍', '✅')
AFFIRM_COMMAND = _lex('نفذ', 'كمل', 'اعتمد', 'approve', 'approved', 'go', 'do', 'اعمل', 'اعملها', 'اعمله',
                      'اعملهم', 'proceed', 'continue', 'confirm', 'يلا', 'اوافق', 'موافق')
NEG_ANSWER = _lex('لا', 'no', 'nope', 'cancel', 'الغي', 'ارفض', 'reject', 'رفض', 'انسي', 'سيبك', 'nevermind')
FILLER = _lex('يا', 'بندق', 'خلاص', 'بس', 'كده', 'please', 'plz', 'pls', 'من', 'فضلك', 'لو', 'سمحت', 'with', 'them',
              'it', 'that', 'this', 'ده', 'دي', 'دول', 'the', 'proposal', 'المقترح', 'عليه', 'عليها', 'عليهم',
              'برضه', 'كمان', 'and', 'و', 'كلهم', 'all', 'of', 'mind', 'never', 'جدا', 'very', 'much', 'so', 'انا',
              'i', 'we', 'it', 'a', 'lot', 'تاني', 'again', 'now', 'دلوقتي', 'هو', 'هي', 'هما', 'ها')
SHOW = _lex('وريني', 'وريهولي', 'ورهولي', 'ابعت', 'ابعته', 'ابعتها', 'ابعتهولي', 'ابعتهالي', 'ابعتلي', 'show', 'send',
            'resend', 'repeat', 'فين', 'where', 'تاني', 'again', 'كرر', 'افكرني', 'remind')

_Q_START = _lex('هل', 'ليه', 'ليش', 'ايه', 'امتي', 'ازاي', 'فين', 'مين', 'انهي', 'كام', 'هو', 'هي', 'هما', 'ينفع',
                'is', 'are', 'was', 'were', 'has', 'have', 'did', 'does', 'should', 'shall', 'may', 'what', 'why',
                'when', 'how', 'which', 'who', 'where', 'whats', 'hows', 'whether')
_PRONOUNS_1ST = _lex('i', 'we')
_NEG = _lex('مش', 'مو', 'لا', 'لم', 'لن', 'مفيش', 'مافيش', 'بلاش', 'not', 'no', 'never', 'dont', 'doesnt', 'didnt',
            'isnt', 'wasnt', 'arent', 'werent', 'havent', 'hasnt', 'hadnt', 'wont', 'cannot', 'couldnt', 'shouldnt',
            'wouldnt', 'aint', 'without', 'none', 'nothing', 'nor', 'neither')
_LIMIT_AFTER_NEG = _lex('اكثر', 'اكتر', 'more')
_NOT_YET = _lex('لسه', 'لسا', 'yet', 'still')
_FUTURE_EN = _lex('will', 'gonna', 'shall', 'later', 'soon', 'بعدين')
_FUTURE_DAY = _lex('بكره', 'بكرا', 'tomorrow', 'tonight', 'بالليل', 'الليله', 'الصبح', 'بعدين')
_COND = _lex('لو', 'اذا', 'لما', 'طالما', 'if', 'unless', 'once', 'when', 'whenever')
_QUOTE = _lex('قال', 'قالي', 'قالت', 'قالتلي', 'قالو', 'قالوا', 'قالولي', 'بيقول', 'بتقول', 'بيقولو', 'بيقولوا',
              'يقول', 'said', 'says', 'told', 'according')
_OTHER_DEST = (('الحساب', 'التاني'), ('حساب', 'تاني'), ('الحساب', 'الشخصي'), ('حسابي',), ('حسابه',), ('حسابها',),
               ('الصفحه', 'التانيه'), ('صفحه', 'تانيه'), ('other', 'account'), ('another', 'account'),
               ('personal', 'account'), ('my', 'account'), ('فيسبوك',), ('فيس',), ('facebook',), ('tiktok',),
               ('تيكتوك',), ('تيك', 'توك'), ('يوتيوب',), ('youtube',), ('linkedin',), ('snapchat',), ('سناب',),
               ('حساب', 'العميل'), ('client', 'account'), ('their', 'account'))
_MISSED = (('كان', 'المفروض'), ('المفروض', 'كان'), ('كان', 'لازم'), ('supposed',), ('should', 'have'),
           ('shouldve',), ('was', 'meant'), ('كنت', 'عايز'), ('كنت', 'عاوز'), ('كنا', 'عايزين'), ('wanted', 'to'))
_BROAD = _lex('كل', 'كله', 'الكل', 'all', 'everything', 'anything', 'every', 'any')
_PLURAL = _lex('هم', 'همه', 'دول', 'دولا', 'them', 'they', 'these', 'those', 'both', 'الاتنين', 'كلهم', 'التلاته',
               'الستوريز', 'البوستات', 'الفيديوهات', 'stories', 'posts', 'videos')
_SINGULAR = _lex('ده', 'دي', 'it', 'this', 'that', 'الستوري', 'البوست', 'الريل', 'الفيديو', 'الكليب', 'story', 'post',
                 'reel', 'video', 'clip')
_NOT_NEG = _lex('مدهش', 'مشوش', 'مندهش', 'منقوش', 'منفوش', 'منتعش', 'منكمش', 'مرتعش', 'منعش', 'متوحش', 'متعطش',
                'مغشوش', 'مبسوط', 'ماشي', 'مشمش', 'منتفش')
_NOT_FUTURE = _lex('هيا', 'هنا', 'هناك', 'هنالك', 'حتي', 'حيث', 'حين', 'هيه', 'هييه', 'هيكل', 'حياه', 'حياتي',
                   'حياتك', 'حيوان', 'هيصه', 'هتلر', 'حته', 'حتت', 'هيئه', 'حينها')
_ACTION_STEMS = ('خلص', 'عمل', 'نشر', 'نزل', 'وقف', 'شغل', 'رجع', 'كمل', 'حول', 'غير', 'جهز', 'بعت', 'اعتمد', 'شيل',
                 'تخط', 'سيب', 'خلي', 'طلع', 'روح', 'بقي', 'يبقي')
_PROGRESSIVE_EN = _lex('doing', 'working', 'processing', 'rendering', 'finishing', 'running', 'exporting',
                       'uploading')
_NOT_PROGRESSIVE = _lex('بتاع', 'بتاعت', 'بتاعه', 'بتاعها', 'بتاعهم', 'بتاعنا', 'بتوع', 'بتاعي', 'بتاعك', 'بنات',
                        'بيت', 'بيتنا', 'بنفسي', 'بنفسك', 'بيزنس', 'بتقول', 'بيقول', 'بيقولو', 'بيقولوا')
_STUDIO = _lex('حسابنا', 'حساب', 'الحساب', 'our', 'studio', 'الاستوديو', 'waset', 'واسط', 'وسط', 'instagram',
               'انستجرام', 'انستا', 'insta')


def negated(tok: str) -> bool:
    """Arabic circumfix negation (متنشرش, ماتنشرهمش, معملتش) without substring guesses: 'مدهش' and 'مشوش'
    are adjectives, not negations (R5 M25)."""
    if tok in _NOT_NEG or len(tok) < 4 or not tok.endswith('ش') or not tok.startswith('م'):
        return False
    inner = tok[2:-1] if tok.startswith('ما') else tok[1:-1]
    cands = {inner}
    for s in ('هو', 'ها', 'هم', 'كو', 'كي', 'لي', 'له', 'لها', 'لهم', 'نا', 'ني', 'ك', 'و', 'ه'):
        if inner.endswith(s) and len(inner) - len(s) >= 2:
            cands.add(inner[:-len(s)])
    for x in cands:
        if len(x) >= 3 and x[0] in 'تينا':
            return True
        if any(x.startswith(st) for st in _ACTION_STEMS):
            return True
    return False


def future_verb(tok: str) -> bool:
    """Egyptian future prefix ه/ح on a verb (هيبقى, هتنشرهم, هنعمل, حيخلص); 'هنا', 'حتى' are not verbs."""
    if tok in _NOT_FUTURE or len(tok) < 3 or tok[0] not in 'هح':
        return False
    rest = tok[1:]
    if rest[0] in 'يتن' and len(rest) >= 3:
        return True
    if rest.startswith('ا') and any(rest[1:].startswith(st) for st in _ACTION_STEMS):
        return True
    return any(rest.startswith(st) for st in _ACTION_STEMS)


def progressive(tok: str) -> bool:
    """Egyptian ب-imperfect (بيعمل, بنعمل): something in progress, not completed."""
    if tok in _NOT_PROGRESSIVE or tok in _PROGRESSIVE_EN:
        return tok in _PROGRESSIVE_EN
    return len(tok) >= 4 and tok[0] == 'ب' and tok[1] in 'يتن' and any(st in tok[2:] for st in _ACTION_STEMS)


# ---------------------------------------------------------------------------------------------- structure
class Text:
    """The owner's message split into sentences (question/condition/quotation scope) and conjuncts
    (negation/stop/future scope), with token positions."""

    def __init__(self, raw: str):
        self.raw = raw or ''
        self.quoted = []
        lines = (raw or '').split('\n')
        self.toks, self.sent, self.conj, self.q, self.in_quote = [], [], [], {}, []
        s = c = 0
        for line in lines:
            quoted = line.strip().startswith(('&gt;', '>'))
            for piece in re.split(r'([.!?؟]+)', line):
                if not piece:
                    continue
                if re.fullmatch(r'[.!?؟]+', piece):
                    if '?' in piece or '؟' in piece:
                        self.q[s] = True
                    s, c = s + 1, c + 1
                    continue
                for part in re.split(r'([,،;؛:])', piece):
                    if re.fullmatch(r'[,،;؛:]', part or ''):
                        c += 1
                        continue
                    seg = tokens(part)
                    seg = [x for x in seg if x != '?']
                    quote_open = False
                    for t in seg:
                        if self._boundary(t) and self.toks and self.sent[-1] == s:
                            c += 1
                        self.toks.append(t)
                        self.sent.append(s)
                        self.conj.append(c)
                        self.in_quote.append(quoted or quote_open)
                    if re.search('[«»“”"]', part):
                        self._mark_quotes(part)
            s, c = s + 1, c + 1

    def _mark_quotes(self, part):
        for m in re.finditer(r'[«“"]([^«»“”"]+)[»”"]', part):
            self.quoted.append(compact(m.group(1)))

    @staticmethod
    def _boundary(t) -> bool:
        if t in ('and', 'then', 'but', 'بس', 'ثم', 'وبعدين', 'لكن', 'بعدها'):
            return True
        if t.startswith('و') and len(t) > 3:
            rest = t[1:]
            lex = PUBLISH | STOP | SKIP | RESUME | CHANGE | CANCEL | APPROVE | REWORK
            return match(rest, lex, _VERB_PRE) is not None or negated(rest)
        return False

    # --- locating the evidence quote
    def span(self, quote: str) -> tuple[int, int] | None:
        q = [x for x in tokens(quote) if x != '?']
        if not q:
            return None
        n = len(q)
        for i in range(len(self.toks) - n + 1):
            if self.toks[i:i + n] == q:
                return i, i + n
        # attached clitics: the quote may cut a word ("تنشرهم" quoted as "هم")
        joined = ' '.join(self.toks)
        k = joined.find(' '.join(q))
        if k < 0:
            return None
        start = joined[:k].count(' ')
        return start, start + max(1, ' '.join(q).count(' ') + 1)

    def scope(self, span, level: str) -> list[str]:
        ids = getattr(self, level)
        keys = {ids[i] for i in range(*span)}
        return [t for t, k in zip(self.toks, ids) if k in keys]

    def sentences_of(self, span):
        return {self.sent[i] for i in range(*span)}

    def question(self, span) -> bool:
        for s in self.sentences_of(span):
            if self.q.get(s):
                return True
            toks = [t for t, k in zip(self.toks, self.sent) if k == s]
            toks = [t for t in toks if t not in ('طب', 'طيب', 'يا', 'بندق', 'so', 'ok', 'okay', 'and', 'و')]
            if not toks:
                continue
            first = toks[0]
            if first in _Q_START:
                return True
            if first in ('can', 'could', 'would', 'will', 'do') and len(toks) > 1 and toks[1] in _PRONOUNS_1ST:
                return True
            if first == 'do' and len(toks) > 1 and toks[1] in ('you', 'they', 'it'):
                return True
            if first == 'ممكن' and len(toks) > 1 and toks[1][:1] in ('ن', 'ا'):
                return True
            for a, b in zip(toks, toks[1:]):     # alternatives: "انشرهم ولا نستنى", "or wait"
                if a == 'ولا' and (b[:1] in 'نتيا' or b in ('لا', 'لأ')):
                    return True
                if a == 'or' and b in ('wait', 'not', 'hold', 'should', 'leave', 'later', 'skip', 'keep'):
                    return True
        return False


# ---------------------------------------------------------------------------------------------- markers
def _negations(toks) -> list[int]:
    out = []
    for i, t in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ''
        if t in _NEG:
            if t in ('مش', 'no', 'not') and nxt in _LIMIT_AFTER_NEG:     # "بس مش اكثر" = nothing more
                continue
            if t == 'nothing' and nxt == 'more':
                continue
            if t == 'لا' and nxt in ('خلاص',) and i == 0:
                out.append(i)
                continue
            out.append(i)
        elif t == 'ما' and nxt.endswith('ش') and len(nxt) >= 3:
            out.append(i)
        elif negated(t):
            out.append(i)
    return out


def _has_pairs(toks, pairs) -> bool:
    return any(_phrase(toks, p) for p in pairs)


def markers(text: str, quote: str | None = None) -> dict:
    """What the owner's words themselves say about the clause holding `quote` (whole message if None)."""
    T = Text(text)
    sp = T.span(quote) if quote else (0, len(T.toks))
    if sp is None or not T.toks:
        return {'located': False}
    sent, conj = T.scope(sp, 'sent'), T.scope(sp, 'conj')
    sp_toks = T.toks[sp[0]:sp[1]]
    quoted = any(T.in_quote[i] for i in range(*sp)) or (quote and compact(quote) in T.quoted and
                                                       compact(quote) != compact(text))
    return {
        'located': True, 'tokens': sp_toks, 'sentence': sent, 'conjunct': conj,
        'question': T.question(sp),
        'negation': bool(_negations(conj)),
        'not_yet': any(t in _NOT_YET for t in conj),
        'future': any(future_verb(t) for t in conj) or any(t in _FUTURE_EN for t in conj) or
        _phrase(conj, ('going', 'to')),
        'future_day': any(t in _FUTURE_DAY for t in conj) or any(match(t, _lex('بكره', 'بكرا'), ('', 'و', 'ل', 'من'))
                                                               is not None for t in conj),
        'progressive': any(progressive(t) for t in conj),
        'conditional': any(t in _COND and not (t == 'لو' and nxt in ('سمحت', 'سمحتي', 'تسمح', 'سمحتو'))
                           for t, nxt in zip(sent, sent[1:] + [''])) or _phrase(sent, ('اول', 'ما')) or
        _phrase(sent, ('بعد', 'ما')) or _phrase(sent, ('as', 'soon', 'as')) or _phrase(sent, ('in', 'case')),
        'quotation': quoted or any(t in _QUOTE for t in sent),
        'missed': _has_pairs(sent, _MISSED),
        'other_destination': _has_pairs(sent, _OTHER_DEST),
        'embedded_check': any(match(t, CHECK, _VERB_PRE) is not None for t in sent) and
        any(t in ('ان', 'لو', 'if', 'whether', 'that') for t in sent),
        'stop': has(conj, STOP, _VERB_PRE),
        'rework': has(conj, REWORK),
        'non_item_object': has(conj, NON_ITEM_OBJECT),
        'ack': any(t in ACK for t in sent),
    }


# ---------------------------------------------------------------------------------------------- per-operation reading
GO_OPS = ('resume', 'request_publish', 'request_reschedule', 'change_format', 'confirm_topaz', 'report_published',
          'update_caption', 'approve_caption', 'replace_source', 'request_rework', 'set_window', 'cancel_time')
COMPLETION_OPS = ('confirm_topaz', 'report_published')


def contradiction(op: str, text: str, quote: str) -> str | None:
    """Why the owner's own words contradict an executable reading of `op` (None: no contradiction)."""
    m = markers(text, quote)
    if not m.get('located'):
        return 'the quoted words are not in your message'
    if m['quotation']:
        return 'it is a quotation or something someone else said'
    if m['question']:
        return 'it reads as a question'
    if m['conditional']:
        return 'it is a condition ("if/when"), not an instruction'
    if op in COMPLETION_OPS or op == 'resolve_published':
        if m['missed']:
            return 'it says what should have happened, not what happened'
        if m['embedded_check']:
            return 'it asks to check, it does not state the result'
        if m['negation'] or m['not_yet']:
            return 'it is negated or not done yet'
        if m['future'] or m['progressive'] or m['future_day']:
            return 'it is about the future or still in progress'
        if op != 'confirm_topaz' and m['other_destination']:
            return 'it mentions another account or platform'
        return None
    if op in ('pause', 'skip'):
        if m['missed']:
            return 'it says what should have happened'
        if m['future'] and not m['stop']:
            return 'it is a plan for later'
        conj = m['conjunct']
        lex = STOP if op == 'pause' else SKIP
        for i, t in enumerate(conj):
            if match(t, lex, _VERB_PRE) is not None and (negated(t) or any(
                    x in _NEG for x in conj[max(0, i - 3):i])):
                return 'the stop itself is negated'
        if op == 'skip' and (has(conj, STOP, _VERB_PRE) or has(conj, RESUME, _VERB_PRE)):
            return 'it is about pausing or resuming, not skipping'
        return None
    if op == 'set_window':
        return 'it is negated' if m['missed'] else None
    if m['missed']:
        return 'it says what should have happened, not an instruction'
    if m['future']:
        return 'it is about the future, not an instruction now'
    if m['negation']:
        return 'it is negated'
    if m['stop'] and op not in ('request_rework', 'cancel_time'):
        return 'it asks to stop or wait'
    if op in ('resume', 'request_publish') and m['rework']:
        return 'it sends the item back to the editor'
    if op == 'resume' and m['non_item_object'] and not has(m['conjunct'], PUBLISH, _VERB_PRE):
        return 'it is about something other than publishing the item'
    return None


_CODE_LIKE = re.compile(r'^[a-z]{2,4}\d+$|^\d{3,}$')


def anaphor(toks) -> str | None:
    """'plural' / 'singular' reference to items under discussion, from words or attached pronouns."""
    if any(t in _PLURAL for t in toks):
        return 'plural'
    for t in toks:
        for core, s in _forms(t, _VERB_PRE, _OBJ_SUF):
            if s in ('هم', 'همش', 'وهم') and len(core) >= 2:
                return 'plural'
    if any(t in _SINGULAR for t in toks):
        return 'singular'
    for t in toks:
        for core, s in _forms(t, _VERB_PRE, _OBJ_SUF):
            if s in ('ه', 'ها', 'هوش', 'هاش', 'وه', 'وها') and len(core) >= 2 and \
                    match(core, PUBLISH | STOP | SKIP | RESUME | CHANGE | APPROVE | CANCEL, ('',), ('',)) is not None:
                return 'singular'
    return None


def broad(toks) -> bool:
    """'all', 'everything', 'كل اللي شغال' without a plural reference to a named set ('كلهم', 'all of them')."""
    for i, t in enumerate(toks):
        if t in _BROAD:
            nxt = toks[i + 1:i + 3]
            if t in ('all', 'كل') and nxt and (nxt[0] in _PLURAL or nxt[:2] == ['of', 'them'] or
                                               nxt[:2] == ['of', 'these'] or nxt[:2] == ['of', 'those']):
                continue
            return True
    return False


def supports(op: str, text: str, quote: str, args: dict | None = None) -> bool:
    """The words in the clause support this kind of action at all (a miss costs one question)."""
    args = args or {}
    m = markers(text, quote)
    if not m.get('located'):
        return False
    conj, sent = m['conjunct'], m['sentence']
    if op == 'pause':
        return has(conj, STOP, _VERB_PRE) or (has(conj, PUBLISH, _VERB_PRE) and (
            bool(_negations(conj)) or any(negated(t) for t in conj)))
    if op == 'skip':
        return has(conj, SKIP, _VERB_PRE) or (has(conj, CANCEL, _VERB_PRE) and has(conj, PUB_NOUN))
    if op in ('resume', 'request_publish'):
        if has(conj, PUBLISH, _VERB_PRE):
            return True
        if has(conj, RESUME - CONTINUE, _VERB_PRE):
            return True
        # "كمل" / "continue" / "go ahead" continue something specific: only with an item reference
        if has(conj, CONTINUE, _VERB_PRE) or _phrase(conj, ('go', 'ahead')):
            return bool(anaphor(conj)) or any(_CODE_LIKE.match(t) for t in sent)
        return False
    if op == 'request_rework':
        return m['rework']
    if op == 'confirm_topaz':
        return has(sent, TOPAZ) and (has(conj, DONE, _VERB_PRE) or 'topazed' in conj)
    if op in ('report_published', 'resolve_published'):
        return has(conj, COMPLETED_PUBLISH, _VERB_PRE) or _phrase(conj, ('went', 'live')) or \
            _phrase(conj, ('is', 'up')) or _phrase(conj, ('its', 'up'))
    if op == 'change_format':
        target = args.get('format')
        if target not in FORMAT_WORDS or not has(sent, CHANGE, _VERB_PRE):
            return False
        other = 'Story' if target == 'Post' else 'Post'

        def last(words):
            pos = -1
            for i, t in enumerate(sent):
                prev = sent[i - 1] if i else ''
                if match(t, words) is not None and prev not in ('من', 'from', 'بدل', 'instead', 'of'):
                    pos = i
            return pos
        return last(FORMAT_WORDS[target]) > last(FORMAT_WORDS[other])
    if op == 'approve_caption':
        at = next((i for i, t in enumerate(conj) if match(t, APPROVE, _VERB_PRE) is not None), None)
        if at is not None:       # "ابعته عشان اعتمده" (so that I approve it) is not an approval
            return not any(t in ('عشان', 'علشان', 'لاجل', 'so', 'to') for t in conj[:at])
        return answer(text) == 'yes'
    if op == 'update_caption':
        caption = (args.get('text') or '').strip()
        return len(caption) >= 10 and contains(text, caption) and (
            has(sent, CAPTION) or len(compact(text)) - len(compact(caption)) <= 80)
    if op == 'replace_source':
        url = args.get('url') or ''
        return bool(url) and url in decode_slack(text)
    if op == 'set_window':
        return bool(days(text, date(2000, 1, 1))) and (has(conj, PUBLISH, _VERB_PRE) or any(
            t in ('قبل', 'before', 'after', 'من', 'from', 'starting', 'بعد') for t in conj))
    if op == 'request_reschedule':
        return bool(times(text))
    if op == 'cancel_time':
        return has(conj, CANCEL, _VERB_PRE) and any(t in ('معاد', 'ميعاد', 'المعاد', 'الميعاد', 'وقت', 'time',
                                                          'schedule', 'الوقت') for t in conj)
    return False


def explicit(op: str, args: dict, text: str) -> bool:
    """Deterministic reading only (kept for the consistency check and its tests): would trusted code accept the
    whole message as an imperative/completed statement for `op`? It is NOT an authorization by itself: the
    bridge also needs the model's structured meaning, an evidence quote and bound targets."""
    op = {'resolve_outcome': 'resolve_published' if (args or {}).get('outcome') == 'published' else None}.get(op, op)
    if op is None:
        return False                     # "not published" is never executed without a question
    kind = {'resume': 'resume', 'skip': 'skip', 'confirm_topaz': 'confirm_topaz', 'change_format': 'change_format',
            'approve_caption': 'approve_caption', 'update_caption': 'update_caption',
            'resolve_published': 'resolve_published'}.get(op, op)
    if kind == 'approve_caption' and answer(text) == 'yes':
        return True
    if contradiction(kind, text, text):
        return False
    return supports(kind, text, text, args)


# ---------------------------------------------------------------------------------------------- bare answers
_PROPOSAL_ID = re.compile(r'\bb-([0-9a-f]{8})\b')


def proposal_ids(text: str) -> list[str]:
    seen = []
    for m in _PROPOSAL_ID.finditer(norm(text)):
        pid = 'B-' + m.group(1).upper()
        if pid not in seen:
            seen.append(pid)
    return seen


def answer(text: str) -> str | None:
    """A short reply that only answers: 'yes', 'no', 'ack' (thanks, nice), 'show' (send it again), else None."""
    t = _PROPOSAL_ID.sub(' ', norm(text))
    toks = [x for x in re.findall(r'[^\W_]+|👍|✅|🙏|❤️|👌', t) if x]
    if not toks or len(toks) > 8:
        return None
    if any(x in SHOW for x in toks) and not any(x in NEG_ANSWER for x in toks):
        core = [x for x in toks if x not in FILLER and x not in SHOW and x not in CAPTION and x not in AFFIRM]
        # "ابعتهولي تاني عشان اعتمده" asks to see it again; approval comes after seeing it
        if all(x in ('عشان', 'علشان', 'اعتمده', 'اعتمدها', 'اشوفه', 'اشوفها', 'me', 'it', 'to', 'the', 'draft',
                     'الكابشن', 'المسوده') or match(x, CAPTION) is not None for x in core):
            return 'show'
        return None
    neg = [x for x in toks if x in NEG_ANSWER or match(x, _lex('الغي', 'ارفض'), ('',)) is not None]
    rest = [x for x in toks if x not in FILLER]
    if neg and all(x in NEG_ANSWER or x in FILLER or match(x, _lex('الغي', 'ارفض'), ('',)) is not None
                   or x in ('عايز', 'عاوز', 'مش', 'خلاص') for x in toks):
        return 'no'
    if not rest:
        return None
    if all(x in ACK or x in AFFIRM or x in FILLER for x in toks):
        if any(x in ACK for x in toks) and not any(x in AFFIRM_COMMAND for x in toks):
            return 'ack'
        if any(x in AFFIRM for x in toks):
            return 'yes'
    return None


def approval(text: str):
    """('approve'|'reject', ids, quoted) for approval-shaped messages, else None. Decorations Slack adds or people
    type (bold, backticks, RLM/zero-width, 👍, a trailing period) do not matter; extra instructions do."""
    t = norm(text)
    quoted = bool(re.match(r'^\s*(&gt;|>)', decode_slack(text).strip())) or t.startswith('>')
    t = re.sub(r'^\s*>\s*', '', t)
    ids = proposal_ids(t)
    body = _PROPOSAL_ID.sub(' ', t)
    toks = [x for x in re.findall(r'[^\W_]+', body)]
    verb = [x for x in toks if match(x, _lex('اعتمد', 'approve', 'approved'), ('', 'و', 'ف'), _OBJ_SUF) is not None]
    rej = [x for x in toks if match(x, _lex('ارفض', 'reject', 'rejected'), ('', 'و', 'ف'), _OBJ_SUF) is not None]
    if not verb and not rej:
        if ids and all(x in FILLER or x in AFFIRM or x in ACK for x in toks):
            return ('approve', ids, quoted)       # "B-1A2B3C4D 👍"
        return None
    allowed = set(verb + rej) | FILLER | _lex('و', 'and', 'الاتنين', 'both', 'دول', 'ايوه', 'اه', 'تمام', 'ok', 'yes',
                                              'هو', 'هي', 'الكابشن', 'المسوده', 'caption', 'draft', 'كابشن')
    if any(x not in allowed for x in toks):
        return None                               # "اعتمد B-… وغيره كمان": more than an approval -> the model
    return ('reject' if rej and not verb else 'approve', ids, quoted)


# ---------------------------------------------------------------------------------------------- days and times
_WEEKDAYS = {'السبت': 5, 'سبت': 5, 'الحد': 6, 'الاحد': 6, 'حد': 6, 'احد': 6, 'الاتنين': 0, 'الاثنين': 0, 'اتنين': 0,
             'التلات': 1, 'الثلاثاء': 1, 'التلاتاء': 1, 'الاربع': 2, 'الاربعاء': 2, 'الخميس': 3, 'خميس': 3,
             'الجمعه': 4, 'جمعه': 4, 'saturday': 5, 'sunday': 6, 'monday': 0, 'tuesday': 1, 'wednesday': 2,
             'thursday': 3, 'friday': 4, 'sat': 5, 'sun': 6, 'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4}


def days(text: str, today: date) -> list[tuple[date, str]]:
    """Day expressions with their kind: 'on' (that day) or 'not_before' (from that day on)."""
    T = tokens(text)
    out = []
    i = 0
    while i < len(T):
        t, prev = T[i], (T[i - 1] if i else '')
        prev2 = T[i - 2] if i > 1 else ''
        d = None
        core = t
        for p in ('و', 'ل', 'من', 'ب'):
            if t.startswith(p) and t[len(p):] in ('بكره', 'بكرا', 'النهارده', 'النهارد'):
                core, prev = t[len(p):], p
        if core in ('بعد',) and i + 1 < len(T) and T[i + 1] in ('بكره', 'بكرا'):
            d, i = today + timedelta(days=2), i + 1
        elif core in ('بكره', 'بكرا', 'tomorrow'):
            d = today + timedelta(days=1)
            if prev == 'after' and prev2 == 'day':
                d = today + timedelta(days=2)
        elif core in ('النهارده', 'النهارد', 'النهاردا', 'today', 'tonight', 'النهارده'):
            d = today
        elif core in _WEEKDAYS or match(core, set(_WEEKDAYS), ('', 'و', 'يوم', 'ل')) is not None:
            wd = _WEEKDAYS.get(core) if core in _WEEKDAYS else next(
                v for k, v in _WEEKDAYS.items() if match(core, {k}, ('', 'و', 'ل')) is not None)
            d = today + timedelta(days=(wd - today.weekday()) % 7)
        if d is not None:
            kind = 'not_before' if prev in ('قبل', 'before', 'من', 'from', 'starting', 'after') or \
                (prev2 in ('not', 'مش') and prev in ('before', 'قبل')) else 'on'
            out.append((d, kind))
        i += 1
    for m in re.finditer(r'\b(20\d\d)-(\d\d)-(\d\d)\b', norm(text)):
        try:
            out.append((date(int(m[1]), int(m[2]), int(m[3])), 'on'))
        except ValueError:
            pass
    for m in re.finditer(r'(?<![\d:/])(\d{1,2})/(\d{1,2})(?![\d/])', norm(text)):
        try:
            d = date(today.year, int(m[2]), int(m[1]))
            out.append((d if d >= today else date(today.year + 1, d.month, d.day), 'on'))
        except ValueError:
            pass
    return out


_PM = ('pm', 'م', 'مساء', 'مساءا', 'بالليل', 'بليل', 'العصر', 'عصر', 'الضهر', 'المغرب', 'بعدالضهر')
_AM = ('am', 'ص', 'صباحا', 'الصبح', 'صبح', 'الفجر')


def times(text: str) -> list[tuple[int, int, str | None]]:
    """Clock times: (hour, minute, 'am'|'pm'|None)."""
    t = norm(text)
    out = []
    for m in re.finditer(r'(?<![\d:])(\d{1,2})\s*[:.]\s*(\d{2})(?![\d:])\s*([^\W\d_]+)?', t):
        h, mi, tail = int(m[1]), int(m[2]), (m[3] or '')
        if h <= 23 and mi <= 59:
            out.append((h, mi, 'pm' if tail in _PM else 'am' if tail in _AM else None))
    for m in re.finditer(r'(?:الساعه|الساعة|at)\s*(\d{1,2})(?![\d:.])\s*([^\W\d_]+)?', t):
        h, tail = int(m[1]), (m[2] or '')
        if h <= 23:
            out.append((h, 0, 'pm' if tail in _PM else 'am' if tail in _AM else None))
    for m in re.finditer(r'(?<![\d:.])(\d{1,2})\s*(am|pm)\b', t):
        out.append((int(m[1]), 0, m[2]))
    return out


def time_matches(h: int, mi: int, said) -> bool:
    for sh, sm, period in said:
        if sm != mi:
            continue
        if period == 'pm':
            ok = h == (sh % 12) + 12
        elif period == 'am':
            ok = h == sh % 12
        else:
            ok = h in (sh, (sh + 12) % 24) if sh < 12 else h == sh
        if ok:
            return True
    return False
