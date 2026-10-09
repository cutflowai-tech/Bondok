from __future__ import annotations
import contextlib, hashlib, html, json, math, os, re, sqlite3, time, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx

BOARD='5105608159'
TZ=ZoneInfo('Africa/Cairo')
C={'format':'color_mm7xm9b6','caption':'long_text_mm7x2ay1','topaz':'color_mm7xe2j2','source':'link_mm7x8dy7','code':'text_mm7xqn4e','notes':'text_mm7xaf3t','folder':'link_mm7xaep2','post_link':'link_mm7xb56a','video':'link_mm7ywc0w','collab':'text_mm7yhjf1','style':'text_mm7y5kd1','variety':'text_mm7yjmqd','owner':'multiple_person_mm7yy4tk','at':'date_mm7y8s9t','media':'text_mm7yp8h','asset':'text_mm7yy451','issue':'long_text_mm7zbtbn','measurements':'text_mm7zjt4m','processed':'text_mm7z139h','checked':'date_mm7zd2b9','system':'long_text_mm7ysrbz'}
SLOTS={5:(21,0),0:(21,0),2:(22,45),3:(21,0)}
STORY_SLOTS={(11,0),(14,0),(18,0),(21,0),(22,0)}
class GuardError(Exception): pass
class ServiceError(Exception): pass

def dumps(x): return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def fingerprint(x): return hashlib.sha256(dumps(x).encode()).hexdigest()
def val(item,key):
    v=next((x.get('value') for x in item.get('column_values',[]) if x['id']==C.get(key,key)),None)
    try:return json.loads(v) if isinstance(v,str) else v
    except (ValueError,TypeError):return None

def txt(item,key):return next((x.get('text') or '' for x in item.get('column_values',[]) if x['id']==C.get(key,key)),'').strip()
def instant(s):return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(timezone.utc)
def at(item):
    v=val(item,'at') or {}
    try:
        if v.get('date') and v.get('time'):return instant(v['date']+'T'+v['time']+'+00:00')
        d=val(item,'date4') or {};h=val(item,'hour_mm7xy9cf') or {}
        if d.get('date') and h.get('hour') is not None:return datetime.fromisoformat(d['date']).replace(hour=int(h['hour']),minute=int(h.get('minute',0)),tzinfo=TZ).astimezone(timezone.utc)
    except (ValueError,TypeError): pass
    return None

def valid_slot(fmt,t):
    t=t.astimezone(TZ)
    return t.second==0 and t.microsecond==0 and ((fmt=='Story' and (t.hour,t.minute) in STORY_SLOTS) or (fmt=='Post' and SLOTS.get(t.weekday())==(t.hour,t.minute)))

def render_proposal(p):
    b=p['change'];a=b['args'];op=b['operation']
    names={'edit':'تعديل بيانات','format':'تغيير النوع','source':'استبدال رابط الفيديو','topaz':'تأكيد تجهيز Topaz','pause':'إيقاف','resume':'استئناف مع إعادة الفحص','recheck':'إعادة فحص','reschedule':'تعديل الموعد','archive':'أرشفة','add':'إضافة فيديو','comment':'إضافة تعليق'}
    fields={'caption':'الكابشن','notes':'الملاحظات','code':'الكود','collab':'التعاون','variety':'تنويع الستوري','owner':'المسؤول'}
    detail=''
    if op=='edit':detail=fields[a['field']]+': '+a['value']
    elif op=='format':detail='النوع الجديد: '+a['format']+' — سيتم إلغاء الموعد والاعتماد السابق وإعادة الفحص.'
    elif op=='source':detail='الرابط الجديد: '+a['url']+' — سيتم إعادة الفحص وتأكيد Topaz.'
    elif op=='topaz':detail='Topaz: '+('مكتمل حسب تأكيدك' if a['confirmed'] else 'غير مكتمل')
    elif op=='reschedule':detail='الموعد بتوقيت القاهرة: '+instant(a['at']).astimezone(TZ).strftime('%Y-%m-%d %H:%M')
    elif op=='comment':detail=a['text']
    elif op=='add':detail='النوع: '+a['format']+' | الكود: '+a['code']+'\n'+a['url']
    return '*اقتراح '+p['proposal']+' — لم ينفذ*\n'+names[op]+': '+b['name']+'\n'+detail+'\nالسبب: '+b['reason']+'\nللاعتماد خلال 30 دقيقة، رد في نفس الثريد: `'+p['approval_command']+'`'

