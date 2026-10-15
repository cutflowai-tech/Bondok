import json, logging, os, re, threading, time
from pathlib import Path
from dotenv import dotenv_values
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
from core import Agent,Manager,Monday,Pipeline,Store,GuardError,ServiceError,audit_board,dumps,fingerprint

LOG=logging.getLogger('bondok')
logging.basicConfig(level=logging.WARNING,format='%(asctime)s %(levelname)s %(name)s %(message)s')
LOG.setLevel(logging.INFO)
# SDK debug logs can include raw payloads; never enable in production.
ENV_PATH=Path(os.environ.get('BONDOK_ENV_FILE','/opt/waset-bondok/.env'))
REQUIRED=('SLACK_BOT_TOKEN','SLACK_APP_TOKEN','SLACK_CHANNEL_ID','SLACK_TEAM_ID','SLACK_OWNER_ID','MONDAY_API_TOKEN','OPENROUTER_API_KEY')

def configured(e):return all(e.get(k) for k in REQUIRED)
def accept_event(body,channel,team,known_thread,bot):
    e=body.get('event',{})
    if body.get('team_id')!=team or e.get('channel')!=channel or e.get('bot_id') or e.get('subtype') or e.get('user')==bot:return False
    if e.get('type')=='app_mention':return True
    return e.get('type')=='message' and bool(e.get('thread_ts')) and known_thread(e['thread_ts'])

