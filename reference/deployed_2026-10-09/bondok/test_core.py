import copy,json,sqlite3,tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock
from core import *
from app import accept_event,Runtime

def item(status='Unscheduled',fmt='Story',ident='1'):
    x={'id':ident,'name':'Test item','state':'active','column_values':[]}
    for k,v in [('status',status),('format',fmt),('code','LIP123'),('topaz','Topazed'),('media','m1'),('asset','file@1'),('video','video')]:
        x['column_values'].append({'id':C.get(k,k),'text':v,'value':dumps({'url':'https://www.dropbox.com/x'}) if k=='video' else None})
    return x

def setcol(x,k,text='',v=None):
    x['column_values']=[c for c in x['column_values'] if c['id']!=C.get(k,k)]+[{'id':C.get(k,k),'text':text,'value':dumps(v) if v is not None else None}]

class Tests(unittest.TestCase):
 def setUp(self):
    self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name)
    self.store=Store(p/'bondok.sqlite');self.pipe=Pipeline(p/'pipeline.sqlite')
    c=sqlite3.connect(self.pipe.path);c.executescript('CREATE TABLE locks(name TEXT PRIMARY KEY,owner TEXT,until REAL);CREATE TABLE publications(item TEXT PRIMARY KEY,stage TEXT);CREATE TABLE reservations(item TEXT PRIMARY KEY,format TEXT,style TEXT,at TEXT,committed INT,UNIQUE(format,at));CREATE TABLE media(id TEXT,item TEXT,path TEXT,source TEXT,metadata TEXT);CREATE TABLE audit_log(item TEXT,kind TEXT,detail TEXT,created REAL);');c.close()
    self.monday=Mock();self.row=item();self.monday.item.side_effect=lambda *a,**k:copy.deepcopy(self.row);self.monday.change.return_value={'id':'1'}
    self.manager=Manager(self.store,self.monday,self.pipe,'OWNER')
 def tearDown(self):self.tmp.cleanup()
 def proposal(self,op='edit',args=None):return self.manager.propose('THREAD','OWNER',op,'1',({'field':'caption','value':'caption'} if args is None else args),'test')['proposal']
 def test_owner_required(self):
    with self.assertRaises(GuardError):self.manager.propose('T','OTHER','pause','1',{},'x')
 def test_no_write_before_approval(self):self.proposal();self.monday.change.assert_not_called()
 def test_approval_wrong_actor(self):
    p=self.proposal()
    with self.assertRaises(GuardError):self.manager.approve(p,'THREAD','OTHER')
    self.monday.change.assert_not_called()
 def test_approval_wrong_thread(self):
    p=self.proposal()
    with self.assertRaises(GuardError):self.manager.approve(p,'OTHER','OWNER')
 def test_duplicate_approval(self):
    p=self.proposal();self.manager.approve(p,'THREAD','OWNER')
    with self.assertRaises(GuardError):self.manager.approve(p,'THREAD','OWNER')
    self.assertEqual(self.monday.change.call_count,1)
 def test_changed_snapshot(self):
    p=self.proposal();self.row['name']='Changed';self.manager.approve(p,'THREAD','OWNER');self.monday.change.assert_not_called()
 def test_expired_approval(self):
    p=self.proposal()
    with self.store.db() as c:c.execute('UPDATE plans SET created=0')
    with self.assertRaises(GuardError):self.manager.approve(p,'THREAD','OWNER')
 def test_receipt_prevents_write(self):
    p=self.proposal()
    with self.pipe.db() as c:c.execute("INSERT INTO publications VALUES('1','publish_requested')")
    self.manager.approve(p,'THREAD','OWNER');self.monday.change.assert_not_called()
 def test_preparation_lock_prevents_write(self):
    p=self.proposal()
    with self.pipe.lease():self.manager.approve(p,'THREAD','OWNER')
    self.monday.change.assert_not_called()
 def test_unknown_result_not_retried(self):
    p=self.proposal();self.monday.change.side_effect=TimeoutError();self.manager.approve(p,'THREAD','OWNER')
    with self.assertRaises(GuardError):self.manager.approve(p,'THREAD','OWNER')
    self.assertEqual(self.monday.change.call_count,1)
 def test_conversion_clears_schedule_qa(self):
    p=self.proposal('format',{'format':'Post'});self.manager.approve(p,'THREAD','OWNER')
    cv=self.monday.change.call_args.args[1];self.assertEqual(cv[C['format']],{'label':'Post'});self.assertEqual(cv[C['media']],'');self.assertEqual(cv[C['at']],{});self.assertEqual(cv['status'],{'label':'جاري فحص الفيديو'})
 def test_technical_column_block(self):
    for field in ('status','at','media','asset','published','board_id'):
     with self.subTest(field=field),self.assertRaises(GuardError):validate_args('edit',{'field':field,'value':'Posted'})
 def test_url_and_graphql_injection(self):
    for url in ['http://www.dropbox.com/x','https://evil.test/x','https://www.dropbox.com.evil.test/x','https://user:pass@www.dropbox.com/x']:
     with self.subTest(url=url),self.assertRaises(GuardError):validate_args('source',{'url':url})
    with self.assertRaises(GuardError):validate_args('pause',{'board_id':'2'})
 def test_protected_states(self):
    for status in ('Posted','Publishing'):
     with self.subTest(status=status),self.assertRaises(GuardError):assert_mutable(item(status),'edit')
 def test_paused_cannot_reschedule(self):
    with self.assertRaises(GuardError):assert_mutable(item('Paused'),'reschedule')
 def test_near_due_protected(self):
    x=item('Scheduled');t=datetime.now(timezone.utc)+timedelta(minutes=5);setcol(x,'at',v={'date':t.date().isoformat(),'time':t.strftime('%H:%M:%S')})
    with self.assertRaises(GuardError):assert_mutable(x,'pause')
 def test_cairo_slots_dst(self):
    self.assertTrue(valid_slot('Post',datetime(2026,10,10,21,tzinfo=TZ)))
    self.assertFalse(valid_slot('Post',datetime(2026,10,11,21,tzinfo=TZ)))
    self.assertTrue(valid_slot('Story',datetime(2026,11,1,11,tzinfo=TZ)))
    self.assertFalse(valid_slot('Story',datetime(2026,11,1,11,0,1,tzinfo=TZ)))
 def insert_media(self,duration=59.9,fmt='Story',size=299999999,edge=1080):
    m={'width':edge,'height':1920,'bytes':size,'duration':duration,'format':fmt,'qaPolicy':3,'topazed':True,'assetKey':'file@1','url':'https://www.dropbox.com/x'}
    with self.pipe.db() as c:c.execute('DELETE FROM media');c.execute('INSERT INTO media VALUES(?,?,?,?,?)',('m1','1','file','source',dumps(m)))
 def test_sixty_seconds_story_rejected(self):
    self.insert_media(duration=60)
    with self.pipe.db() as c,self.assertRaises(GuardError):self.pipe.verified(self.row,c)
 def test_long_post_allowed(self):
    self.insert_media(duration=100,fmt='Post');setcol(self.row,'format','Post')
    with self.pipe.db() as c:self.pipe.verified(self.row,c)
 def test_size_boundary_rejected(self):
    self.insert_media(size=300000000)
    with self.pipe.db() as c,self.assertRaises(GuardError):self.pipe.verified(self.row,c)
 def test_1080_boundary(self):
    self.insert_media(edge=1080)
    with self.pipe.db() as c:self.pipe.verified(self.row,c)
    self.insert_media(edge=1079)
    with self.pipe.db() as c,self.assertRaises(GuardError):self.pipe.verified(self.row,c)
 def test_changed_source_rejected(self):
    self.insert_media();setcol(self.row,'asset','file@2')
    with self.pipe.db() as c,self.assertRaises(GuardError):self.pipe.verified(self.row,c)
 def test_wrong_board_no_content_read(self):
    m=Monday('test');m.query=Mock(return_value={'items':[{'id':'2','board':{'id':'999'}}]})
    with self.assertRaises(GuardError):m.item('2')
    self.assertEqual(m.query.call_count,1)
 def test_slack_scope_and_bot_loops(self):
    b={'team_id':'T','event':{'type':'app_mention','channel':'C','user':'U'}}
    self.assertTrue(accept_event(b,'C','T',lambda x:True,'BOT'))
    self.assertFalse(accept_event(b,'OTHER','T',lambda x:True,'BOT'))
    self.assertFalse(accept_event(b,'C','OTHER',lambda x:True,'BOT'))
    b['event']['bot_id']='B';self.assertFalse(accept_event(b,'C','T',lambda x:True,'BOT'))
 def test_thread_followups_only(self):
    b={'team_id':'T','event':{'type':'message','channel':'C','user':'U','thread_ts':'1'}}
    self.assertTrue(accept_event(b,'C','T',lambda x:True,'BOT'));self.assertFalse(accept_event(b,'C','T',lambda x:False,'BOT'))
 def test_slack_receive_persists_and_deduplicates(self):
    runtime=Runtime.__new__(Runtime)
    runtime.channel='C';runtime.team='T';runtime.bot='BOT';runtime.store=self.store;runtime.wake=Mock()
    event={'type':'app_mention','channel':'C','user':'OWNER','ts':'123.456','text':'راجع الجدولة'}
    runtime.receive({'team_id':'T','event':event},None)
    runtime.receive({'team_id':'T','event':event},None)
    with self.store.db() as c:rows=c.execute('SELECT * FROM events').fetchall()
    self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'queued')
    self.assertEqual(json.loads(rows[0]['payload'])['text'],'راجع الجدولة')
    self.assertTrue(runtime.known('123.456'))
 def test_state_persisted(self):
    self.store.remember('T','user','hello');self.assertEqual(Store(self.store.path).history('T')[0]['content'],'hello')
 def test_schema_extra_actions_rejected(self):
    with self.assertRaises(GuardError):validate_args('shell',{})
    with self.assertRaises(GuardError):validate_args('reschedule',{'at':'2026-11-01T11:00:00'})
 def test_pause_removes_reservation(self):
    with self.pipe.db() as c:c.execute("INSERT INTO reservations VALUES('1','Story','LIP','2030-01-01T11:00:00+00:00',1)")
    p=self.proposal('pause',{});self.manager.approve(p,'THREAD','OWNER')
    with self.pipe.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],0)
 def test_colliding_slot_blocked(self):
    self.insert_media();t=datetime(2030,1,1,11,tzinfo=TZ);x=item('Scheduled',ident='2');utc=t.astimezone(timezone.utc);setcol(x,'at',v={'date':utc.date().isoformat(),'time':utc.strftime('%H:%M:%S')})
    with self.assertRaises(GuardError):self.pipe.reserve(self.row,t.isoformat(),[x])

if __name__=='__main__':unittest.main(verbosity=2)