def compact(item):
    return {'id':item['id'],'name':item['name'],'state':item.get('state'),'columns':{x['id']:x.get('text','') for x in item.get('column_values',[])},'url':f'https://waset-co-studio.monday.com/boards/{BOARD}/pulses/{item["id"]}'}

def assert_mutable(item,op,now=None):
    now=now or datetime.now(timezone.utc)
    if item.get('state') not in (None,'active'):raise GuardError('العنصر مؤرشف أو محذوف.')
    status=txt(item,'status')
    if status in ('Posted','Publishing') or (val(item,'post_link') or {}).get('url') or txt(item,'text_mm7yfqhb'):raise GuardError('للعنصر نشر مسجل أو قيد التنفيذ؛ التعديل محظور.')
    if status in ('Paused','Skipped') and op not in ('resume','edit','archive'):raise GuardError('العنصر متوقف. اطلب استئنافه صراحة أولًا.')
    t=at(item)
    if status=='Scheduled' and (not t or t<=now+timedelta(minutes=10)):raise GuardError('العنصر داخل نافذة النشر أو متأخر؛ راجع محاولة النشر أولًا.')

class Store:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.path=str(path)
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,payload TEXT,state TEXT,reply TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,thread TEXT,role TEXT,text TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,thread TEXT,actor TEXT,body TEXT,before_hash TEXT,state TEXT,result TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,kind TEXT,detail TEXT,created REAL);
            ''')
    def db(self):
        c=sqlite3.connect(self.path,timeout=20);c.row_factory=sqlite3.Row
        return _connection(c)
    def log(self,kind,data):
        with self.db() as c:c.execute('INSERT INTO audit(kind,detail,created)VALUES(?,?,?)',(kind,dumps(data),time.time()))
    def history(self,thread):
        with self.db() as c: rows=c.execute('SELECT role,text FROM messages WHERE thread=? ORDER BY id DESC LIMIT 16',(thread,)).fetchall()
        return [{'role':r['role'],'content':r['text']} for r in reversed(rows)]
    def remember(self,thread,role,text):
        with self.db() as c:c.execute('INSERT INTO messages(thread,role,text,created)VALUES(?,?,?,?)',(thread,role,text[:18000],time.time()))
    def kv(self,key,value=None):
        with self.db() as c:
            if value is not None:c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(key,dumps(value)));return value
            r=c.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone();return json.loads(r[0]) if r else None

@contextlib.contextmanager
def _connection(c):
    try:
        with c:yield c
    finally:c.close()

class Monday:
    def __init__(self,token):
        self.http=httpx.Client(timeout=35,headers={'Authorization':token,'API-Version':'2026-07','Content-Type':'application/json'},follow_redirects=False)
    def query(self,q,v=None):
        try:
            r=self.http.post('https://api.monday.com/v2',json={'query':q,'variables':v or {}})
            if r.status_code!=200:raise ServiceError('Monday HTTP '+str(r.status_code))
            data=r.json()
            if data.get('errors') or 'data' not in data:raise ServiceError('Monday رفض الطلب؛ لم يتم تأكيد التنفيذ.')
            return data['data']
        except httpx.HTTPError:raise ServiceError('انقطع اتصال Monday؛ نتيجة أي تعديل غير مؤكدة، لن أعيده تلقائيًا.') from None
    def board(self):
        b=self.query('query($b:[ID!]){boards(ids:$b){id name columns{id title type settings_str} groups{id title} items_page(limit:500){cursor items{id name state updated_at column_values{id text value}}}}}',{'b':[BOARD]})['boards']
        if len(b)!=1 or b[0]['id']!=BOARD:raise GuardError('بورد السوشيال غير متاح.')
        b=b[0];items=b.pop('items_page');b['items']=items['items'];cursor=items.get('cursor')
        while cursor:
            if len(b['items'])>=5000:raise GuardError('البورد تجاوز حد القراءة؛ المراجعة غير مكتملة.')
            page=self.query('query($c:String!){next_items_page(cursor:$c,limit:500){cursor items{id name state updated_at column_values{id text value}}}}',{'c':cursor})['next_items_page'];b['items']+=page['items'];cursor=page.get('cursor')
        return b
    def item(self,i,updates=False):
        i=str(i)
        if not re.fullmatch(r'\d{1,20}',i):raise GuardError('معرف العنصر غير صالح.')
        # Only metadata is read until membership is verified. No foreign item contents reach the model.
        meta=self.query('query($i:[ID!]){items(ids:$i){id board{id}}}',{'i':[i]})['items']
        if len(meta)!=1 or meta[0]['board']['id']!=BOARD:raise GuardError('هذا العنصر خارج بورد السوشيال أو غير موجود.')
        extra=' updates(limit:20){id text_body created_at}' if updates else ''
        row=self.query('query($i:[ID!]){items(ids:$i){id name state updated_at board{id} column_values{id text value}'+extra+'}}',{'i':[i]})['items'][0]
        if row['board']['id']!=BOARD:raise GuardError('تم نقل العنصر خارج البورد.')
        return row
    def change(self,i,cv):
        self.item(i)
        return self.query('mutation($b:ID!,$i:ID!,$v:JSON!){change_multiple_column_values(board_id:$b,item_id:$i,column_values:$v,create_labels_if_missing:false){id}}',{'b':BOARD,'i':str(i),'v':dumps(cv)})
    def create(self,name,fmt,code,url):
        cv={'status':{'label':'جاري فحص الفيديو'},C['format']:{'label':fmt},C['code']:code,C['source']:{'url':url,'text':'Source video'},C['topaz']:{'label':'Not yet'}}
        return self.query('mutation($b:ID!,$n:String!,$v:JSON!){create_item(board_id:$b,item_name:$n,column_values:$v){id}}',{'b':BOARD,'n':name,'v':dumps(cv)})
    def update(self,i,text):
        self.item(i)
        return self.query('mutation($i:ID!,$t:String!){create_update(item_id:$i,body:$t){id}}',{'i':str(i),'t':html.escape(text)})
    def archive(self,i):
        self.item(i)
        return self.query('mutation($i:ID!){archive_item(item_id:$i){id}}',{'i':str(i)})

class Pipeline:
    def __init__(self,path):self.path=str(path)
    def db(self):
        if not Path(self.path).is_file():raise GuardError('قاعدة بيانات النشر غير متاحة؛ التعديلات متوقفة بأمان.')
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row;return _connection(c)
    @contextlib.contextmanager
    def lease(self):
        owner='bondok-'+uuid.uuid4().hex
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM locks WHERE name=?',('preparation',)).fetchone()
            if old and old['until']>time.time():raise GuardError('تجهيز/مراجعة الجدولة شغال الآن. اطلب اقتراحًا جديدًا بعد انتهائه.')
            c.execute('INSERT OR REPLACE INTO locks VALUES(?,?,?)',('preparation',owner,time.time()+300))
        try:yield owner
        finally:
            with self.db() as c:c.execute('DELETE FROM locks WHERE name=? AND owner=?',('preparation',owner))
    def receipt_guard(self,c,i):
        if c.execute('SELECT 1 FROM publications WHERE item=?',(str(i),)).fetchone():raise GuardError('توجد محاولة نشر محفوظة للعنصر؛ لا يجوز تعديلها أو تكرارها.')
    def cancel_reservation(self,i):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');self.receipt_guard(c,i)
            old=c.execute('SELECT * FROM reservations WHERE item=?',(str(i),)).fetchone()
            if old:c.execute('INSERT INTO audit_log(item,kind,detail,created) VALUES(?,?,?,?)',(str(i),'bondok_invalidate',dumps(dict(old)),time.time()))
            c.execute('DELETE FROM reservations WHERE item=?',(str(i),))
    def verified(self,item,c):
        self.receipt_guard(c,item['id'])
        r=c.execute('SELECT * FROM media WHERE id=? AND item=?',(txt(item,'media'),str(item['id']))).fetchone()
        if not r:raise GuardError('الفيديو غير معتمد من فحص التجهيز.')
        m=json.loads(r['metadata']);fmt=txt(item,'format')
        try:good=all(math.isfinite(float(m[k])) and float(m[k])>0 for k in ['width','height','bytes','duration'])
        except (KeyError,ValueError,TypeError):good=False
        if not good or min(float(m['width']),float(m['height']))<1080 or float(m['bytes'])>=300000000 or (fmt=='Story' and float(m['duration'])>=60):raise GuardError('قياسات الفيديو لا تسمح بالجدولة.')
        if fmt not in ('Story','Post') or m.get('format')!=fmt or m.get('qaPolicy')!=3 or m.get('topazed') is not True or txt(item,'topaz')!='Topazed':raise GuardError('تأكيد Topaz أو نسخة فحص الجودة غير صالح.')
        if m.get('assetKey')!=txt(item,'asset') or not m.get('url') or m['url']!=(val(item,'video') or {}).get('url'):raise GuardError('نسخة الفيديو أو رابط النشر تغير؛ أعد الفحص.')
        return m
    def reserve(self,item,target,items):
        fmt=txt(item,'format');t=instant(target)
        if t<=datetime.now(timezone.utc)+timedelta(minutes=10) or not valid_slot(fmt,t):raise GuardError('اختر موعدًا مستقبليًا من جدول النشر المتفق عليه.')
        for other in items:
            if other['id']!=item['id'] and txt(other,'format')==fmt and txt(other,'status') in ('Scheduled','Posted') and at(other)==t:raise GuardError('الموعد محجوز لعنصر آخر.')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');self.verified(item,c)
            for r in c.execute('SELECT item,at FROM reservations WHERE format=?',(fmt,)):
                if r['item']!=str(item['id']) and instant(r['at'])==t:raise GuardError('الموعد محجوز في سجل النشر.')
            c.execute('INSERT OR REPLACE INTO reservations VALUES(?,?,?,?,0)',(str(item['id']),fmt,(re.match(r'([A-Za-z]{2,3})',txt(item,'code')) or ['',''])[1].upper(),t.isoformat()))
        return t
    def commit(self,item,t):
        with self.db() as c:c.execute('UPDATE reservations SET committed=1 WHERE item=? AND at=?',(str(item),t.isoformat()))

EDIT_FIELDS={'caption','notes','code','collab','variety','owner'}
OPERATIONS={'edit','format','source','topaz','pause','resume','recheck','reschedule','archive','add','comment'}
def validate_args(op,a):
    if op not in OPERATIONS or not isinstance(a,dict):raise GuardError('عملية غير مدعومة.')
    keys={'edit':{'field','value'},'format':{'format'},'source':{'url'},'topaz':{'confirmed'},'reschedule':{'at'},'add':{'name','format','code','url'},'comment':{'text'}}.get(op,set())
    if set(a)!=keys:raise GuardError('بيانات التعديل ناقصة أو فيها حقول غير مسموحة.')
    if len(dumps(a))>9000:raise GuardError('التعديل أكبر من الحد المسموح.')
    if op=='edit':
        if a['field'] not in EDIT_FIELDS or not isinstance(a['value'],str):raise GuardError('هذا عمود فني تديره الأتمتة أو قيمة غير صالحة.')
        if a['field']=='owner' and not re.fullmatch(r'\d{1,20}',a['value']):raise GuardError('استخدم Monday user ID صحيحًا للمسؤول.')
        if a['field']=='code' and not re.fullmatch(r'[A-Za-z]{2,3}[ _#-]?\d+[A-Za-z0-9 _-]*',a['value']):raise GuardError('الكود يبدأ بحرفين أو ثلاثة ثم رقم.')
    if op in ('format','add') and a['format'] not in ('Story','Post'):raise GuardError('النوع يجب أن يكون Post أو Story.')
    if op in ('source','add'):
        from urllib.parse import urlsplit
        u=urlsplit(a['url'])
        if u.scheme!='https' or u.hostname not in ('www.dropbox.com','dropbox.com','dl.dropboxusercontent.com') or u.username or u.password:raise GuardError('مطلوب رابط Dropbox آمن.')
    if op=='topaz' and type(a['confirmed']) is not bool:raise GuardError('تأكيد Topaz يجب أن يكون صريحًا.')
    if op=='add':
        if not isinstance(a['name'],str) or not a['name'].strip() or len(a['name'])>250:raise GuardError('اسم العنصر غير صالح.')
        validate_args('edit',{'field':'code','value':a['code']})
    if op=='reschedule':
        try:
            if datetime.fromisoformat(a['at'].replace('Z','+00:00')).tzinfo is None:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise GuardError('الموعد مطلوب بصيغة ISO مع المنطقة الزمنية.')
    if op=='comment' and (not isinstance(a['text'],str) or not a['text'].strip()):raise GuardError('نص التعليق فارغ.')

def audit_board(b):
    issues=[];seen={};scheduled=[]
    for i in b['items']:
        status=txt(i,'status');fmt=txt(i,'format');t=at(i)
        if status!='Scheduled':continue
        reasons=[]
        if not t:reasons.append('موعد ناقص أو غير صالح')
        elif not valid_slot(fmt,t):reasons.append('خارج أوقات النشر المعتمدة')
        elif (fmt,t.isoformat()) in seen:reasons.append('تعارض موعد مع '+seen[(fmt,t.isoformat())])
        if t:seen[(fmt,t.isoformat())]=i['id'];scheduled.append((t,i))
        if not txt(i,'media') or not txt(i,'asset') or txt(i,'topaz')!='Topazed':reasons.append('بيانات اعتماد الفيديو ناقصة')
        if t and t<datetime.now(timezone.utc)-timedelta(hours=2):reasons.append('متأخر أكثر من ساعتين؛ يحتاج مراجعة النشر')
        if reasons:issues.append({'id':i['id'],'name':i['name'],'reasons':reasons})
    for fmt in ('Post','Story'):
        previous=None
        for t,i in sorted(scheduled,key=lambda x:x[0]):
            if txt(i,'format')!=fmt:continue
            prefix=re.match(r'\s*([A-Za-z]{2,3})[ _#-]?\d',txt(i,'code'));prefix=prefix[1].upper() if prefix else ''
            if previous and prefix and prefix==previous:issues.append({'id':i['id'],'name':i['name'],'reasons':['نفس بادئة الستايل متتابعة؛ راجع وجود بدائل جاهزة قبل التغيير']})
            previous=prefix
    return {'checked':len(b['items']),'scheduled':len(scheduled),'issues':issues,'note':'قراءة بورد؛ القياس النهائي ونسخة الملف يتحقق منهم مسار n8n قبل النشر.'}

class Manager:
    def __init__(self,store,monday,pipeline,owner):self.store,self.monday,self.pipeline,self.owner=store,monday,pipeline,owner
    def propose(self,thread,actor,op,item_id,args,reason):
        if actor!=self.owner:raise GuardError('التعديلات متاحة لصاحب الحساب فقط؛ يمكنك طلب قراءة أو اقتراح نص.')
        validate_args(op,args)
        item=None if op=='add' else self.monday.item(item_id)
        if item:assert_mutable(item,op)
        body={'operation':op,'item_id':str(item_id),'args':args,'reason':str(reason)[:1000],'name':item['name'] if item else args['name']}
        pid='B-'+uuid.uuid4().hex[:8].upper()
        with self.store.db() as c:c.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)',(pid,thread,actor,dumps(body),fingerprint(item), 'pending',None,time.time()))
        return {'proposal':pid,'change':body,'expires_in_minutes':30,'approval_command':'اعتمد '+pid,'applied':False}
    def approve(self,pid,thread,actor):
        if actor!=self.owner:raise GuardError('الاعتماد لصاحب الحساب فقط.')
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone()
            if not r or r['thread']!=thread or r['actor']!=actor:raise GuardError('الاقتراح ليس في هذه المحادثة.')
            if r['state']!='pending':raise GuardError('تم التعامل مع هذا الاقتراح بالفعل؛ لن أكرره.')
            if r['created']+1800<time.time():raise GuardError('انتهت صلاحية الاقتراح؛ اطلب اقتراحًا محدثًا.')
            c.execute("UPDATE plans SET state='applying' WHERE id=?",(pid,))
        b=json.loads(r['body']);op=b['operation'];a=b['args'];i=b['item_id'];validate_args(op,a)
        try:
            with self.pipeline.lease():
                item=None if op=='add' else self.monday.item(i)
                if fingerprint(item)!=r['before_hash']:raise GuardError('العنصر اتغير بعد الاقتراح؛ اقرأه وراجع اقتراحًا جديدًا.')
                if item:
                    assert_mutable(item,op)
                    with self.pipeline.db() as c:self.pipeline.receipt_guard(c,i)
                if op=='add':result=self.monday.create(a['name'],a['format'],a['code'],a['url'])
                elif op=='comment':result=self.monday.update(i,a['text'])
                elif op=='archive':self.pipeline.cancel_reservation(i);result=self.monday.archive(i)
                else:
                    cv={}
                    invalidates=op in ('format','source','topaz','resume','recheck','pause') or (op=='edit' and a['field']=='code')
                    if op=='edit':
                        field=a['field'];value=a['value']
                        cv[C[field]]={'text':value} if field=='caption' else {'personsAndTeams':[{'id':int(value),'kind':'person'}]} if field=='owner' else value
                    elif op=='format':cv[C['format']]={'label':a['format']}
                    elif op=='source':cv[C['source']]={'url':a['url'],'text':'Source video'};cv[C['topaz']]={'label':'Not yet'}
                    elif op=='topaz':cv[C['topaz']]={'label':'Topazed' if a['confirmed'] else 'Not yet'}
                    elif op=='reschedule':
                        t=self.pipeline.reserve(item,a['at'],self.monday.board()['items']);local=t.astimezone(TZ)
                        cv={'date4':{'date':local.date().isoformat()},'hour_mm7xy9cf':{'hour':local.hour,'minute':local.minute},C['at']:{'date':t.date().isoformat(),'time':t.strftime('%H:%M:%S')},'status':{'label':'Scheduled'}}
                    if invalidates:
                        if txt(item,'status') in ('Paused','Skipped') and op!='resume':raise GuardError('لا أستأنف عنصرًا متوقفًا ضمن تعديل آخر. اعتمد استئنافًا صريحًا أولًا.')
                        self.pipeline.cancel_reservation(i)
                        cv.update({C['at']:{},'date4':{},'hour_mm7xy9cf':{},C['media']:'',C['processed']:'',C['checked']:{},'status':{'label':'Paused' if op=='pause' else 'جاري فحص الفيديو'}})
                        cv[C['issue']]={'text':'متوقف بطلب صاحب الحساب' if op=='pause' else 'بانتظار فحص التجهيز مجددًا قبل الجدولة'}
                    cv[C['system']]={'text':f'Bondok {pid}: '+b['reason']}
                    result=self.monday.change(i,cv)
                    if op=='reschedule':self.pipeline.commit(i,t)
            self.store.log('applied',{'proposal':pid,'change':b,'result':result})
            state='applied';reply='تم تنفيذ '+pid+' على '+b['name']+('. رجع لفحص التجهيز قبل أي جدولة.' if op in ('format','source','topaz','resume','recheck') else '.')
        except GuardError as e:state='rejected';reply=str(e)
        except Exception:
            state='uncertain';reply='نتيجة التنفيذ غير مؤكدة. أوقفت تكراره؛ راجع العنصر قبل أي محاولة جديدة.'
        with self.store.db() as c:c.execute('UPDATE plans SET state=?,result=? WHERE id=?',(state,reply,pid))
        return reply

TOOLS=[
 {'type':'function','name':'get_board','description':'Read current social board schema and items. Optional filter finds name/code/status. Complete board audit uses audit_schedule.','parameters':{'type':'object','properties':{'filter':{'type':'string'}},'required':['filter'],'additionalProperties':False},'strict':True},
 {'type':'function','name':'get_item','description':'Read one item and recent updates, limited to the social board.','parameters':{'type':'object','properties':{'item_id':{'type':'string'}},'required':['item_id'],'additionalProperties':False},'strict':True},
 {'type':'function','name':'audit_schedule','description':'Check current schedule for conflicts, invalid slots, missing verification, late items and repeated styles. Read-only.','parameters':{'type':'object','properties':{},'additionalProperties':False},'strict':True},
 {'type':'function','name':'propose_change','description':'Prepare one owner-approved change. No mutation yet. Operations: edit {field:caption|notes|code|collab|variety|owner,value:string}; format {format:Post|Story}; source {url}; topaz {confirmed:bool}; pause/resume/recheck/archive {}; reschedule {at:ISO_with_offset}; add {name,format,code,url}; comment {text}. args_json is serialized JSON. Item ID empty for add. Do not use topaz true without explicit owner confirmation that processing was completed.','parameters':{'type':'object','properties':{'operation':{'type':'string','enum':sorted(OPERATIONS)},'item_id':{'type':'string'},'args_json':{'type':'string'},'reason':{'type':'string'}},'required':['operation','item_id','args_json','reason'],'additionalProperties':False},'strict':True}]

class Agent:
    def __init__(self,manager,key,model='openai/gpt-6.1-sol'):
        self.manager=manager;self.key=key;self.model=model
        self.policy=Path(__file__).with_name('policy.txt').read_text()
        self.http=httpx.Client(timeout=150,follow_redirects=False)
    def call(self,history,tools=TOOLS,choice='auto'):
        payload={'model':self.model,'input':history,'instructions':self.policy+'\nCurrent time: '+datetime.now(TZ).isoformat(),'store':False,'reasoning':{'effort':'medium'},'tools':tools,'tool_choice':choice,'max_output_tokens':4000}
        try:
            r=self.http.post('https://openrouter.ai/api/v1/responses',headers={'Authorization':'Bearer '+self.key,'X-Title':'Waset Bondok'},json=payload)
            if r.status_code!=200:raise ServiceError('اتصال الموديل رجع HTTP '+str(r.status_code))
            result=r.json()
            if result.get('error') or result.get('status') not in (None,'completed'):raise ServiceError('الموديل لم يكمل الرد؛ لم أنفذ تعديلًا.')
            if not result.get('output'):raise ServiceError('الموديل لم يرجع ردًا صالحًا.')
            self.manager.store.log('model_usage',{'model':result.get('model',self.model),'usage':result.get('usage',{})})
            return result
        except httpx.HTTPError:raise ServiceError('اتصال الموديل اتقطع. الطلب يحتاج إعادة محاولة منك.') from None
    def tool(self,name,args,thread,actor):
        m=self.manager
        if name=='get_board':
            b=m.monday.board();f=args.get('filter','').casefold();matched=[compact(i) for i in b.pop('items') if not f or f in dumps(compact(i)).casefold()]
            return {**b,'items':matched[:100],'matched':len(matched),'truncated':len(matched)>100}
        if name=='get_item':return m.monday.item(args['item_id'],updates=True)
        if name=='audit_schedule':return audit_board(m.monday.board())
        if name=='propose_change':return m.propose(thread,actor,args['operation'],args['item_id'],json.loads(args['args_json']),args['reason'])
        raise GuardError('أداة غير مسموحة.')
    def answer(self,text,thread,actor):
        m=self.manager;text=re.sub(r'<@[A-Z0-9]+>','',text).strip()
        approval=re.fullmatch(r'(?:اعتمد|approve)\s+(B-[0-9A-F]{8})',text,re.I)
        if approval:return m.approve(approval[1].upper(),thread,actor)
        history=m.store.history(thread)+[{'role':'user','content':f'Slack actor {actor}: '+text[:12000]}];proposals=[]
        m.store.remember(thread,'user',f'{actor}: '+text)
        for _ in range(6):
            result=self.call(history);output=result['output'];history+=output;calls=[x for x in output if x.get('type')=='function_call']
            if not calls:
                reply='\n'.join(y['text'] for x in output if x.get('type')=='message' for y in x.get('content',[]) if y.get('type')=='output_text')
                if not reply:raise ServiceError('لم يصل رد نصي من الموديل.')
                # Exact proposal is rendered by trusted application code, independent of the model's summary.
                for p in proposals:reply+='\n\n'+render_proposal(p)
                m.store.remember(thread,'assistant',reply);return reply
            for call in calls[:8]:
                try:
                    if len(call.get('arguments',''))>12000:raise GuardError('طلب الأداة طويل جدًا.')
                    if call['name']=='propose_change' and len(proposals)>=3:raise GuardError('راجع الثلاثة اقتراحات أولًا.')
                    value=self.tool(call['name'],json.loads(call['arguments']),thread,actor)
                    if call['name']=='propose_change':proposals.append(value)
                except (GuardError,ServiceError,ValueError,KeyError,TypeError) as e:value={'error':str(e)}
                history.append({'type':'function_call_output','call_id':call['call_id'],'output':dumps(value)[:90000]})
        raise ServiceError('وصلت لحد خطوات المراجعة. قسّم الطلب؛ لم أنفذ أي اقتراح دون اعتماد.')
