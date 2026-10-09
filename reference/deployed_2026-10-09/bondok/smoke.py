from pathlib import Path
from dotenv import dotenv_values
from core import *
e=dotenv_values('/opt/waset-bondok/.env')
s=Store('/var/lib/bondok/smoke.sqlite');m=Monday(e['MONDAY_API_TOKEN']);b=m.board()
print(dumps({'monday_connected':True,'board_id':b['id'],'board_name':b['name'],'items':len(b['items']),'columns':len(b['columns'])}),flush=True)
a=Agent(Manager(s,m,None,e['SLACK_OWNER_ID']),e['OPENROUTER_API_KEY'],e['OPENROUTER_MODEL'])
r=a.call([{'role':'user','content':'اختبار اتصال للقراءة فقط. استعمل get_board مرة مع filter فارغ ثم قل اسم البورد وعدد العناصر المتاحة. لا تقترح تعديلًا.'}],tools=TOOLS[:1],choice={'type':'function','name':'get_board'})
calls=[x for x in r['output'] if x.get('type')=='function_call']
assert len(calls)==1 and calls[0]['name']=='get_board'
c=calls[0];v=a.tool(c['name'],json.loads(c['arguments']),'smoke',e['SLACK_OWNER_ID'])
history=[{'role':'user','content':'Read-only connectivity test: state the board name and total matched items.'}]+r['output']+[{'type':'function_call_output','call_id':c['call_id'],'output':dumps(v)}]
r2=a.call(history,tools=TOOLS[:1],choice='none')
answer='\n'.join(c['text'] for o in r2['output'] if o.get('type')=='message' for c in o.get('content',[]) if c.get('type')=='output_text')
assert answer
print(dumps({'model_requested':a.model,'model_returned':r2.get('model'),'tool_call_success':True,'answer':answer}),flush=True)
