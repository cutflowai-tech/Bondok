"""Build v2 workflows from the original exports (deterministic, reviewable).

    python3 workflows/build.py            -> workflows/dist/*.json

Integration nodes (Monday reads, Dropbox, Instagram Graph, caption LLM, editor
subitems) are reused from the originals with their credentials and settings.
Decision logic moves to the shared handler (helper /v2 routes). Workflow IDs
and names are preserved so the cutover replaces each workflow in place.
"""
from __future__ import annotations

import copy
import json
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
from waset_ops import board  # noqa: E402

ORIG = HERE / 'original'
DIST = HERE / 'dist'
HELPER = 'python3 /home/node/.n8n-files/waset-social/helper.py '
MAX_COMMAND = 120_000          # Linux MAX_ARG_STRLEN is 131072 bytes per argument
SOCIAL_COLS = json.dumps(board.SNAPSHOT_COLUMNS)


def load(name):
    return json.loads((ORIG / name).read_text())


class Flow:
    def __init__(self, original: dict, name_suffix=''):
        self.orig = {n['name']: n for n in original['nodes']}
        self.meta = original
        self.nodes: dict[str, dict] = {}
        self.conns: dict[str, dict] = {}
        self.col = 0

    # -------------------------------------------------------------- node factories
    def _add(self, node):
        assert node['name'] not in self.nodes, node['name']
        node.setdefault('id', str(uuid.uuid5(uuid.NAMESPACE_URL, 'waset-v2/' + node['name'])))
        node.setdefault('position', [240 * (len(self.nodes) % 12), 200 * (len(self.nodes) // 12)])
        self.nodes[node['name']] = node
        return node['name']

    def reuse(self, name, new_name=None, **overrides):
        n = copy.deepcopy(self.orig[name])
        n.pop('id', None)
        if new_name:
            n['name'] = new_name
        for k, v in overrides.items():
            if k == 'parameters':
                n['parameters'] = v
            else:
                n[k] = v
        # v1 bug: an object literal ending in '}}' closes the n8n {{ }} expression early, so n8n
        # fails with "invalid syntax" before any request is sent (Dropbox share links never created).
        body = n.get('parameters', {}).get('jsonBody')
        if isinstance(body, str) and "requested_visibility:'public'}})" in body:
            n['parameters']['jsonBody'] = body.replace("requested_visibility:'public'}})",
                                                       "requested_visibility:'public'} })")
        return self._add(n)

    def code(self, name, js, *, each=False, on_error=None):
        n = {'name': name, 'type': 'n8n-nodes-base.code', 'typeVersion': 2,
             'parameters': {'jsCode': js.strip()}}
        if each:
            n['parameters']['mode'] = 'runOnceForEachItem'
        if on_error:
            n['onError'] = on_error
        return self._add(n)

    def cond(self, name, expr):
        return self._add({'name': name, 'type': 'n8n-nodes-base.if', 'typeVersion': 2.3, 'parameters': {
            'conditions': {'options': {'caseSensitive': True, 'leftValue': '', 'typeValidation': 'strict',
                                       'version': 2},
                           'conditions': [{'id': str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
                                           'leftValue': '={{ ' + expr + ' }}', 'rightValue': True,
                                           'operator': {'type': 'boolean', 'operation': 'true',
                                                        'singleValue': True}}],
                           'combinator': 'and'}, 'options': {}}})

    def monday(self, name, body_expr, on_error='continueErrorOutput'):
        n = {'name': name, 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.3, 'parameters': {
            'url': 'https://api.monday.com/v2', 'method': 'POST',
            'options': {'timeout': 60000, 'response': {'response': {'responseFormat': 'json', 'neverError': False}}},
            'authentication': 'predefinedCredentialType', 'nodeCredentialType': 'mondayComApi', 'sendBody': True,
            'contentType': 'json', 'specifyBody': 'json', 'jsonBody': '={{ ' + body_expr + ' }}'},
             'credentials': copy.deepcopy(self.monday_cred)}
        if on_error:
            n['onError'] = on_error
        return self._add(n)

    @property
    def monday_cred(self):
        return next(n['credentials'] for n in self.orig.values()
                    if n.get('credentials', {}).get('mondayComApi'))

    def helper(self, name, route, body_js, *, each=False, fail=True, on_error=None):
        """Input (Code) -> Local n8n (Execute Command) -> parse (Code).

        Handled helper failures arrive as {ok:false,...} on stdout (exit 0), so
        the diagnostic is preserved in the execution; parse throws with it.
        """
        ret = 'return {json:{command}};' if each else 'return [{json:{command}}];'
        js = (f'const body={body_js};\n'
              f'const payload=Buffer.from(JSON.stringify({{path:"{route}",body}})).toString("base64");\n'
              f"const command='{HELPER}'+payload;\n"
              f"if(command.length>{MAX_COMMAND})throw new Error('Helper payload too large ('+command.length+' bytes); refusing to truncate');\n"
              + ret)
        a = self.code(name + ' — Input', js, each=each)
        b = self._add({'name': name + ' — Local n8n', 'type': 'n8n-nodes-base.executeCommand', 'typeVersion': 1,
                       'parameters': {'executeOnce': False, 'command': '={{ $json.command }}'}})
        check = ("if($json.exitCode!==undefined&&$json.exitCode!==0)throw new Error('Helper process failed (exit '+$json.exitCode+'): '+String($json.stderr||'').slice(-600));\n"
                 "const r=JSON.parse($json.stdout||'{}');\n"
                 + ("if(r.ok===false)throw new Error('Handler '+(r.kind||'error')+': '+r.error);\n" if fail else ''))
        c = self.code(name, check + ('return {json:r};' if each else 'return [{json:r}];'), each=each,
                      on_error=on_error)
        self.link(a, b)
        self.link(b, c)
        return a, c

    def link(self, src, dst, out=0):
        outs = self.conns.setdefault(src, {}).setdefault('main', [])
        while len(outs) <= out:
            outs.append([])
        outs[out].append({'node': dst, 'type': 'main', 'index': 0})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    def link_ai(self, model, chain):
        self.conns.setdefault(model, {}).setdefault('ai_languageModel', [[]])[0].append(
            {'node': chain, 'type': 'ai_languageModel', 'index': 0})

    def export(self, settings_overrides=None):
        w = {k: copy.deepcopy(self.meta[k]) for k in ('id', 'name', 'settings') if k in self.meta}
        w['settings'].update(settings_overrides or {})
        w['nodes'] = list(self.nodes.values())
        w['connections'] = self.conns
        w['active'] = False      # activation is a cutover step, never part of import
        w['meta'] = {'waset': 'v2', 'built_from': self.meta.get('versionId')}
        return w


# ---------------------------------------------------------------------------- shared JS
SNAPSHOT = ('query($b:[ID!]!){boards(ids:$b){items_page(limit:500){cursor items{id name group{id} '
            'column_values(ids:' + SOCIAL_COLS + '){id text value}}}}}')
SNAPSHOT_NEXT = ('query($c:String!){next_items_page(cursor:$c,limit:500){cursor items{id name group{id} '
                 'column_values(ids:' + SOCIAL_COLS + '){id text value}}}}')
COMPACT = ("const compact=i=>({id:i.id,name:i.name,group:i.group||null,column_values:(i.column_values||[])"
           ".filter(c=>c.text||c.value).map(c=>({id:c.id,text:c.text,value:c.value}))});")
MISSING_CODE = ('Code is missing or invalid on the social board, so no project folder can be created. Add the Code '
                '(for example LIP12) or a Dropbox link to the item')
# One classification for every Dropbox failure in WF1 (selection and delivery): 'config' = permanent for the current
# evidence (the item waits for a changed link/folder/Code or an explicit recheck), 'infra' = temporary (bounded
# retries, then one escalation). R5 M10.
DROPBOX_CLASSIFY = r"""
const classify=(raw,j)=>{
const e=(raw&&typeof raw==='object')?raw:{};
// n8n may carry the message as a plain string, and appends " [line N]" to Code node errors.
const msg=String((typeof raw==='string'&&raw)||e.message||e.description||j.error_summary||j.message||'Dropbox request failed')
  .replace(/\s*\[line [^\]]*\]\s*$/,'');
// Typed failures raised by our own Code nodes: "[config] text" / "[editor] text" (permanent, item-local).
const typed=msg.match(/^\s*(?:Error:\s*)?\[(config|editor)\]\s*([\s\S]*)$/);
if(typed)return {text:typed[2].trim().slice(0,400),kind:typed[1]};
// Status from structured HTTP fields or n8n's "409 - {...}" / "status code 503" forms only: a number
// elsewhere in free text (an item name like "Calli 403", an address) says nothing (audit MP4).
const code=String(e.httpCode||e.statusCode||e.status||(e.response&&e.response.status)||j.statusCode||
  (msg.match(/^\s*([45]\d\d) - /)||msg.match(/status code ([45]\d\d)\b/)||[])[1]||'');
const blob=msg+' '+String(e.description||'');
// A bare Dropbox error tag thrown by a Code node (e.g. "path/not_found/..") is Dropbox's own answer.
const bare=(msg.trim().match(/^([a-z_]+(?:\/[a-z_]+)*)\/*(?:\.\.?)?$/)||[])[1]||'';
// Owner-readable text instead of raw API JSON; the Dropbox error tag is kept in brackets.
const tag=String(e.error_summary||j.error_summary||(blob.match(/"error_summary"\s*:\s*"([^"]+)"/)||[])[1]||bare||'').replace(/\/+(\.\.?)?$/,'');
const PERMANENT=/^(path\/(not_found|malformed_path|no_write_permission|insufficient_space|conflict|disallowed_name)|path_lookup\/(not_found|malformed_path)|shared_link_(not_found|access_denied|is_directory)|unsupported_link_type|email_not_verified)/;
// Only a Dropbox 4xx about the request itself (or a bare permanent tag) blocks the content. 401/403 (expired or
// revoked token, app permission) affect every item, 408/429 are temporary, and no status at all is a system problem.
const credential=(code==='401'||code==='403')&&!/^shared_link/.test(tag);
const config=!credential&&((/^4/.test(code)&&!['408','429'].includes(code))||(!code&&!!bare&&PERMANENT.test(tag)));
const known={'shared_link_not_found':'The Dropbox link on the board no longer works. Replace the folder link on the board.',
'shared_link_access_denied':'Dropbox refused access to the link on the board. Check the link\'s sharing settings.',
'path/not_found':'The Dropbox folder or file was not found. Check the folder link on the board.',
'path/malformed_path':'The Dropbox path is invalid. Check the folder link on the board.',
'path/no_write_permission':'The connected Dropbox account may not write to this folder.',
'path/insufficient_space':'The connected Dropbox account is full.'};
const head=tag.split('/')[0],text=known[tag]||known[head]||null;
const out=config?(text||('Dropbox rejected the request'+(tag?'':': '+msg.slice(0,200))))+(tag?' (Dropbox: '+tag+')':'')
  :'Temporary Dropbox problem'+(code?' ('+code+')':'')+': '+msg.slice(0,200);
return {text:out.slice(0,400),kind:config?'config':'infra'};};
"""


def social_snapshot(f: Flow, prefix: str):
    """Paginated full read of board 5105608159 (refuses partial snapshots)."""
    start = f.code(prefix + ' Start', 'return [{json:{accum:[],gql:{query:' + json.dumps(SNAPSHOT)
                   + ',variables:{b:["5105608159"]}}}}];')
    page = f.code(prefix + ' Page Request', 'return $input.all();')
    read = f.monday(prefix + ' Read Page', 'JSON.stringify($json.gql)', on_error=None)
    collect = f.code(prefix + ' Collect',
                     "if($json.errors)throw new Error(JSON.stringify($json.errors));\n"
                     f"const s=$('{prefix} Page Request').item.json;\n"
                     'const p=$json.data?.boards?.[0]?.items_page||$json.data?.next_items_page;\n'
                     "if(!p)throw new Error('Missing board page; refusing a partial snapshot');\n"
                     'return [{json:{accum:[...s.accum,...p.items],more:!!p.cursor,gql:{query:'
                     + json.dumps(SNAPSHOT_NEXT) + ',variables:{c:p.cursor}}}}];')
    more = f.cond(prefix + ' More Pages?', '$json.more===true')
    snap = f.code(prefix + ' Snapshot', 'return [{json:{items:$json.accum}}];')
    f.chain(start, page, read, collect, more)
    f.link(more, page, 0)
    f.link(more, snap, 1)
    return start, snap


# ============================================================================ WF2
def build_wf2():
    o = load('pUIshuf16zIYoYRz__2_Publish_When_Due.json')
    f = Flow(o)
    trig = f.reuse('Time Trigger')
    cfg = f.code('Configuration', "const config={armed:true,worker:'wf2-'+$execution.id};\n"
                                  'if(!config.armed)return [];\nreturn [{json:config}];')
    f.link(trig, cfg)

    # ---- display synchronization (applies committed state; decides nothing)
    take_in, take = f.helper('Display Sync — Take', '/v2/outbox/take',
                             "{kinds:['monday','source_monday'],worker:$('Configuration').first().json.worker,limit:10}")
    jobs = f.code('Sync Jobs', 'return ($json.jobs||[]).map(j=>({json:j}));')
    mut = f.code('Build Sync Mutation', r"""
const j=$json,p=j.payload;
if(j.kind==='source_monday'){
  return {json:{job:j.id,gql:{query:'mutation($b:ID!,$i:ID!,$v:JSON!){change_column_value(board_id:$b,item_id:$i,column_id:"color_mm1ryfcb",value:$v){id}}',variables:{b:'5091110326',i:String(p.source_item_id),v:JSON.stringify({label:p.label})}}}};
}
let q='mutation($b:ID!,$i:ID!,$v:JSON!){change_multiple_column_values(board_id:$b,item_id:$i,column_values:$v,create_labels_if_missing:false){id}';
const vars={b:'5105608159',i:String(p.item_id),v:JSON.stringify(p.columns||{})};
if(p.group){q=q.replace('$v:JSON!)','$v:JSON!,$g:String!)')+' move_item_to_group(item_id:$i,group_id:$g){id}';vars.g=p.group}
const guard=p.guard&&Object.keys(p.guard).length?p.guard:null;
return {json:{job:j.id,item:String(p.item_id),guard,gql:{query:q+'}',variables:vars}}};
""", each=True)
    # Compare-before-write: a status or human column a person changed since v2 last wrote it is not
    # overwritten (audit MS4); the job is retried after WF1 has observed the person's edit.
    guarded = f.cond('Guarded Sync?', '!!$json.guard')
    g_read = f.monday('Read Board Before Sync', "JSON.stringify({query:'query($i:[ID!],$c:[String!]){items(ids:$i){"
                      "column_values(ids:$c){id text value} } }',variables:{i:[$json.item],c:Object.keys($json.guard)} })",
                      on_error='continueRegularOutput')
    g_check = f.code('Check Board Before Sync', r"""
const m=$('Build Sync Mutation').item.json;
if($json.error||$json.errors)return {json:{...m,conflict:null,readError:String(($json.error&&$json.error.message)||JSON.stringify($json.errors)).slice(0,300)}};
if(!($json.data?.items||[]).length)return {json:{...m,conflict:null,readError:'item not found on the board'}};
const cols=$json.data.items[0].column_values||[];
const norm=(c,kind)=>{if(!c)return null;
  if(kind==='long'){try{const v=JSON.parse(c.value||'null');if(v&&typeof v.text==='string')return v.text.trim()||null}catch(e){}}
  return String(c.text||'').trim()||null};
// 'was' lists every value the board may legitimately show (last confirmed + writes already in flight).
const changed=Object.entries(m.guard).filter(([id,g])=>{const cur=norm(cols.find(c=>c.id===id),g.kind);
  const ok=[].concat(g.was===undefined?null:g.was).map(v=>v??null);
  return !ok.includes(cur)&&cur!==(g.new??null)}).map(([id])=>id);
return {json:{...m,conflict:changed.length?'conflict: changed on the board by a person ('+changed.join(',')+')':null}};
""", each=True)
    conflict = f.cond('Sync Conflict?', '!!$json.conflict || !!$json.readError')
    c_ack_in, c_ack = f.helper('Display Sync — Conflict Ack', '/v2/outbox/ack', r"""({id:$json.job,
worker:$('Configuration').first().json.worker,ok:false,error:$json.conflict||('read before sync failed: '+$json.readError)})""",
                               each=True, fail=False)
    apply_ = f.monday('Apply Display Sync', 'JSON.stringify($json.gql)', on_error='continueRegularOutput')
    ack_in, ack = f.helper('Display Sync — Ack', '/v2/outbox/ack', r"""(()=>{
const job=$('Build Sync Mutation').item.json.job;
const err=$json.error?(($json.error.message||JSON.stringify($json.error))):($json.errors?JSON.stringify($json.errors):null);
return {id:job,worker:$('Configuration').first().json.worker,ok:!err&&!!$json.data,error:err?String(err).slice(0,500):null};})()""",
                           each=True, fail=False)
    f.chain(cfg, take_in)
    f.chain(take, jobs, mut, guarded)
    f.link(guarded, g_read, 0)
    f.link(guarded, apply_, 1)
    f.chain(g_read, g_check, conflict)
    f.link(conflict, c_ack_in, 0)
    f.link(conflict, apply_, 1)
    f.chain(apply_, ack_in)

    # ---- due work from durable state (empty queue ends cleanly: no Monday call)
    due_in, due = f.helper('Due Work', '/v2/publish/due', "{worker:$('Configuration').first().json.worker,limit:5}")
    f.link(cfg, due_in)
    items = f.code('Due Items', 'return ($json.work||[]).map(w=>({json:w}));')
    loop = f.reuse('Each Due Item')
    f.chain(due, items, loop)
    is_rec = f.cond('Reconcile Attempt?', "$json.kind==='reconcile'")
    f.link(loop, is_rec, 1)
    done = f.code('Publication Item Finished', 'return [{json:{done:true}}];')
    f.link(done, loop)

    # reconcile one specific unknown attempt using its container status
    chk_r = f.reuse('Check Container', 'Check Unknown Container', onError='continueRegularOutput')
    f.nodes[chk_r]['parameters']['url'] = "=https://graph.facebook.com/v26.0/{{ $json.container_id }}?fields=status_code,status"
    rec_in, rec = f.helper('Reconcile Attempt', '/v2/publish/reconcile', r"""{attemptId:$('Each Due Item').item.json.attempt_id,
containerStatus:$json.status_code||null,error:$json.error?String($json.error.message||'query failed').slice(0,200):null}""",
                           fail=False)
    f.link(is_rec, chk_r, 0)
    f.chain(chk_r, rec_in)
    f.link(rec, done)

    # fresh refresh of the item + its source project through the trusted read
    q = f.code('Fresh Item — Query', r"""
const id=$('Each Due Item').item.json.item_id;
return [{json:{queryBody:JSON.stringify({query:'query($ids:[ID!]){items(ids:$ids){id name group{id} board{id} column_values(ids:""" + SOCIAL_COLS.replace('"', '\\"') + r"""){id text value}}}',variables:{ids:[id]}})}}];""")
    read = f.monday('Read Fresh Item', '$json.queryBody')
    src_q = f.code('Source Status — Query', r"""
if($json.errors)throw new Error(JSON.stringify($json.errors));
const i=$json.data?.items?.[0];
if(!i||i.board?.id!=='5105608159')throw new Error('Item is not on the social board');
const sid=(i.column_values||[]).find(c=>c.id==='text_mm7y4h4a')?.text||null;
return [{json:{fresh:i,sid,queryBody:JSON.stringify({query:'query($ids:[ID!]){items(ids:$ids){id column_values(ids:["color_mm1ryfcb"]){id text}}}',variables:{ids:[sid||'0']}})}}];""",
                   on_error='continueErrorOutput')
    read_src = f.monday('Read Source Status', '$json.queryBody')
    claim_in, claim = f.helper('Claim Publication', '/v2/publish/claim', r"""(()=>{const c=$('Source Status — Query').item.json;
const s=($json.data?.items||[])[0];
const status=s?((s.column_values||[]).find(x=>x.id==='color_mm1ryfcb')?.text||null):null;
const {group,board,...fresh}=c.fresh;
return {itemId:$('Each Due Item').item.json.item_id,worker:$('Configuration').first().json.worker,
freshItem:{...fresh,group},sourceStatus:status};})()""", on_error='continueErrorOutput')
    claimed = f.cond('Publication Claimed?', '$json.claimed===true')
    f.link(is_rec, q, 1)
    f.chain(q, read, src_q, read_src, claim_in)
    for n in (read, src_q, read_src):
        f.link(n, done, 1)
    f.link(claim, claimed)
    f.link(claim, done, 1)
    f.link(claimed, done, 1)

    resume = f.cond('Resume Existing Container?', '!!$json.container_id')
    body = f.code('Create Media Body', r"""
const p=$('Claim Publication').item.json.payload;
const body={media_type:p.media_type,video_url:p.video_url};
if(p.media_type==='REELS')Object.assign(body,{caption:p.caption,share_to_feed:p.share_to_feed===true});
return [{json:{createBody:body}}];""")
    create = f.reuse('Create Container')
    created = f.code('Container Created', "if(!$json.id)throw new Error('No Instagram container ID; attempt left pre-commit');"
                                          'return [{json:{containerId:$json.id}}];', on_error='continueErrorOutput')
    save_in, save = f.helper('Save Container', '/v2/publish/container', r"""{attemptId:$('Claim Publication').item.json.attempt_id,
worker:$('Configuration').first().json.worker,fence:$('Claim Publication').item.json.fence,containerId:$json.containerId}""",
                             on_error='continueErrorOutput')
    active = f.code('Active Container', r"""
const c=$('Claim Publication').item.json;
const id=$json.container_id||$json.containerId||c.container_id;
if(!id)throw new Error('Missing durable container');
return [{json:{containerId:id,attempt:0}}];""")
    f.link(claimed, resume, 0)
    f.link(resume, active, 0)
    f.link(resume, body, 1)
    f.chain(body, create, created, save_in)
    f.link(save, active)
    f.link(create, done, 1)
    f.link(created, done, 1)
    f.link(save, done, 1)

    poll = f.code('Poll Context', 'return [{json:{attempt:($json.attempt||0)+1}}];')
    wait = f.reuse('Wait For Processing')
    renew_in, renew = f.helper('Renew Publication Lease', '/v2/publish/renew', r"""{attemptId:$('Claim Publication').item.json.attempt_id,
worker:$('Configuration').first().json.worker,fence:$('Claim Publication').item.json.fence}""", on_error='continueErrorOutput')
    check = f.reuse('Check Container')
    f.nodes[check]['parameters']['url'] = "=https://graph.facebook.com/v26.0/{{ $('Active Container').item.json.containerId }}?fields=status_code,status"
    status = f.code('Container Status', "return [{json:{...$json,attempt:$('Poll Context').item.json.attempt}}];")
    finished = f.reuse('Container Finished?')
    keep = f.cond('Keep Polling?', "$json.status_code==='IN_PROGRESS'&&$json.attempt<20")
    ab_in, ab = f.helper('Abandon Attempt', '/v2/publish/abandon', r"""{attemptId:$('Claim Publication').item.json.attempt_id,
worker:$('Configuration').first().json.worker,fence:$('Claim Publication').item.json.fence,
reason:'Container status '+($json.status_code||'unknown')+' after '+$json.attempt+' polls (30 s each)'}""", fail=False)
    f.chain(active, poll, wait, renew_in)
    f.chain(renew, check, status, finished)
    f.link(renew, done, 1)
    f.link(check, done, 1)
    f.link(finished, keep, 1)
    f.link(keep, poll, 0)
    f.link(keep, ab_in, 1)
    f.link(ab, done)

    verify = f.reuse('Verify Source Revision')
    f.nodes[verify]['parameters']['jsonBody'] = ("={{ JSON.stringify({path:String($('Claim Publication').item.json"
                                                 ".source_asset).split('@')[0]}) }}")
    src_res = f.code('Source Revision Result', "return [{json:{sourceAsset:($json.id&&$json.rev&&!$json.error)?"
                                               "$json.id+'@'+$json.rev:'unverifiable'}}];")
    # R5 M17: the delivered copy (the shared link Instagram fetched) must still be the Dropbox object bound when it
    # was delivered. Read through the same link; `rev` is always reported, `id` when Dropbox includes it.
    dverify = f.reuse('Verify Source Revision', 'Verify Delivered Copy')
    f.nodes[dverify]['parameters']['url'] = 'https://api.dropboxapi.com/2/sharing/get_shared_link_metadata'
    f.nodes[dverify]['parameters']['jsonBody'] = (
        "={{ JSON.stringify({url:String($('Claim Publication').item.json.payload.video_url||'')"
        ".replace(/([?&])raw=1(&|$)/,(m,a,b)=>b?a:'')}) }}")
    del_res = f.code('Delivered Revision Result', "const s=$('Source Revision Result').item.json;\n"
                                                  "return [{json:{sourceAsset:s.sourceAsset,deliveredAsset:($json.rev&&"
                                                  "!$json.error)?($json.id||'')+'@'+$json.rev:'unverifiable'}}];")
    commit_in, commit = f.helper('Commit Publication', '/v2/publish/commit', r"""{attemptId:$('Claim Publication').item.json.attempt_id,
worker:$('Configuration').first().json.worker,fence:$('Claim Publication').item.json.fence,
sourceAsset:$json.sourceAsset,deliveredAsset:$json.deliveredAsset,containerStatus:$('Container Status').item.json.status_code}""",
                                 on_error='continueErrorOutput')
    committed = f.cond('Committed?', '$json.committed===true')
    f.link(finished, verify, 0)
    f.link(verify, src_res, 0)
    f.link(verify, src_res, 1)         # Dropbox unavailable -> commit refuses ('unverifiable')
    f.link(src_res, dverify)
    f.link(dverify, del_res, 0)
    f.link(dverify, del_res, 1)
    f.link(del_res, commit_in)
    f.link(commit, committed)
    f.link(commit, done, 1)
    f.link(committed, done, 1)

    publish = f.reuse('Publish To Instagram', onError='continueRegularOutput')
    p = f.nodes[publish]['parameters']
    p['options'] = {'response': {'response': {'fullResponse': True, 'neverError': True}}, 'timeout': 120000}
    p['jsonBody'] = "={{ JSON.stringify({creation_id:$('Active Container').item.json.containerId}) }}"
    res_in, res = f.helper('Record Publication Result', '/v2/publish/result', r"""(()=>{
const st=$json.statusCode,b=$json.body||{};
const id=st===200&&b.id?String(b.id):null;
const definitive=!id&&st>=400&&st<500&&!!b.error;   // provider rejected the request
const err=id?null:(b.error?JSON.stringify(b.error).slice(0,600):($json.error?String($json.error.message||$json.error):'no media id returned'));
return {attemptId:$('Claim Publication').item.json.attempt_id,worker:$('Configuration').first().json.worker,
mediaId:id,error:err,httpStatus:st||null,definitive};})()""")
    published = f.cond('Published?', "$json.stage==='published'")
    link_ = f.reuse('Get Permalink')
    f.nodes[link_]['parameters']['url'] = "=https://graph.facebook.com/v26.0/{{ $json.media_id }}?fields=permalink"
    f.nodes[link_]['onError'] = 'continueRegularOutput'
    ev_in, ev = f.helper('Save Permalink', '/v2/publish/evidence', r"""{attemptId:$('Claim Publication').item.json.attempt_id,
permalink:$json.permalink||null}""", fail=False)
    f.link(committed, publish, 0)
    f.chain(publish, res_in)
    f.chain(res, published)
    f.link(published, link_, 0)
    f.link(published, done, 1)
    f.chain(link_, ev_in)
    f.link(ev, done)
    return f.export()


# ============================================================================ WF3
def build_wf3():
    o = load('WasetSocialScheduleGuard__3_Schedule_Supervisor.json')
    f = Flow(o)
    trig = f.reuse('Every 30 Minutes')
    cfg = f.code('Configuration', "const config={armed:true,runId:'wf3-'+$execution.id};\n"
                                  'if(!config.armed)return [];\nreturn [{json:config}];')
    run_in, run = f.helper('Supervise Schedule', '/v2/monitor/run', "{runId:$('Configuration').first().json.runId}")
    f.chain(trig, cfg, run_in)
    # Manual run is inspection only: it cannot write (the old Manual Audit wrote).
    manual = f.reuse('Manual Audit', 'Manual Read-only Inspection')
    insp_in, insp = f.helper('Inspect Schedule (read-only)', '/v2/monitor/inspect', '{}')
    f.chain(manual, insp_in)
    return f.export()


# ============================================================================ WF1
def build_wf1():
    o = load('qI1N5VNgpRjnZAKH__1_Prepare_and_Schedule.json')
    f = Flow(o)
    trig = f.reuse('Time Trigger')
    cfg = f.code('Configuration', "const config={armed:true,runId:'wf1-'+$execution.id,folderRoot:'/Social Media/Production',limit:20};\n"
                                  'if(!config.armed)return [];\nreturn [{json:config}];')
    start_in, start = f.helper('Run Start', '/v2/run/start', "{kind:'wf1',runId:$('Configuration').first().json.runId}")
    acquired = f.cond('Run Acquired?', '$json.acquired===true')
    f.chain(trig, cfg, start_in)
    f.link(start, acquired)

    # source projects (integration read of board 5091110326)
    names = ['Source Start', 'Source Page Request', 'Source Read Page', 'Source Collect', 'Source More Pages?',
             'Source Snapshot']
    for n in names:
        f.reuse(n)
    f.chain(*names[:5])
    f.link('Source More Pages?', 'Source Page Request', 0)
    f.link('Source More Pages?', 'Source Snapshot', 1)
    f.link(acquired, 'Source Start', 0)

    s_start, s_snap = social_snapshot(f, 'Social')
    f.link('Source Snapshot', s_start)

    plan_in, plan = f.helper('Import Plan', '/v2/import/plan', r"""(()=>{
const txt=(it,id)=>((it.column_values||[]).find(c=>c.id===id)?.text||'').trim();
const val=(it,id)=>{try{return JSON.parse((it.column_values||[]).find(c=>c.id===id)?.value||'null')}catch{return null}};
const norm=x=>String(x||'').trim().toUpperCase().replace(/\s+/g,'');
const all=$('Source Snapshot').first().json.items,counts={};
for(const s of all){const c=norm(txt(s,'text_mm066x8y'));if(c)counts[c]=(counts[c]||0)+1}
const sources=all.filter(s=>['Post','Story'].includes(txt(s,'color_mm1ryfcb'))).map(s=>({id:s.id,name:s.name,
 code:norm(txt(s,'text_mm066x8y')),format:txt(s,'color_mm1ryfcb'),duplicate:counts[norm(txt(s,'text_mm066x8y'))]>1,
 link:val(s,'link_mm06bswn')?.url||null,owner_ids:(val(s,'project_owner')?.personsAndTeams||[]).map(p=>p.id)}));
const social=$json.items.map(i=>({id:i.id,source_item:txt(i,'text_mm7y4h4a'),code:txt(i,'text_mm7xqn4e'),format:txt(i,'color_mm7xm9b6')}));
return {sources,social};})()""")
    f.link(s_snap, plan_in)
    has_creates = f.cond('Items To Create?', '($json.creates||[]).length>0')
    build = f.code('Build Create Mutations', r"""
const cs=$json.creates,out=[];
for(let x=0;x<cs.length;x+=20){const b=cs.slice(x,x+20),vars={b:'5105608159'},defs=['$b:ID!'],parts=[];
 b.forEach((a,j)=>{vars['v'+j]=JSON.stringify(a.columns);vars['n'+j]=a.name;vars['g'+j]=a.group;defs.push('$v'+j+':JSON!','$n'+j+':String!','$g'+j+':String!');
 parts.push('s'+a.key+':create_item(board_id:$b,group_id:$g'+j+',item_name:$n'+j+',column_values:$v'+j+'){id}')});
 out.push({json:{gql:{query:'mutation('+defs.join(',')+'){'+parts.join(' ')+'}',variables:vars}}})}
return out;""")
    apply_ = f.reuse('Apply Source Sync', 'Create Social Items', onError='continueRegularOutput')
    rec_in, rec = f.helper('Record Created Items', '/v2/import/record', r"""(()=>{
if($json.errors||$json.error)throw new Error('Item creation failed: '+JSON.stringify($json.errors||$json.error).slice(0,400));
return {pairs:Object.entries($json.data||{}).filter(([k,v])=>v&&v.id).map(([k,v])=>({source:k.slice(1),social:v.id}))};})()""",
                             each=True)
    f.link(plan, has_creates)
    f.link(has_creates, build, 0)
    f.chain(build, apply_, rec_in)

    # observe the board in chunks (human edits -> validated commands)
    chunks = f.code('Board Chunks', COMPACT + r"""
const txt=(it,id)=>((it.column_values||[]).find(c=>c.id===id)?.text||'').trim();
const items=$('Social Snapshot').first().json.items.map(compact);
const canceled=$('Source Snapshot').first().json.items.filter(s=>txt(s,'color_mm1ryfcb')==='Canceled').map(s=>({id:s.id,format:'Canceled'}));
const out=[];let cur=[],size=0;
for(const i of items){const n=JSON.stringify(i).length;if(cur.length&&size+n>60000){out.push(cur);cur=[];size=0}cur.push(i);size+=n}
if(cur.length)out.push(cur);
return out.map((c,k)=>({json:{items:c,sources:k===0?canceled:[]}}));""")
    obs_in, obs = f.helper('Observe Board', '/v2/board/observe',
                           "{items:$json.items,sources:$json.sources,caller:'wf1'}", each=True)
    f.link(has_creates, chunks, 1)
    f.link(rec, chunks)
    f.chain(chunks, obs_in)
    one = f.code('Observation Done', 'return [{json:{edits:$input.all().reduce((n,x)=>n+(x.json.edits||[]).length,0)}}];')
    f.link(obs, one)
    miss_in, miss = f.helper('Board Missing Check', '/v2/board/missing', "{ids:$('Social Snapshot').first().json.items.map(i=>i.id)}")
    q_in, q = f.helper('Preparation Queue', '/v2/prep/queue', "{limit:$('Configuration').first().json.limit}")
    f.chain(one, miss_in)
    f.link(miss, q_in)
    queue = f.code('Queue Items', "const w=$json.work||[];return w.length?w.map(x=>({json:x})):[{json:{empty:true}}];")
    loop = f.reuse('Each Content Item')
    f.chain(q, queue, loop)

    ctx = f.code('Item Context', r"""
const x=$json;if(x.empty)return [{json:{...x,valid:false}}];
const txt=(it,id)=>((it.column_values||[]).find(c=>c.id===id)?.text||'').trim();
const val=(it,id)=>{try{return JSON.parse((it.column_values||[]).find(c=>c.id===id)?.value||'null')}catch{return null}};
const source=$('Source Snapshot').first().json.items.find(s=>String(s.id)===String(x.source_item_id))||null;
const srcLink=source?val(source,'link_mm06bswn')?.url:null;
// R5 B3: a file the owner selected (Dropbox Link / Slack) outranks the project folder: it is resolved through its
// own link and the folder is never listed for this item.
const owner=x.selection_mode==='owner_selected_file'&&!!x.selected_url;
const folder=owner?'':(x.folder_url||[x.file_url,srcLink].find(u=>u&&u.includes('/scl/fo/'))||'');
return [{json:{...x,itemId:x.item_id,source,ownerSelected:owner,selectedUrl:owner?x.selected_url:null,
folderUrl:owner?'':(folder||[x.file_url,srcLink].find(u=>u&&/dropbox/.test(u))||''),valid:true}}];""")
    f.link(loop, ctx, 1)
    finished = f.code('Item Finished', 'return [{json:{done:true}}];')
    f.link(finished, loop)
    valid = f.cond('Has Work?', '$json.valid===true')
    f.link(ctx, valid)
    f.link(valid, finished, 1)

    # caption draft (separate from media; failures never abort the run)
    needs = f.cond('Needs Caption?', '$json.needs_caption===true&&!!$json.source')
    f.link(valid, needs, 0)
    brief_q = f.code('Read Client Brief — Query', "return [{json:{queryBody:JSON.stringify({query:'query($ids:[ID!]){items(ids:$ids){id name updates(limit:100){text_body created_at}}}',variables:{ids:[$('Item Context').item.json.source.id]}})}}];")
    brief = f.reuse('Read Client Brief')
    pick = f.code('Pick Client Brief', r"""
if($json.errors)throw new Error(JSON.stringify($json.errors));
const ctx=$('Item Context').item.json,project=$json.data?.items?.[0];
const strip=t=>t.replace(/https?:\/\/\S+/g,'').trim();
const ups=(project?.updates||[]).map(u=>({text:(u.text_body||'').trim(),at:u.created_at})).filter(u=>strip(u.text).length>=60).sort((a,b)=>a.at.localeCompare(b.at));
const b=ups[0];return [{json:{title:project?.name||ctx.name,brief:b?strip(b.text).replace(/[{}]/g,m=>m==='{'?'(':')').slice(0,6000):''}}];""",
                  on_error='continueErrorOutput')
    need_in, need = f.helper('Caption Needed', '/v2/caption/needed', "{itemId:$('Item Context').item.json.itemId,title:$json.title,brief:$json.brief}",
                             on_error='continueErrorOutput')
    gen = f.cond('Generate Draft?', '$json.needed===true')
    write = f.reuse('Write Caption', onError='continueRegularOutput')
    model = f.reuse('OpenRouter Model')
    f.link_ai(model, write)
    f.nodes[write]['parameters']['text'] = "=Video title: {{ $('Pick Client Brief').item.json.title }}\n\nClient brief (script, notes and style):\n{{ $('Pick Client Brief').item.json.brief }}"
    draft_in, draft = f.helper('Save Caption Draft', '/v2/caption/draft', r"""(()=>{
let t=String($json.text||'').replace(/^```[a-z]*\s*/i,'').replace(/```\s*$/,'').replace(/^caption:\s*/i,'').trim();
return {requestId:'draft:'+$('Caption Needed').item.json.input_hash,itemId:$('Item Context').item.json.itemId,
inputHash:$('Caption Needed').item.json.input_hash,text:t||null,model:'openai/gpt-5.6-sol',
error:$json.error?String($json.error.message||$json.error).slice(0,300):(t?null:'empty model output')};})()""",
                                 on_error='continueErrorOutput')
    cont = f.code('Continue Preparation', "return [{json:$('Item Context').item.json}];")
    f.link(needs, brief_q, 0)
    f.link(needs, cont, 1)
    f.chain(brief_q, brief, pick, need_in)
    f.link(brief, cont, 1)
    f.link(pick, cont, 1)
    f.link(need, gen)
    f.link(need, cont, 1)
    f.link(gen, write, 0)
    f.link(gen, cont, 1)
    f.chain(write, draft_in)
    f.link(draft, cont)
    f.link(draft, cont, 1)

    # Dropbox folder + exact file selection (existing explainable rules)
    has_folder = f.reuse('Has Folder?')
    for n in ('New Folder Context', 'Create Project Folder', 'Check Folder Creation', 'Share New Folder',
              'Find Folder Link', 'New Folder Ready', 'Folder Context', 'Folder Metadata', 'Plan Listing',
              'Files Page Request', 'List Files', 'Collect Files', 'More Dropbox Files?', 'Candidates',
              'Existing File Link', 'File Share Context', 'File Link Exists?', 'Share Final File', 'New File Link'):
        f.reuse(n)
    # R5-LOW-06: never a folder named after a missing Code ('/Social Media/Production/null/Story' was shared by
    # unrelated items). Same Code pattern as rules.CODE_RE.
    f.nodes['New Folder Context']['parameters']['jsCode'] = (
        "const x=$('Item Context').item.json;\n"
        "if(!/^\\s*[a-z]{2,3}\\s*[#_\\- ]?\\s*\\d+/i.test(String(x.code??'')))throw new Error('[config] " + MISSING_CODE +
        "');\n"
        "return [{json:{...x,folderCreated:true,folderPath:$('Configuration').first().json.folderRoot+'/'+String(x.code).replace(/[^A-Za-z0-9#_-]/g,'_')+'/'+x.format}}];")
    # A shared link outside the connected Dropbox has no path: permanent for this link (R5 M10), not 'temporary'.
    plan = f.nodes['Plan Listing']['parameters']['jsCode']
    f.nodes['Plan Listing']['parameters']['jsCode'] = plan.replace(
        "throw new Error('Dropbox link is unavailable')",
        "throw new Error('[config] The Dropbox link on the board is not in the connected Dropbox account or is no "
        "longer shared; replace the link or share it from that account')")
    assert '[config] The Dropbox link on the board' in f.nodes['Plan Listing']['parameters']['jsCode']
    f.nodes['Candidates']['parameters']['jsCode'] = f.nodes['Candidates']['parameters']['jsCode'].replace(
        "note: 'Needs review: folder \"' + folderName + '\" does not match item' } }];",
        "note: 'Folder \"' + folderName + '\" does not match this item', errorKind: 'config' } }];").replace(
        "note: 'No video found in folder' } }];", "note: 'No final video found in the project folder', errorKind: 'editor' } }];")
    assert "errorKind: 'config'" in f.nodes['Candidates']['parameters']['jsCode']
    assert "errorKind: 'editor'" in f.nodes['Candidates']['parameters']['jsCode']
    sel = f.code('Selected File', "const c=$json;const file=c.pick==null?null:c.files[c.pick];"
                                  "return [{json:{...$('Folder Context').item.json,file,ok:!!file,note:c.note||'No final video found in the project folder',errorKind:c.errorKind||'editor'}}];",
                  on_error='continueErrorOutput')
    found = f.reuse('Final File Found?')
    # R5 B3: owner-selected file -> exact file through its own link (metadata + version), never the folder listing.
    owner_q = f.cond('Owner Selected File?', '$json.ownerSelected===true&&!!$json.selectedUrl')
    o_meta = f.reuse('Folder Metadata', 'Owner File Link Metadata')
    f.nodes[o_meta]['parameters']['jsonBody'] = "={{ JSON.stringify({url:$('Item Context').item.json.selectedUrl}) }}"
    o_link = f.code('Owner File Link', r"""
const m=$json;
// A folder link, or a link to a file outside the connected Dropbox, cannot be bound to one file version: a
// permanent configuration problem for this link (never a reason to fall back to the folder).
if(m['.tag']!=='file')throw new Error('[config] The Dropbox link selected for this item is not a file; select the exact video file');
if(!m.path_lower)throw new Error('[config] The Dropbox file selected for this item is not in the connected Dropbox account; share it from that account or select another file');
return [{json:{path:m.path_lower,linkId:m.id||null}}];""", on_error='continueErrorOutput')
    o_ver = f.reuse('Folder Metadata', 'Owner File Version')
    f.nodes[o_ver]['parameters']['url'] = 'https://api.dropboxapi.com/2/files/get_metadata'
    f.nodes[o_ver]['parameters']['jsonBody'] = '={{ JSON.stringify({path:$json.path}) }}'
    o_sel = f.code('Owner Selected File', r"""
const ctx=$('Item Context').item.json,link=$('Owner File Link Metadata').item.json,m=$json;
if(m['.tag']&&m['.tag']!=='file')throw new Error('[config] The Dropbox link selected for this item is not a file; select the exact video file');
if(!m.id||!m.rev||!m.content_hash)throw new Error('[config] Dropbox did not report the exact version of the selected file; select it again');
if(link.id&&link.id!==m.id)throw new Error('[config] The selected Dropbox link no longer points to the same file; select the file again');
if(!/\.(mp4|mov|m4v)$/i.test(m.name||''))throw new Error('[config] The selected Dropbox file is not an MP4/MOV video ('+String(m.name||'').slice(0,80)+')');
return [{json:{file:{id:m.id,rev:m.rev,content_hash:m.content_hash,name:m.name,path:m.path_lower},url:ctx.selectedUrl,ok:true}}];""",
                   on_error='continueErrorOutput')
    f.link(cont, owner_q)
    f.link(owner_q, o_meta, 0)
    f.link(owner_q, has_folder, 1)
    f.chain(o_meta, o_link, o_ver, o_sel)
    # R5-LOW-06: no folder is created for an item without a valid Code.
    creatable = f.cond('Folder Creatable?', '$json.code_valid!==false')
    missing = f.code('Missing Source Identity', "return [{json:{ok:false,errorKind:'config',note:'" + MISSING_CODE + "'}}];")
    f.link(has_folder, 'Folder Context', 0)
    f.link(has_folder, creatable, 1)
    f.link(creatable, 'New Folder Context', 0)
    f.link(creatable, missing, 1)
    f.chain('New Folder Context', 'Create Project Folder', 'Check Folder Creation', 'Share New Folder',
            'Find Folder Link', 'New Folder Ready', 'Folder Context', 'Folder Metadata', 'Plan Listing',
            'Files Page Request', 'List Files', 'Collect Files', 'More Dropbox Files?')
    f.link('More Dropbox Files?', 'Files Page Request', 0)
    f.link('More Dropbox Files?', 'Candidates', 1)
    f.chain('Candidates', sel, found)
    f.link(found, 'Existing File Link', 0)
    f.chain('Existing File Link', 'File Share Context', 'File Link Exists?')
    f.link('File Link Exists?', 'Share Final File', 1)
    f.chain('Share Final File', 'New File Link')

    problem = f.code('Dropbox Problem', DROPBOX_CLASSIFY + r"""
const r=classify($json.error,$json);
return [{json:{error:r.text,errorKind:r.kind}}];""")
    for n in ('New Folder Context', 'Create Project Folder', 'Check Folder Creation', 'Share New Folder',
              'Find Folder Link', 'New Folder Ready', 'Folder Metadata', 'Plan Listing', 'List Files', 'Collect Files',
              'Candidates', 'Existing File Link', 'File Share Context', 'Share Final File', 'New File Link', sel,
              o_meta, o_link, o_ver, o_sel):
        f.link(n, problem, 1)
    step_in, step = f.helper('Preparation Step', '/v2/prep/step', r"""(()=>{
const ctx=$('Item Context').item.json,cfg=$('Configuration').first().json,run=$('Run Start').first().json;
let fc={};try{fc=$('Folder Context').item.json}catch{}
// The selection mode this run used (R5 B3): the handler refuses a result that is not the owner-selected file.
const base={requestId:cfg.runId+':'+ctx.itemId,itemId:ctx.itemId,runId:cfg.runId,fence:run.fence,expectedFormat:ctx.format,
folderUrl:fc.folderCreated?fc.folderUrl:null,
selection:{mode:ctx.selection_mode||'automatic_folder_selection',url:ctx.selected_url||null}};
if($json.file&&$json.ok!==false&&$json.url)return {...base,file:{id:$json.file.id,rev:$json.file.rev,content_hash:$json.file.content_hash,name:$json.file.name},url:$json.url};
if($json.ok===false)return {...base,error:$json.note,errorKind:$json.errorKind||'editor'};
return {...base,error:$json.error||'Dropbox selection failed',errorKind:$json.errorKind||'infra'};})()""",
                             on_error='continueErrorOutput')
    f.link(found, step_in, 1)
    f.link('File Link Exists?', step_in, 0)
    f.link('New File Link', step_in)
    f.link(problem, step_in)
    f.link(o_sel, step_in)
    f.link(missing, step_in)
    f.link(step, finished, 1)

    # Delivery of the verified file (the copy Instagram fetches). Every stage reports to the handler: a confirmed
    # upload is recorded with its Dropbox identity (R5 M17) and is not repeated when sharing fails; failures are
    # recorded with their stage and bounded (R5 M9).
    upload = f.cond('Upload Needed?', "$json.state==='completed'&&['upload','share','identify'].includes($json.next)")
    f.link(step, upload)
    f.link(upload, finished, 1)
    up_stage = f.cond('Upload Stage?', "$json.next==='upload'")
    id_stage = f.cond('Identify Stage?', "$json.next==='identify'")
    ens = f.reuse('Ensure Prepared Dropbox Folder')
    chk = f.code('Prepared Folder Checked', "if($json.error_summary&&!$json.error_summary.startsWith('path/conflict/folder'))throw new Error($json.error_summary);return [{json:$('Preparation Step').item.json}];",
                 on_error='continueErrorOutput')
    rd = f.reuse('Read Verified Video from n8n Disk')
    up = f.reuse('Upload Verified 1080 Video')
    f.nodes[up]['parameters']['headerParameters']['parameters'][0]['value'] = (
        "={{ JSON.stringify({path:$('Preparation Step').item.json.deliveryPath,mode:'overwrite',autorename:false,"
        "mute:true}) }}")
    ident = f.reuse('Folder Metadata', 'Identify Delivered Copy')
    f.nodes[ident]['parameters']['url'] = 'https://api.dropboxapi.com/2/files/get_metadata'
    f.nodes[ident]['parameters']['jsonBody'] = "={{ JSON.stringify({path:$('Preparation Step').item.json.deliveryPath}) }}"
    rec_in, rec = f.helper('Record Upload', '/v2/prep/delivered', r"""(()=>{const s=$('Preparation Step').item.json;
const stage=s.next==='identify'?'identified':'uploaded';
return {requestId:$('Configuration').first().json.runId+':'+$('Item Context').item.json.itemId+':'+stage,
itemId:$('Item Context').item.json.itemId,runId:$('Configuration').first().json.runId,fence:$('Run Start').first().json.fence,
mediaId:s.mediaId,stage,upload:{id:$json.id,rev:$json.rev,content_hash:$json.content_hash,path_lower:$json.path_lower,size:$json.size}};})()""",
                           on_error='continueErrorOutput')
    recorded = f.cond('Upload Recorded?', "$json.state==='completed'&&$json.next==='share'")
    target = f.code('Delivery Target', "const p=$json.deliveryPath||$('Preparation Step').item.json.deliveryPath;\n"
                                       "if(!p)throw new Error('Delivery path missing');return [{json:{path:p}}];",
                    on_error='continueErrorOutput')
    share = f.reuse('Share Verified Video')
    f.nodes[share]['parameters']['jsonBody'] = (
        "={{ JSON.stringify({path:$('Delivery Target').item.json.path,settings:{requested_visibility:'public'} }) }}")
    fshare = f.reuse('Find Verified Share')
    f.nodes[fshare]['parameters']['jsonBody'] = (
        "={{ JSON.stringify({path:$('Delivery Target').item.json.path,direct_only:true}) }}")
    link = f.code('Verified Delivery Link', r"""
const s=$('Share Verified Video').item.json;let link=s.url||s.error?.shared_link_already_exists?.metadata?.url||$json.links?.[0]?.url;
if(!link)throw new Error('Verified video delivery link missing');
link=link.replace(/[?&](dl|raw)=\d+/g,'').replace(/\?&/,'?');link+=(link.includes('?')?'&':'?')+'raw=1';
return [{json:{url:link}}];""", on_error='continueErrorOutput')
    del_in, dl = f.helper('Register Verified Delivery', '/v2/prep/delivered', r"""{requestId:$('Configuration').first().json.runId+':'+$('Item Context').item.json.itemId+':delivered',
itemId:$('Item Context').item.json.itemId,runId:$('Configuration').first().json.runId,fence:$('Run Start').first().json.fence,
mediaId:$('Preparation Step').item.json.mediaId,stage:'shared',url:$json.url}""", on_error='continueErrorOutput')
    d_problem = f.code('Delivery Problem', DROPBOX_CLASSIFY + r"""
const STAGES={'Ensure Prepared Dropbox Folder':'folder','Prepared Folder Checked':'folder',
'Read Verified Video from n8n Disk':'read','Upload Verified 1080 Video':'upload','Identify Delivered Copy':'identify',
'Record Upload':'record','Delivery Target':'share','Share Verified Video':'share','Find Verified Share':'share',
'Verified Delivery Link':'share','Register Verified Delivery':'register'};
let prev='';try{prev=$prevNode.name}catch(e){}
const r=classify($json.error,$json);
const local=/ENOENT|no such file/i.test(JSON.stringify($json.error||''));
return [{json:{error:r.text,errorKind:r.kind,stage:$json.stage||STAGES[prev]||(local?'read':'unknown')}}];""")
    fail_in, fail = f.helper('Record Delivery Failure', '/v2/prep/delivered', r"""(()=>{const s=$('Preparation Step').item.json;
return {requestId:$('Configuration').first().json.runId+':'+$('Item Context').item.json.itemId+':delivery-failed:'+$json.stage,
itemId:$('Item Context').item.json.itemId,runId:$('Configuration').first().json.runId,fence:$('Run Start').first().json.fence,
mediaId:s.mediaId,stage:$json.stage,error:$json.error,errorKind:$json.errorKind};})()""", fail=False)
    f.link(upload, up_stage, 0)
    f.link(up_stage, ens, 0)
    f.link(up_stage, id_stage, 1)
    f.link(id_stage, ident, 0)
    f.link(id_stage, target, 1)                        # 'share': the upload was confirmed earlier
    f.chain(ens, chk, rd, up, rec_in)
    f.link(ident, rec_in)
    f.link(rec, recorded)
    f.link(recorded, target, 0)
    f.link(recorded, finished, 1)
    f.chain(target, share, fshare, link, del_in)
    for n in (ens, chk, rd, up, ident, rec, target, share, fshare, link, dl):
        f.link(n, d_problem, 1)
    f.chain(d_problem, fail_in)
    f.link(fail, finished)
    f.link(dl, finished)

    # editor tasks: durable (item, issue) identity; only material changes
    e_take_in, e_take = f.helper('Editor Tasks — Take', '/v2/outbox/take', "{kinds:['editor'],worker:$('Configuration').first().json.runId,limit:10}")
    f.link(loop, e_take_in, 0)
    e_items = f.code('Editor Jobs', "const j=$json.jobs||[];return j.length?j.map(x=>({json:x})):[{json:{empty:true}}];")
    e_loop = f._add({'name': 'Each Editor Job', 'type': 'n8n-nodes-base.splitInBatches', 'typeVersion': 3,
                     'parameters': {'batchSize': 1, 'options': {}}})
    f.chain(e_take, e_items, e_loop)
    e_has = f.cond('Editor Job?', '!$json.empty&&!!$json.payload?.source_item_id')
    f.link(e_loop, e_has, 1)
    # Reuse an existing editor subitem (including ones created by v1 with the same
    # "Social <itemId> — " name) instead of creating a duplicate.
    e_find_q = f.code('Editor Task Lookup — Query', r"""
const p=$json.payload;
return [{json:{queryBody:JSON.stringify({query:'query($id:[ID!]){items(ids:$id){id subitems{id name updates(limit:25){text_body}}}}',variables:{id:[String(p.source_item_id)]}})}}];""")
    e_find = f.monday('Find Existing Editor Task', '$json.queryBody')
    e_mut = f.code('Editor Task Mutation', r"""
if($json.errors)throw new Error(JSON.stringify($json.errors));
const p=$('Each Editor Job').item.json.payload;
const subs=$json.data?.items?.[0]?.subitems||[];
const prefix='Social '+p.item_id+' — ';
const existing=p.task_id||(subs.find(s=>String(s.name||'').startsWith(prefix))||{}).id||null;
// Already told (v1 or v2): an update on this subitem names the same file version for the same kind of
// issue, or carries the same text. Record the task without posting again or touching its status.
const ups=((existing&&subs.find(s=>String(s.id)===String(existing)))||{}).updates||[];
const m=String(p.issue_key||'').match(/^(topaz|story_duration|media):(id:[^@\s]+@[0-9a-f]+)/);
const kind=m?m[1]:null,asset=m?m[2]:null;
const norm=t=>String(t||'').replace(/\s+/g,' ').trim();
const reason=t=>(String(t||'').match(/(?:reason|Issue):\s*([^\n]*)/)||[])[1]||'';
const sameKind=t=>kind==='topaz'?/topaz/i.test(reason(t)):kind==='story_duration'?(!/topaz/i.test(reason(t))&&/مدة الستوري|duration|ثانية|seconds/i.test(reason(t))):false;
const already=!!existing&&ups.some(u=>norm(u.text_body)===norm(p.body)||(asset&&String(u.text_body||'').includes(asset)&&sameKind(u.text_body)));
if(already)return [{json:{taskId:String(existing),already:true,data:{alreadyNotified:true}}}];
if(existing)return [{json:{taskId:String(existing),gql:{query:'mutation($b:ID!,$i:ID!,$v:JSON!){change_column_value(board_id:$b,item_id:$i,column_id:"status",value:$v){id}}',variables:{b:'5091137380',i:String(existing),v:JSON.stringify({label:'Working on it'})}}}}];
return [{json:{taskId:null,gql:{query:'mutation($p:ID!,$n:String!,$v:JSON!){create_subitem(parent_item_id:$p,item_name:$n,column_values:$v){id}}',variables:{p:String(p.source_item_id),n:p.task_name,v:JSON.stringify({status:{label:'Working on it'}})}}}}];""",
                   on_error='continueErrorOutput')
    e_told = f.cond('Editor Already Notified?', '$json.already===true')
    e_apply = f.monday('Apply Editor Task', 'JSON.stringify($json.gql)')
    e_body = f.code('Editor Task Update', r"""
if($json.errors)throw new Error(JSON.stringify($json.errors));
const p=$('Each Editor Job').item.json.payload;const id=$('Editor Task Mutation').item.json.taskId||$json.data?.create_subitem?.id;
if(!id)throw new Error('Editor task id missing');
return [{json:{taskId:String(id),gql:{query:'mutation($i:ID!,$b:String!){create_update(item_id:$i,body:$b){id}}',variables:{i:String(id),b:p.body}}}}];""",
                    on_error='continueErrorOutput')
    e_upd = f.monday('Post Editor Instructions', 'JSON.stringify($json.gql)')
    e_ack_in, e_ack = f.helper('Editor Task — Ack', '/v2/outbox/ack', r"""(()=>{
const ok=!$json.errors&&!$json.error&&!!$json.data;
let task=null;try{task=$('Editor Task Update').item.json.taskId}catch{}
if(!task){try{task=$('Editor Task Mutation').item.json.taskId||null}catch{}}
return {id:$('Each Editor Job').item.json.id,worker:$('Configuration').first().json.runId,ok,
error:ok?null:JSON.stringify($json.errors||$json.error||'editor task failed').slice(0,400),result:{task_id:task}};})()""", fail=False)
    f.link(e_has, e_find_q, 0)
    f.chain(e_find_q, e_find, e_mut, e_told)
    f.link(e_told, e_ack_in, 0)
    f.link(e_told, e_apply, 1)
    f.chain(e_apply, e_body, e_upd, e_ack_in)
    f.link(e_find, e_ack_in, 1)
    f.link(e_mut, e_ack_in, 1)
    f.link(e_apply, e_ack_in, 1)
    f.link(e_body, e_ack_in, 1)
    f.link(e_upd, e_ack_in, 1)
    e_next = f.code('Editor Job Finished', 'return [{json:{done:true}}];')
    f.link(e_ack, e_next)
    # A queued job without a projects item (queued before the core stopped creating them) is acknowledged,
    # otherwise it stays in flight and is taken again on every lease expiry.
    e_nosrc = f.cond('Job Without Source Item?', '!$json.empty&&!!$json.id')
    e_nosrc_in, e_nosrc_ack = f.helper('Editor Task — Skip', '/v2/outbox/ack', r"""(()=>({id:$json.id,
worker:$('Configuration').first().json.runId,ok:true,error:null,result:{task_id:null,skipped:'no projects item'}}))()""", fail=False)
    f.link(e_has, e_nosrc, 1)
    f.link(e_nosrc, e_nosrc_in, 0)
    f.link(e_nosrc, e_next, 1)
    f.link(e_nosrc_ack, e_next)
    f.link(e_next, e_loop)

    m_in, m = f.helper('Retain Published Files', '/v2/maintenance', '{}', fail=False)
    fin_in, fin = f.helper('Run Finish', '/v2/run/finish', "{kind:'wf1',runId:$('Configuration').first().json.runId}")
    f.link(e_loop, m_in, 0)
    f.link(m, fin_in)
    return f.export()


def main():
    DIST.mkdir(exist_ok=True)
    out = {'qI1N5VNgpRjnZAKH__1_Prepare_and_Schedule.v2.json': build_wf1(),
           'pUIshuf16zIYoYRz__2_Publish_When_Due.v2.json': build_wf2(),
           'WasetSocialScheduleGuard__3_Schedule_Supervisor.v2.json': build_wf3()}
    for name, w in out.items():
        (DIST / name).write_text(json.dumps(w, ensure_ascii=False, indent=1) + '\n')
        print(name, len(w['nodes']), 'nodes')


if __name__ == '__main__':
    main()