class Runtime:
    def __init__(self,e):
        self.e=e;self.channel=e['SLACK_CHANNEL_ID'];self.team=e['SLACK_TEAM_ID'];self.store=Store(Path(e['BONDOK_STATE_DIR'])/'bondok.sqlite')
        self.manager=Manager(self.store,Monday(e['MONDAY_API_TOKEN']),Pipeline(e['PIPELINE_DB']),e['SLACK_OWNER_ID'])
        self.agent=Agent(self.manager,e['OPENROUTER_API_KEY'],e.get('OPENROUTER_MODEL','openai/gpt-6.1-sol'))
        self.app=App(token=e['SLACK_BOT_TOKEN'],ignoring_self_events_enabled=True)
        auth=self.app.client.auth_test()
        if auth['team_id']!=self.team:raise GuardError('Slack token belongs to another workspace')
        self.bot=auth['user_id']
        if self.bot!='<bondok-bot-user-id>':raise GuardError('Slack token is not Bondok')
        if '-<bondok-app-id>-' not in e['SLACK_APP_TOKEN']:raise GuardError('Socket Mode token is not Bondok')
        info=self.app.client.conversations_info(channel=self.channel)['channel']
        if not info.get('is_private') or not info.get('is_member'):raise GuardError('Bot must be invited to the configured private channel')
        self.app.event('app_mention')(self.receive)
        self.app.event('message')(self.receive)
        self.wake=threading.Event();self.stop=threading.Event()
    def known(self,ts):return bool(self.store.kv('thread:'+ts))
    def receive(self,body,logger):
        if not accept_event(body,self.channel,self.team,self.known,self.bot):return
        e=body['event'];key=self.team+':'+self.channel+':'+e['ts'];thread=e.get('thread_ts') or e['ts']
        if not e.get('user') or not e.get('text'):return
        self.store.kv('thread:'+thread,True)
        with self.store.db() as c:c.execute('INSERT OR IGNORE INTO events(id,payload,state,reply,created) VALUES(?,?,?,?,?)',(key,dumps(e),'queued',None,time.time()))
        self.wake.set()
    def post(self,text,thread=None):
        # Plain message text, no @channel or untrusted link unfurling, only configured destination.
        text=re.sub(r'<![^>]+>','[تنبيه]',text)
        return self.app.client.chat_postMessage(channel=self.channel,thread_ts=thread,text=text[:38000],unfurl_links=False,unfurl_media=False,parse='none',link_names=False)
    def worker(self):
        with self.store.db() as c:
            # A crash may leave an uncertain write; do not re-run its model/tool turn.
            c.execute("UPDATE events SET state='reply_ready',reply=? WHERE state='working'",('الخدمة اتعملها إعادة تشغيل أثناء الطلب. راجع النتيجة قبل إعادة أي اعتماد؛ مش هكرر تعديل غير مؤكد.',))
        while not self.stop.is_set():
            with self.store.db() as c:
                c.execute('BEGIN IMMEDIATE');r=c.execute("SELECT * FROM events WHERE state IN ('queued','reply_ready') ORDER BY created LIMIT 1").fetchone()
                if r and r['state']=='queued':c.execute("UPDATE events SET state='working' WHERE id=?",(r['id'],))
            if not r:self.wake.wait(3);self.wake.clear();continue
            e=json.loads(r['payload']);thread=e.get('thread_ts') or e['ts']
            if r['state']=='reply_ready':reply=r['reply']
            else:
                try:
                    if r['created']+1800<time.time():raise GuardError('الرسالة قديمة بعد توقف الخدمة؛ ابعتها من جديد لو لسه مطلوبة.')
                    reply=self.agent.answer(e['text'],thread,e['user'])
                except (GuardError,ServiceError) as ex:reply=str(ex)
                except Exception as ex:
                    LOG.error('request_failed type=%s',type(ex).__name__);reply='حصل خطأ في المراجعة. لم أؤكد تنفيذ أي تعديل. راجع الحالة قبل إعادة المحاولة.'
                with self.store.db() as c:c.execute("UPDATE events SET state='reply_ready',reply=? WHERE id=?",(reply,r['id']))
            # Mark before send: on an ambiguous network failure do not duplicate the reply or operation.
            with self.store.db() as c:c.execute("UPDATE events SET state='sending' WHERE id=?",(r['id'],))
            try:self.post(reply,thread);state='done'
            except Exception as ex:LOG.error('reply_failed type=%s',type(ex).__name__);state='send_uncertain'
            with self.store.db() as c:c.execute('UPDATE events SET state=? WHERE id=?',(state,r['id']))
            self.store.kv('last_request_at',time.time())
    def monitor_once(self):
        result=audit_board(self.manager.monday.board());issues=result['issues'];sig=fingerprint(issues)
        previous=self.store.kv('monitor_signature')
        self.store.kv('last_monitor_at',time.time())
        if previous==sig:return
        if not issues:
            if previous:self.post('مراجعة الجدولة: المشاكل اللي رصدتها في البورد اختفت. التحقق النهائي من الملفات لسه مسؤولية مسار النشر.')
        else:
            data=self.agent.call([{'role':'user','content':'هذه نتيجة مراجعة آلية للبورد. لخص المخالفات في رسالة مصرية قصيرة، لا تدّعي إصلاحها. مراقب n8n يصلح الجدولة وفق القواعد المعتمدة. البيانات: '+dumps(result)[:18000]}],tools=[],choice='none')
            reply='\n'.join(c['text'] for o in data['output'] if o.get('type')=='message' for c in o.get('content',[]) if c.get('type')=='output_text')
            if reply:self.post('مراجعة Bondok الدورية:\n'+reply)
        self.store.kv('monitor_signature',sig)
    def monitor(self):
        while not self.stop.wait(max(300,int(self.e.get('MONITOR_INTERVAL_SECONDS','1800')))):
            try:self.monitor_once()
            except Exception as ex:
                LOG.error('monitor_failed type=%s',type(ex).__name__)
                last=self.store.kv('monitor_error_at') or 0
                if time.time()-last>7200:
                    try:self.post('مراجعة Bondok الدورية لم تكتمل بسبب مشكلة اتصال. الأتمتة الأساسية مستقلة عنه؛ أعد المراجعة عند رجوع الاتصال.');self.store.kv('monitor_error_at',time.time())
                    except Exception:pass
    def run(self):
        threading.Thread(target=self.worker,daemon=True,name='bondok-worker').start()
        threading.Thread(target=self.monitor,daemon=True,name='bondok-monitor').start()
        LOG.info('ready model=%s board=5105608159 slack_private_channel_verified=true',self.agent.model)
        self.store.kv('service_ready_at',time.time())
        SocketModeHandler(self.app,self.e['SLACK_APP_TOKEN']).start()

def main():
    reported=False
    while True:
        e=dict(dotenv_values(ENV_PATH))
        if not configured(e):
            if not reported:LOG.warning('waiting_for_secure_slack_setup');reported=True
            time.sleep(15);continue
        if e.get('MONDAY_BOARD_ID')!='5105608159':raise GuardError('Board scope cannot be changed in env')
        Runtime(e).run();return

if __name__=='__main__':
    try:main()
    except Exception as ex:
        LOG.error('startup_failed type=%s',type(ex).__name__)
        raise SystemExit(1)
