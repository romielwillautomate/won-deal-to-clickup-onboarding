"""End-to-end test for the Won Deal to ClickUp Client Onboarding workflow against MOCK APIs.

No real ClickUp workspace, Postmark account or Slack workspace is used. The script:
  1. starts mock ClickUp, Postmark and Slack APIs on this machine (port 8786),
  2. imports temporary copies of the workflow into your n8n (API hosts pointed at the mocks, fake keys),
     one per setup: a space template, a folder template, an empty secret and a broken setting,
  3. plays 29 scenarios: forged and empty secrets, lost deals (generic and GoHighLevel), bad payloads and settings,
     the full happy path, duplicates, the GoHighLevel payload, a list that already exists, ClickUp and Postmark
     outages, a template call refused with a 404, a template that takes longer than the timeout, a template call
     that is lost, a half finished setup that is resumed, a half finished list that was deleted (answered with a 404,
     and with "Team not authorized"), ClickUp failing while the tasks are read, a revoked token on a resume, messy
     deal data, a start date on a Saturday, two copies of a webhook at the same moment, a deal id that is also a
     JavaScript property name, a Slack outage, a folder template and the error alert workflow,
  4. checks every request the workflow made and the deal records n8n keeps in the workflow's static data,
     prints PASS or FAIL per check, then deletes the temporary copies.

n8n in Docker reaches the mocks through Docker's host networking. On a busy machine a request can now and then
get lost on the way (n8n waits for its timeout, the mock never sees it). Retries usually cover it, but when it hits
the last attempt of a call the test expects to fail, a check fails. The script then prints a NOTE naming the call,
so that failure is not mistaken for a workflow bug. Run it again in that case.

The mocks answer with the response shapes from the official API docs, and they reject requests the real APIs
would reject: a missing or wrong token, a Bearer prefix on a ClickUp personal token, a missing Accept header
for Postmark, a list name that is already taken, assignees sent as a list on update, a custom field value of the
wrong type, an unknown dropdown option, and so on.

Requirements: Python 3.9+, a running n8n with the public API enabled. On Windows, `pip install tzdata` if
Python cannot find the America/New_York time zone.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
Usage: python tests/e2e_mock_test.py
"""
import json, os, re, sys, threading, time, urllib.error, urllib.request, uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8786
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
WORKFLOW = Path(__file__).resolve().parent.parent / 'workflow' / 'won-deal-to-clickup-onboarding.json'
SLUG = 'won-deal-to-clickup-onboarding'
TZ = ZoneInfo('America/New_York')

TOKEN = 'pk_test_mock_clickup_token'
WORKSPACE, SPACE, FOLDER, TEMPLATE = '9012345', '90150001', '90170001', 't-90160001'
SECRET = 'test-webhook-secret'
PM_TOKEN = 'test-postmark-server-token'
SENDER = 'onboarding@acmegrowth.example.com'
AM, SPEC, OWNER = 111111, 222222, 333333            # ClickUp user ids in the mock workspace
USERS = {AM: 'Riley Account Manager', SPEC: 'Sam Onboarding Specialist', OWNER: 'Template Owner'}
TASKS = [
    {'name': 'Send welcome packet', 'offset_days': -1, 'assignees': [AM], 'priority': 3},
    {'name': 'Kickoff call', 'offset_days': 0, 'assignees': [AM, SPEC], 'priority': 2, 'in_email': True,
     'email_label': 'Kickoff call with your account team'},
    {'name': 'Collect logins and brand assets', 'offset_days': 2, 'assignees': [SPEC], 'in_email': True,
     'email_label': 'Share your logins and brand assets'},
    {'name': 'Set up CRM pipeline', 'offset_days': 4, 'assignees': [SPEC], 'description': 'Build the pipeline stages agreed in the kickoff call.'},
    {'name': 'Week 1 check-in', 'offset_days': 5, 'assignees': [AM], 'in_email': True, 'email_label': 'Week 1 check-in call'},
]
ENV = {
    'WEBHOOK_SECRET': SECRET,
    'CLICKUP_API_TOKEN': TOKEN, 'CLICKUP_WORKSPACE_ID': WORKSPACE, 'CLICKUP_SPACE_ID': SPACE, 'CLICKUP_FOLDER_ID': '',
    'CLICKUP_TEMPLATE_ID': TEMPLATE, 'CLICKUP_TEMPLATE_TIMEOUT_MS': '3000', 'CLICKUP_TEMPLATE_WAIT_SECONDS': '2',
    'BUSINESS_TIMEZONE': 'America/New_York', 'BUSINESS_NAME': 'Acme Growth', 'KICKOFF_BOOKING_URL': 'https://calendar.example.com/acme/kickoff',
    'KICKOFF_TASKS_JSON': json.dumps(TASKS), 'DEFAULT_START_BUSINESS_DAYS': '5', 'DEFAULT_CURRENCY': 'USD',
    'POSTMARK_SERVER_TOKEN': PM_TOKEN, 'POSTMARK_FROM': f'Acme Growth <{SENDER}>', 'POSTMARK_REPLY_TO': 'success@acmegrowth.example.com',
    'POSTMARK_MESSAGE_STREAM': 'outbound', 'SLACK_WEBHOOK_URL': MOCK + '/slack/hook',
}
VARIANTS = {   # name: (env overrides, webhook path suffix)
    'main': ({}, ''),
    # The folder copy also gets KICKOFF_TASKS_JSON with the single quotes of the .env example still around it,
    # the way "docker run --env-file" passes it on.
    'folder': ({'CLICKUP_FOLDER_ID': FOLDER, 'CLICKUP_SPACE_ID': '', 'KICKOFF_TASKS_JSON': "'" + json.dumps(TASKS) + "'"}, '-folder'),
    'nosecret': ({'WEBHOOK_SECRET': ''}, '-nosecret'),
    'badconfig': ({'KICKOFF_TASKS_JSON': 'not json'}, '-badconfig'),
}
HOSTS = {'https://api.clickup.com': MOCK, 'https://api.postmarkapp.com': MOCK}

# ---------------------------------------------------------------- mock state
NSU = uuid.UUID('6c1d2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d')
TEMPLATE_TASKS = [('Kickoff call', [OWNER]), ('Collect logins and brand assets', []), ('Internal: review signed contract', [OWNER])]
TEMPLATE_FIELDS = [('Package', 'drop_down', ['Starter', 'Growth', 'Scale']), ('Start Date', 'date', None),
                   ('Deal Value', 'currency', None), ('Account Manager', 'users', None)]
PAGE_SIZE = 2   # ClickUp pages hold 100 tasks; 2 here, so the template list spans two pages and pagination is tested
LOCK = threading.Lock()
LOG = []        # one dict per request: svc, method, path, query, headers, body, status
STATE = {'lists': {}, 'tasks': {}, 'next_list': 901100000001, 'next_task': 1}
FAIL = {'auth': False, 'template_status': 0, 'template_delay': 0, 'task_fail': set(), 'task_429_once': set(),
        'postmark': '', 'slack': False, 'lists_delay': 0, 'lists_ok_left': None, 'tasks_get_status': 0, 'template_hang_once': False,
        'gone': ''}


def field_id(list_id, name):
    return str(uuid.uuid5(NSU, f'{list_id}:{name}'))


def option_id(list_id, field, option):
    return str(uuid.uuid5(NSU, f'{list_id}:{field}:{option}'))


def now_ms():
    return int(time.time() * 1000)


def new_list(name, parent_kind, parent_id, archived=False, with_template=True):
    lid = str(STATE['next_list'])
    STATE['next_list'] += 1
    fields = []
    if with_template:
        for fname, ftype, options in TEMPLATE_FIELDS:
            f = {'id': field_id(lid, fname), 'name': fname, 'type': ftype, 'type_config': {}, 'date_created': str(now_ms()),
                 'hide_from_guests': False, 'required': False}
            if ftype == 'drop_down':
                f['type_config'] = {'default': 0, 'placeholder': None,
                                    'options': [{'id': option_id(lid, fname, o), 'name': o, 'color': None, 'orderindex': i} for i, o in enumerate(options)]}
            if ftype == 'currency':
                f['type_config'] = {'precision': 2, 'currency_type': 'USD', 'default': None}
            fields.append(f)
    STATE['lists'][lid] = {'id': lid, 'name': name, 'kind': parent_kind, 'parent': parent_id, 'archived': archived,
                           'content': '', 'fields': fields, 'ready_at': 0}
    if with_template:
        for tname, assignees in TEMPLATE_TASKS:
            new_task(lid, {'name': tname, 'assignees': list(assignees)})
    return lid


def new_task(lid, body):
    tid = f"86mock{STATE['next_task']:04d}"
    STATE['next_task'] += 1
    STATE['tasks'][tid] = {'id': tid, 'list': lid, 'name': body['name'], 'description': body.get('description', ''),
                           'assignees': list(body.get('assignees', [])), 'priority': body.get('priority'),
                           'due_date': body.get('due_date'), 'due_date_time': body.get('due_date_time', False), 'fields': {},
                           'created': now_ms()}
    for cf in body.get('custom_fields', []):
        STATE['tasks'][tid]['fields'][cf['id']] = cf['value']
    return tid


def tasks_of(lid):
    return sorted([t for t in STATE['tasks'].values() if t['list'] == lid], key=lambda t: t['id'])


def lists_named(fragment):
    return [l for l in STATE['lists'].values() if fragment in l['name']]


def list_obj(l):
    folder = l['kind'] == 'folder'
    return {'id': l['id'], 'name': l['name'], 'orderindex': 0, 'content': l['content'], 'status': None, 'priority': None,
            'assignee': None, 'task_count': len(tasks_of(l['id'])), 'due_date': None, 'start_date': None,
            'folder': {'id': l['parent'] if folder else '90180009', 'name': 'Clients' if folder else 'hidden', 'hidden': not folder, 'access': True},
            'space': {'id': SPACE, 'name': 'Client Delivery', 'access': True},
            'archived': l['archived'], 'override_statuses': True, 'permission_level': 'create'}


def task_obj(t):
    l = STATE['lists'][t['list']]
    prio = {1: 'urgent', 2: 'high', 3: 'normal', 4: 'low'}
    return {'id': t['id'], 'custom_id': None, 'custom_item_id': 0, 'name': t['name'], 'text_content': t['description'],
            'description': t['description'], 'status': {'status': 'to do', 'color': '#87909e', 'type': 'open', 'orderindex': 0},
            'orderindex': '1.00000000000000000000000000000000', 'date_created': str(t['created']), 'date_updated': str(t['created']),
            'date_closed': None, 'archived': False,
            'creator': {'id': AM, 'username': USERS[AM], 'color': '#7b68ee', 'profilePicture': None},
            'assignees': [{'id': a, 'username': USERS.get(a, 'User'), 'color': '#7b68ee', 'initials': 'U', 'email': f'user{a}@acmegrowth.example.com',
                           'profilePicture': None} for a in t['assignees']],
            'watchers': [], 'checklists': [], 'tags': [], 'parent': None,
            'priority': {'id': str(t['priority']), 'priority': prio[t['priority']], 'color': '#ffcc00', 'orderindex': str(t['priority'])} if t['priority'] else None,
            'due_date': str(t['due_date']) if t['due_date'] else None, 'start_date': None, 'points': None, 'time_estimate': None,
            'custom_fields': [{**{k: v for k, v in f.items() if k != 'required'}, **({'value': t['fields'][f['id']]} if f['id'] in t['fields'] else {})}
                              for f in l['fields']],
            'list': {'id': l['id'], 'name': l['name'], 'access': True}, 'project': {'id': '90180009', 'name': 'hidden', 'hidden': True, 'access': True},
            'folder': {'id': '90180009', 'name': 'hidden', 'hidden': True, 'access': True}, 'space': {'id': SPACE},
            'url': f"https://app.clickup.com/t/{t['id']}"}


def field_problem(f, value, value_options):
    """What ClickUp would refuse in a custom field value. The error codes the mock returns are illustrative."""
    t = f['type']
    if t == 'drop_down':
        return None if isinstance(value, str) and value in [o['id'] for o in f['type_config']['options']] else 'Value must be an option index or uuid'
    if t == 'date':
        if not isinstance(value, int) or isinstance(value, bool) or value < 10 ** 11:
            return 'Value is not a valid date (Unix time in milliseconds)'
        if value_options is not None and not (isinstance(value_options, dict) and isinstance(value_options.get('time'), bool)):
            return 'value_options must be an object with a boolean time'
        return None
    if t in ('currency', 'number'):
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) else 'Value must be a number'
    if t == 'users':
        return None if isinstance(value, dict) and isinstance(value.get('add', []), list) else 'Value must be an object with add and rem'
    return None if isinstance(value, str) else 'Value must be a string'


# ---------------------------------------------------------------- mock HTTP server
class Mock(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _send(self, code, obj, entry, headers=None):
        entry['status'], entry['reply'] = code, obj
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json')
            self.send_header('Content-Length', str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass   # n8n gave up waiting (the timeout scenario)

    def _handle(self, method):
        parts = urlsplit(self.path)
        path, query = parts.path.rstrip('/'), parse_qs(parts.query)
        headers = {k.lower(): v for k, v in self.headers.items()}
        raw = self.rfile.read(int(headers.get('content-length') or 0))
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = '<not json>'
        svc = ('clickup' if path.startswith('/api/v2/') else 'postmark' if path == '/email' else 'alerts' if path == '/slack/errors'
               else 'slack' if path.startswith('/slack') else 'other')
        entry = {'svc': svc, 'method': method, 'path': path, 'query': query, 'headers': headers, 'body': body, 'status': None, 't': time.time()}
        LOG.append(entry)
        if svc == 'clickup':
            return self.clickup(method, path, query, headers, body, entry)
        if svc == 'postmark':
            return self.postmark(headers, body, entry)
        if svc in ('slack', 'alerts'):
            if FAIL['slack'] and svc == 'slack':
                return self._send(404, 'no_service', entry)
            if 'application/json' not in headers.get('content-type', '') or not isinstance(body, dict):
                return self._send(400, 'invalid_payload', entry)
            if not isinstance(body.get('text'), str) or not body['text'].strip():
                return self._send(400, 'no_text', entry)
            return self._send(200, 'ok', entry)
        return self._send(404, {'err': 'Route not found', 'ECODE': 'APP_001'}, entry)

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    # ClickUp API v2 -----------------------------------------------------------------------
    def clickup(self, method, path, query, headers, body, entry):
        auth = headers.get('authorization')
        if not auth:
            return self._send(400, {'err': 'Authorization header required', 'ECODE': 'OAUTH_017'}, entry)
        if FAIL['auth'] or auth != TOKEN:      # personal tokens are sent as is, without "Bearer"
            return self._send(401, {'err': 'Token invalid', 'ECODE': 'OAUTH_025'}, entry)
        if method in ('POST', 'PUT') and ('application/json' not in headers.get('content-type', '') or not isinstance(body, dict)):
            return self._send(400, {'err': 'Request body must be JSON', 'ECODE': 'INPUT_001'}, entry)

        m = re.match(r'^/api/v2/(space|folder)/(\w+)/list$', path)
        if m and method == 'GET':
            kind, pid = m.groups()
            if (kind, pid) not in (('space', SPACE), ('folder', FOLDER)):
                return self._send(404, {'err': 'Parent not found', 'ECODE': 'ACCESS_083'}, entry)
            archived = query.get('archived', ['false'])[0] == 'true'
            if FAIL['lists_ok_left'] is not None:          # answer a few lookups, then fail like an outage
                if FAIL['lists_ok_left'] <= 0:
                    return self._send(503, {'err': 'Service unavailable', 'ECODE': 'APP_503'}, entry)
                FAIL['lists_ok_left'] -= 1
            with LOCK:
                snapshot = [list_obj(l) for l in STATE['lists'].values() if l['kind'] == kind and l['parent'] == pid and l['archived'] == archived]
            if FAIL['lists_delay']:
                time.sleep(FAIL['lists_delay'])
            return self._send(200, {'lists': snapshot}, entry)

        m = re.match(r'^/api/v2/(space|folder)/(\w+)/list_template/([\w-]+)$', path)
        if m and method == 'POST':
            kind, pid, tpl = m.groups()
            if (kind, pid) not in (('space', SPACE), ('folder', FOLDER)):
                return self._send(404, {'err': 'Parent not found', 'ECODE': 'ACCESS_083'}, entry)
            if tpl != TEMPLATE:
                return self._send(404, {'err': 'Template not found', 'ECODE': 'TEMPLATE_001'}, entry)
            name, options = body.get('name'), body.get('options', {})
            if not isinstance(name, str) or not name.strip():
                return self._send(400, {'err': 'List name invalid', 'ECODE': 'INPUT_005'}, entry)
            if not isinstance(options, dict) or any(k != 'content' and not isinstance(v, bool) for k, v in options.items()):
                return self._send(400, {'err': 'Template options must be booleans', 'ECODE': 'INPUT_006'}, entry)
            if FAIL['template_status']:
                err = {404: ('Template not found', 'TEMPLATE_001')}.get(FAIL['template_status'], ('Internal server error', 'TEMPLATE_500'))
                return self._send(FAIL['template_status'], {'err': err[0], 'ECODE': err[1]}, entry)
            if FAIL['template_hang_once']:              # the call is lost: no list is made and no answer comes in time
                FAIL['template_hang_once'] = False
                time.sleep(4.5)
                return self._send(504, {'err': 'Gateway timeout', 'ECODE': 'APP_504'}, entry)
            with LOCK:
                if any(l['name'] == name and l['kind'] == kind and l['parent'] == pid for l in STATE['lists'].values()):
                    return self._send(400, {'err': 'List name taken', 'ECODE': 'SUBCAT_016'}, entry)
                lid = new_list(name, kind, pid, with_template=True)
                l = STATE['lists'][lid]
                l['content'] = options.get('content', '')
                if options.get('custom_fields') is not True:
                    l['fields'] = []
                if options.get('return_immediately', True) is not False:
                    l['ready_at'] = time.time() + 3      # ClickUp is still applying the template in the background
            if FAIL['template_delay']:
                time.sleep(FAIL['template_delay'])      # the list exists already, the answer is just late
            return self._send(200, {'id': lid, 'list': {**list_obj(l), 'statuses': [{'id': f'sc{lid}_todo', 'status': 'to do', 'orderindex': 0,
                                                                                         'color': '#87909e', 'type': 'open'}]}}, entry)

        m = re.match(r'^/api/v2/list/(\d+)/field$', path)
        if m and method == 'GET':
            l = STATE['lists'].get(m.group(1))
            if not l:
                return self.no_list(entry)
            return self._send(200, {'fields': l['fields']}, entry)

        m = re.match(r'^/api/v2/list/(\d+)/task$', path)
        if m:
            l = STATE['lists'].get(m.group(1))
            if not l:
                return self.no_list(entry)
            if method == 'GET':
                page = query.get('page', ['0'])[0]
                if not page.isdigit():
                    return self._send(400, {'err': 'Page must be a number', 'ECODE': 'INPUT_002'}, entry)
                if FAIL['tasks_get_status']:
                    return self._send(FAIL['tasks_get_status'], {'err': 'Service unavailable', 'ECODE': 'APP_503'}, entry)
                with LOCK:
                    visible = [] if time.time() < l['ready_at'] else tasks_of(l['id'])
                p = int(page)
                chunk = visible[p * PAGE_SIZE:(p + 1) * PAGE_SIZE]
                return self._send(200, {'tasks': [task_obj(t) for t in chunk], 'last_page': (p + 1) * PAGE_SIZE >= len(visible)}, entry)
            if method == 'POST':
                problem = self.task_problem(l, body, create=True)
                if problem:
                    return self._send(400, {'err': problem, 'ECODE': 'INPUT_010'}, entry)
                if body['name'] in FAIL['task_429_once']:
                    FAIL['task_429_once'].discard(body['name'])
                    return self._send(429, {'err': 'Rate limit reached', 'ECODE': 'APP_002'}, entry,
                                      {'X-RateLimit-Limit': '100', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(int(time.time()) + 60)})
                if body['name'] in FAIL['task_fail']:
                    return self._send(500, {'err': 'Internal server error', 'ECODE': 'ITEM_500'}, entry)
                with LOCK:
                    tid = new_task(l['id'], body)
                return self._send(200, task_obj(STATE['tasks'][tid]), entry)

        m = re.match(r'^/api/v2/task/(\w+)$', path)
        if m and method == 'PUT':
            t = STATE['tasks'].get(m.group(1))
            if not t:
                return self._send(404, {'err': 'Task not found, deleted', 'ECODE': 'ITEM_013'}, entry)
            problem = self.task_problem(STATE['lists'][t['list']], body, create=False)
            if problem:
                return self._send(400, {'err': problem, 'ECODE': 'INPUT_010'}, entry)
            with LOCK:
                if 'assignees' in body:
                    t['assignees'] = [a for a in t['assignees'] if a not in body['assignees'].get('rem', [])]
                    t['assignees'] += [a for a in body['assignees'].get('add', []) if a not in t['assignees']]
                for k in ('due_date', 'due_date_time', 'priority', 'name'):
                    if k in body:
                        t[k] = body[k]
            return self._send(200, task_obj(t), entry)

        m = re.match(r'^/api/v2/task/(\w+)/field/([\w-]+)$', path)
        if m and method == 'POST':
            t = STATE['tasks'].get(m.group(1))
            if not t:
                return self._send(404, {'err': 'Task not found, deleted', 'ECODE': 'ITEM_013'}, entry)
            f = next((f for f in STATE['lists'][t['list']]['fields'] if f['id'] == m.group(2)), None)
            if not f:
                return self._send(400, {'err': 'Custom field not found on this task', 'ECODE': 'FIELD_033'}, entry)
            if 'value' not in body:
                return self._send(400, {'err': 'Value is required', 'ECODE': 'FIELD_002'}, entry)
            problem = field_problem(f, body['value'], body.get('value_options'))
            if problem:
                return self._send(400, {'err': problem, 'ECODE': 'FIELD_011'}, entry)
            with LOCK:
                t['fields'][f['id']] = body['value']
            return self._send(200, {}, entry)
        return self._send(404, {'err': 'Route not found', 'ECODE': 'APP_001'}, entry)

    def no_list(self, entry):
        """A list id that does not exist (any more). ClickUp's docs list "Team not authorized" codes for ids a token
        cannot reach, so one scenario gets that answer instead of the 404."""
        if FAIL['gone'] == 'oauth':
            return self._send(401, {'err': 'Team not authorized', 'ECODE': 'OAUTH_027'}, entry)
        return self._send(404, {'err': 'List not found', 'ECODE': 'SUBCAT_013'}, entry)

    @staticmethod
    def task_problem(l, body, create):
        if create and (not isinstance(body.get('name'), str) or not body['name'].strip()):
            return 'Task name invalid'
        if 'assignees' in body:
            a = body['assignees']
            if create:
                if not isinstance(a, list) or not all(isinstance(x, int) and x in USERS for x in a):
                    return 'Assignees must be a list of workspace user ids'
            # The Update Task schema marks both "add" and "rem" as required.
            elif (not isinstance(a, dict) or not isinstance(a.get('add'), list) or not isinstance(a.get('rem'), list)
                  or not all(isinstance(x, int) and x in USERS for x in a['add'] + a['rem'])):
                return 'Assignees must be an object with add and rem lists of user ids'
        if 'due_date' in body and (not isinstance(body['due_date'], int) or isinstance(body['due_date'], bool) or body['due_date'] < 10 ** 11):
            return 'Due date must be Unix time in milliseconds'
        if 'due_date_time' in body and not isinstance(body['due_date_time'], bool):
            return 'due_date_time must be a boolean'
        if body.get('priority') is not None and body['priority'] not in (1, 2, 3, 4):
            return 'Priority must be 1, 2, 3 or 4'
        for cf in body.get('custom_fields', []) if create else []:
            f = next((f for f in l['fields'] if f['id'] == cf.get('id')), None)
            if not f:
                return 'Custom field not found on this list'
            problem = field_problem(f, cf.get('value'), cf.get('value_options'))
            if problem:
                return problem
        return None

    # Postmark ---------------------------------------------------------------------------
    def postmark(self, headers, body, entry):
        if 'application/json' not in headers.get('accept', '') or 'application/json' not in headers.get('content-type', ''):
            return self._send(415, {'ErrorCode': 0, 'Message': 'Accept and Content-Type must be application/json.'}, entry)
        if headers.get('x-postmark-server-token') != PM_TOKEN:
            return self._send(401, {'ErrorCode': 10, 'Message': 'Request does not contain a valid Server token.'}, entry)
        if not isinstance(body, dict):
            return self._send(422, {'ErrorCode': 300, 'Message': 'Invalid email request: the body is not JSON.'}, entry)
        sender = re.search(r'<([^>]+)>\s*$', body.get('From', '')) or re.match(r'^(\S+@\S+)$', body.get('From', ''))
        if not sender or sender.group(1).lower() != SENDER:
            return self._send(422, {'ErrorCode': 400, 'Message': f"The 'From' address you supplied ({body.get('From')}) is not a Sender Signature on your account."}, entry)
        to = body.get('To', '')
        if not isinstance(to, str) or not re.match(r'^[^\s@,]+@[^\s@,]+\.[a-z]{2,}$', to, re.I):
            return self._send(422, {'ErrorCode': 300, 'Message': f"Invalid 'To' address: '{to}'."}, entry)
        if not (body.get('HtmlBody') or body.get('TextBody')):
            return self._send(422, {'ErrorCode': 300, 'Message': 'Provide either email TextBody or HtmlBody or both.'}, entry)
        if body.get('MessageStream', 'outbound') != 'outbound':
            return self._send(422, {'ErrorCode': 1235, 'Message': f"The message stream '{body.get('MessageStream')}' was not found."}, entry)
        if not all(isinstance(v, str) for v in (body.get('Metadata') or {}).values()):
            return self._send(422, {'ErrorCode': 300, 'Message': 'Metadata values must be strings.'}, entry)
        if FAIL['postmark'] == 'inactive':
            return self._send(422, {'ErrorCode': 406, 'Message': 'You tried to send to recipient(s) that have been marked as inactive. '
                                    'Found inactive addresses: ' + to + '. Inactive recipients are ones that have generated a hard bounce, a spam complaint, or a manual suppression.'}, entry)
        return self._send(200, {'To': to, 'SubmittedAt': datetime.now(timezone.utc).isoformat(), 'MessageID': str(uuid.uuid4()),
                                'ErrorCode': 0, 'Message': 'OK'}, entry)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128     # the default backlog of 5 is small when n8n and the error workflow call at once


# ---------------------------------------------------------------- n8n helpers
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(variant):
    """The workflow with $env values replaced by test values and the API hosts pointed at the mocks."""
    overrides, suffix = VARIANTS[variant]
    env = {**ENV, **overrides}
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(env.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = f'{SLUG}-e2e{suffix}'
    return wf


class Flow:
    """One temporary workflow in n8n."""

    def __init__(self, variant, wid):
        self.variant, self.wid, self.sent = variant, wid, 0
        self.path = f'{SLUG}-e2e{VARIANTS[variant][1]}'

    def finished(self):
        return [e for e in api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250').get('data', []) if e.get('stoppedAt')]

    def wait(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            done = self.finished()
            if len(done) >= self.sent:
                return sorted(done, key=lambda e: int(e['id']))
            time.sleep(0.4)
        raise TimeoutError(f'{self.variant}: the workflow did not finish in time')

    def send(self, payload, secret=SECRET, header=True):
        data = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json'}
        if header and secret is not None:
            headers['X-Webhook-Secret'] = secret
        req = urllib.request.Request(f'{N8N}/webhook/{self.path}', method='POST', data=data, headers=headers)
        self.sent += 1
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def post(self, payload, **kw):
        """Send one webhook and wait for its run. Returns (status, body, run)."""
        status, body = self.send(payload, **kw)
        return status, body, self.wait()[-1]

    def memory(self):
        """The deal records in the workflow's static data, as n8n saved them after the last run."""
        sd = api('GET', f'/api/v1/workflows/{self.wid}').get('staticData') or {}
        if isinstance(sd, str):
            sd = json.loads(sd)
        return (sd.get('global') or {}).get('deals') or {}


def ended(ex):
    """The node a run ended on, its status and its error message."""
    data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
    result = data.get('resultData') or {}
    return result.get('lastNodeExecuted'), ex.get('status'), (result.get('error') or {}).get('message', '')


def network_timeouts(flows):
    """API calls that got no answer in time, although the mocks answer at once. Only the slow template scenario
    (deal D-3003) and the lost template call (deal D-3012) expect one each. Any other one means a request was lost
    between the n8n container and this machine, so a check that failed in that run failed because of the network,
    not the workflow."""
    found, expected = [], {'D-3003': 0, 'D-3012': 0}
    for f in flows.values():
        for e in f.finished():
            rd = ((api('GET', f"/api/v1/executions/{e['id']}?includeData=true").get('data') or {}).get('resultData') or {}).get('runData') or {}
            try:
                body = rd['Deal Webhook'][0]['data']['main'][0][0]['json'].get('body') or {}
            except (KeyError, IndexError, TypeError):
                body = {}
            deal_id = body.get('deal_id') or body.get('id') or '(no id)'
            for name, runs in rd.items():
                if not name.startswith(('ClickUp: ', 'Postmark: ', 'Slack: ')):
                    continue
                for r in runs:
                    outs = (r.get('data') or {}).get('main') or []
                    for item in (outs[1] or []) if len(outs) > 1 else []:
                        msg = str(((item.get('json') or {}).get('error') or {}).get('message', ''))
                        if 'timeout' not in msg:
                            continue
                        if deal_id in expected and name == 'ClickUp: Create From Template':
                            expected[deal_id] += 1
                        else:
                            found.append(f'a request was lost on the way to the mock, not refused by it (deal {deal_id}, {name}: {msg})')
    for deal_id, n in expected.items():
        if n == 0:   # the scan is broken if it misses a timeout the test causes on purpose
            found.append(f'the timeout scan found no timeout for deal {deal_id}, where the test causes one, so the scan cannot be trusted')
        elif n > 1:
            found.append(f'a request was lost on the way to the mock, not refused by it (deal {deal_id}, ClickUp: Create From Template '
                         f'timed out {n} times, the test causes it once)')
    return found


def since(mark, svc=None, method=None, path=None, status=None):
    """Requests since a mark. Alerts from the error workflow arrive a moment after a failed run, so they only
    count when asked for by name."""
    return [e for e in LOG[mark:] if (e['svc'] == svc if svc else e['svc'] != 'alerts') and (method is None or e['method'] == method)
            and (path is None or re.search(path, e['path'])) and (status is None or e['status'] == status)]


# ---------------------------------------------------------------- expected values, computed independently
def noon_ms(ymd):
    y, m, d = map(int, ymd.split('-'))
    return int(datetime(y, m, d, 12, tzinfo=TZ).timestamp() * 1000)


def add_bd(ymd, n):
    d = date.fromisoformat(ymd)
    step, left = (1 if n >= 0 else -1), abs(n)
    while left:
        d += timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return d.isoformat()


def on_business_day(ymd):
    """The date itself on a weekday, otherwise the Monday after."""
    return ymd if date.fromisoformat(ymd).weekday() < 5 else add_bd(ymd, 1)


def today_ny():
    return datetime.now(TZ).date().isoformat()


def short(ymd):
    d = date.fromisoformat(ymd)
    return f'{d:%a}, {d:%b} {d.day}, {d.year}'


def long(ymd):
    d = date.fromisoformat(ymd)
    return f'{d:%A}, {d:%B} {d.day}, {d.year}'


def deal(deal_id, client, contact, email, **extra):
    return {'deal_id': deal_id, 'status': 'won', 'client_name': client, 'contact_name': contact, 'contact_email': email,
            'package': 'Growth', 'deal_value': 4800, 'currency': 'USD', 'start_date': '2030-03-04', **extra}


class Results:
    def __init__(self):
        self.rows = []

    def check(self, scenario, label, ok, detail=''):
        self.rows.append((scenario, label, bool(ok), detail))


def created_list(fragment):
    found = lists_named(fragment)
    return found[0] if len(found) == 1 else None


# ---------------------------------------------------------------- the scenarios
def run(flows, R):
    main, folder, nosecret, badconfig = flows['main'], flows['folder'], flows['nosecret'], flows['badconfig']

    # 1. Forged and missing secrets
    for label, kw in (('wrong secret', {'secret': 'not-the-secret'}), ('no secret', {'secret': None})):
        mark = len(LOG)
        status, body, ex = main.post(deal('D-0001', 'Forged Client', 'Mallory Doe', 'mallory@example.com'), **kw)
        node, st, _ = ended(ex)
        R.check(f'Request with {label}', 'refused with 401, run stopped, no API called',
                status == 401 and node == 'Stop: Bad Secret' and st == 'error' and not since(mark), f'{status}, ended at {node}')

    # 2. Fail closed when WEBHOOK_SECRET is empty
    mark = len(LOG)
    results = [nosecret.post(deal('D-0002', 'Empty Secret Co', 'Pat Doe', 'pat@example.com'), secret=s) for s in ('', 'anything')]
    statuses, stops = [r[0] for r in results], [ended(r[2])[0] for r in results]
    R.check('WEBHOOK_SECRET left empty', 'every request refused (fail closed)',
            statuses == [401, 401] and stops == ['Stop: Bad Secret'] * 2 and not since(mark), f'statuses {statuses}, ended at {stops}')

    # 3. A deal that is not won
    mark = len(LOG)
    status, body, ex = main.post({**deal('D-0003', 'Lost Deal LLC', 'Lee Doe', 'lee@example.com'), 'status': 'lost'})
    node, _, _ = ended(ex)
    R.check('Deal marked lost', 'answered 200 "ignored", nothing created',
            status == 200 and body.get('status') == 'ignored' and node == 'Nothing To Do' and not since(mark), f'{status} {body.get("status")}, ended at {node}')

    # 3b. GoHighLevel sends the same trigger for lost and abandoned opportunities when the trigger has no status filter
    mark = len(LOG)
    answers = []
    for ghl_status in ('lost', 'abandoned'):
        status, body, ex = main.post({'contact_id': 'cnt0Lost0000000000001', 'full_name': 'Dana Lee', 'email': 'dana.lee@example.com',
                                      'company_name': 'Lee Landscaping', 'opportunity_name': 'Lee Landscaping: Starter', 'status': ghl_status,
                                      'lead_value': 900, 'pipleline_stage': 'Closed', 'pipeline_id': 'pip3Gt8Lw1Nc5Vb7Mq2D', 'id': 'opp0Lost000000000001',
                                      'customData': {'webhook_secret': SECRET}}, header=False)
        answers.append((status, body.get('status'), body.get('reason'), body.get('deal_id'), ended(ex)[0]))
    R.check('GoHighLevel opportunity not won', 'lost and abandoned answered 200 "ignored" with the status, nothing created',
            answers == [(200, 'ignored', f'status is "{s}", only won deals start onboarding', 'opp0Lost000000000001', 'Nothing To Do')
                        for s in ('lost', 'abandoned')] and not since(mark), f'{[a[:2] for a in answers]}')

    # 4. A won deal without an id
    mark = len(LOG)
    status, body, ex = main.post({k: v for k, v in deal('', 'No Id Dental', 'Kim Doe', 'kim@example.com').items() if k != 'deal_id'})
    node, _, err = ended(ex)
    R.check('Won deal without a deal id', 'refused with 400 and the reason, nothing created',
            status == 400 and body.get('details') == ['deal_id is missing'] and node == 'Stop: Rejected Deal' and not since(mark), f'{status}, ended at {node}')

    # 5. A broken setting
    mark = len(LOG)
    status, body, ex = badconfig.post(deal('D-0005', 'Config Test Co', 'Jo Doe', 'jo@example.com'))
    node, _, err = ended(ex)
    R.check('KICKOFF_TASKS_JSON is not valid JSON', 'answered 500 without details, run stopped with the reason',
            status == 500 and 'details' not in body and node == 'Stop: Rejected Deal' and 'KICKOFF_TASKS_JSON is not valid JSON' in err and not since(mark),
            f'{status}, ended at {node}')

    # 6. The happy path: generic JSON
    S = 'Happy path (generic JSON)'
    mark = len(LOG)
    harbor = deal('D-1042', 'Harbor Dental Studio', 'Maya Chen', 'maya.chen@example.com', contact_id='C-889')   # contact_id must not make it look like GoHighLevel
    status, body, ex = main.post(harbor)
    node, st, err = ended(ex)
    R.check(S, 'answered 202 accepted right away', status == 202 and body == {'status': 'accepted', 'deal_id': 'D-1042', 'action': 'create'}, f'{status} {body}')
    finds = since(mark, 'clickup', 'GET', r'/list$')
    R.check(S, 'looked for an existing list first, with the token as is (no Bearer)',
            len(finds) == 1 and finds[0]['path'] == f'/api/v2/space/{SPACE}/list' and finds[0]['query'] == {'archived': ['false']}
            and finds[0]['headers'].get('authorization') == TOKEN, f'{len(finds)} lookup(s)')
    tpl = since(mark, 'clickup', 'POST', r'/list_template/')
    opts = tpl[0]['body'].get('options', {}) if tpl else {}
    R.check(S, 'one list from the template, waiting for the template to finish',
            len(tpl) == 1 and tpl[0]['path'] == f'/api/v2/space/{SPACE}/list_template/{TEMPLATE}'
            and tpl[0]['body'].get('name') == 'Harbor Dental Studio Onboarding [D-1042]' and opts.get('return_immediately') is False
            and opts.get('custom_fields') is True and opts.get('old_due_date') is False and 'Package: Growth' in opts.get('content', '')
            and 'Value: $4,800.00' in opts.get('content', ''), json.dumps(tpl[0]['body']['name']) if tpl else 'no call')
    lst = created_list('[D-1042]')
    lid = lst['id'] if lst else ''
    pages = [e['query'].get('page', [''])[0] for e in since(mark, 'clickup', 'GET', rf'/list/{lid}/task$')]
    inc = all(e['query'].get('include_closed') == ['true'] for e in since(mark, 'clickup', 'GET', rf'/list/{lid}/task$'))
    R.check(S, 'read every page of the template tasks', pages == ['0', '1'] and inc, f'pages {pages}')
    tmpl = {t['name']: t['id'] for t in tasks_of(lid)} if lid else {}
    puts = sorted((e['path'].split('/')[-1], json.dumps(e['body'], sort_keys=True)) for e in since(mark, 'clickup', 'PUT'))
    want_puts = sorted((tid, json.dumps(b, sort_keys=True)) for tid, b in (
        (tmpl.get('Kickoff call'), {'due_date': noon_ms('2030-03-04'), 'due_date_time': False, 'assignees': {'add': [AM, SPEC], 'rem': []}, 'priority': 2}),
        (tmpl.get('Collect logins and brand assets'), {'due_date': noon_ms('2030-03-06'), 'due_date_time': False, 'assignees': {'add': [SPEC], 'rem': []}}),
    ))
    contract = tmpl.get('Internal: review signed contract')
    R.check(S, 'template tasks adjusted (due date, assignees), unrelated template task untouched',
            puts == want_puts and not [e for e in since(mark) if contract and contract in e['path']], f'{len(puts)} updates')
    fid = lambda n: field_id(lid, n)
    cf = [{'id': fid('Package'), 'value': option_id(lid, 'Package', 'Growth')},
          {'id': fid('Start Date'), 'value': noon_ms('2030-03-04'), 'value_options': {'time': False}},
          {'id': fid('Deal Value'), 'value': 4800}]
    creates = [e['body'] for e in since(mark, 'clickup', 'POST', r'/task$')]
    want_creates = [
        {'name': 'Send welcome packet', 'assignees': [AM], 'due_date': noon_ms('2030-03-01'), 'due_date_time': False, 'custom_fields': cf, 'priority': 3},
        {'name': 'Set up CRM pipeline', 'assignees': [SPEC], 'due_date': noon_ms('2030-03-08'), 'due_date_time': False, 'custom_fields': cf,
         'description': 'Build the pipeline stages agreed in the kickoff call.'},
        {'name': 'Week 1 check-in', 'assignees': [AM], 'due_date': noon_ms('2030-03-11'), 'due_date_time': False, 'custom_fields': cf},
    ]
    R.check(S, 'missing tasks created with assignees, business day due dates and custom fields',
            creates == want_creates and noon_ms('2030-03-11') == 1899475200000,
            f"{len(creates)} created; Week 1 check-in due at noon New York time on Mon Mar 11 2030, which is 16:00 UTC because the clocks changed on Mar 10")
    sets = sorted((e['path'].split('/')[-3], e['path'].split('/')[-1], json.dumps(e['body'], sort_keys=True)) for e in since(mark, 'clickup', 'POST', r'/field/'))
    want_sets = sorted((tmpl.get(t), c['id'], json.dumps({k: v for k, v in c.items() if k != 'id'}, sort_keys=True))
                       for t in ('Kickoff call', 'Collect logins and brand assets') for c in cf)
    R.check(S, 'custom fields set on the template tasks (package, start date, deal value)', sets == want_sets, f'{len(sets)} field values set')
    names = sorted(t['name'] for t in tasks_of(lid)) if lid else []
    R.check(S, 'list holds exactly 6 tasks, none twice', len(names) == 6 and len(set(names)) == 6 and len(lists_named('[D-1042]')) == 1, ', '.join(names))
    mail = since(mark, 'postmark')
    m = mail[0]['body'] if mail else {}
    text = m.get('TextBody', '')
    R.check(S, 'one welcome email through Postmark with the kickoff dates',
            # n8n's default Accept header also contains application/json, so only an exact match proves the workflow sets it
            len(mail) == 1 and mail[0]['headers'].get('x-postmark-server-token') == PM_TOKEN and mail[0]['headers'].get('accept') == 'application/json'
            and mail[0]['headers'].get('content-type') == 'application/json'
            and m.get('From') == f'Acme Growth <{SENDER}>' and m.get('To') == 'maya.chen@example.com'
            and m.get('Subject') == 'Welcome aboard, Maya! Your onboarding starts Mon, Mar 4, 2030'
            and 'Your onboarding starts on Monday, March 4, 2030.' in text
            and 'Monday, March 4, 2030: Kickoff call with your account team' in text
            and 'Wednesday, March 6, 2030: Share your logins and brand assets' in text
            and 'Monday, March 11, 2030: Week 1 check-in call' in text and 'Set up CRM pipeline' not in text
            and 'https://calendar.example.com/acme/kickoff' in text and m.get('MessageStream') == 'outbound'
            and m.get('Metadata') == {'deal_id': 'D-1042'} and m.get('ReplyTo') == 'success@acmegrowth.example.com' and m.get('Tag') == 'client-onboarding',
            m.get('Subject', 'no email'))
    slack = since(mark, 'slack')
    s = slack[0]['body']['text'] if slack else ''
    R.check(S, 'Slack gets the ClickUp link, the deal facts and every kickoff task',
            len(slack) == 1 and s.startswith(':white_check_mark: *Onboarding ready: Harbor Dental Studio*')
            and f'<https://app.clickup.com/{WORKSPACE}/v/li/{lid}|Harbor Dental Studio Onboarding [D-1042]>' in s
            and 'Deal D-1042 · Growth · $4,800.00 · starts Mon, Mar 4, 2030' in s and s.count('\n• ') == 5
            and '• Kickoff call, due Mon, Mar 4, 2030 (from the template)' in s and '• Week 1 check-in, due Mon, Mar 11, 2030' in s
            and 'Welcome email sent to maya.chen@example.com' in s and ':warning:' not in s, s.splitlines()[0] if s else 'no message')
    R.check(S, 'run ended as a success', node == 'Done' and st == 'success', f'ended at {node} ({st})')
    rec = main.memory().get('D-1042', {})
    R.check(S, 'deal memory: one small "done" record with the list and the Postmark message id',
            rec.get('status') == 'done' and rec.get('listId') == lid and rec.get('listUrl') == f'https://app.clickup.com/{WORKSPACE}/v/li/{lid}'
            and rec.get('listName') == 'Harbor Dental Studio Onboarding [D-1042]' and mail and rec.get('messageId') == (mail[0].get('reply') or {}).get('MessageID')
            and set(rec) == {'status', 'listId', 'listUrl', 'listName', 'clientName', 'createdAt', 'setupDoneAt', 'emailSentAt', 'messageId', 'updatedAt'},
            f"{rec.get('status')}, keys {', '.join(sorted(rec))}")

    # 7. The same webhook again
    mark = len(LOG)
    status, body, ex = main.post(harbor)
    node, _, _ = ended(ex)
    R.check('Same won deal sent again', 'answered "duplicate", no ClickUp, email or Slack call',
            status == 200 and body.get('status') == 'duplicate' and body.get('list_url') == f'https://app.clickup.com/{WORKSPACE}/v/li/{lid}'
            and not since(mark) and node == 'Nothing To Do', f'{status} {body.get("status")}, {len(since(mark))} API calls')

    # 8. GoHighLevel payload (secret as custom data, no header), no start date, and one 429 from ClickUp
    S = 'GoHighLevel opportunity webhook'
    opp = 'opp5Rk9Tz2Hx7Wc1Qf4J'
    ghl = {
        'contact_id': 'cnt8Vw2LpQ4rT6yZ1aB3', 'first_name': 'Elena', 'last_name': 'Ortiz', 'full_name': 'Elena Ortiz',
        'email': 'elena.ortiz@example.com', 'phone': '+15555550199', 'tags': 'customer', 'company_name': 'Ortiz & Daughters Roofing',
        'country': 'US', 'timezone': 'America/Chicago', 'date_created': '2030-01-15T16:20:00.000Z', 'contact_source': 'website',
        'contact_type': 'customer',
        'location': {'name': 'Acme Growth', 'id': 'loc7Hd2Ks9Qm4Xp1Rz6T', 'address': '100 Example Street', 'city': 'Austin', 'state': 'TX',
                     'country': 'US', 'postalCode': '78701', 'fullAddress': '100 Example Street, Austin TX 78701'},
        'opportunity_name': 'Ortiz & Daughters Roofing: Scale retainer', 'status': 'won', 'lead_value': 6200, 'opportunity_source': 'website',
        'source': 'website', 'pipleline_stage': 'Closed', 'pipeline_id': 'pip3Gt8Lw1Nc5Vb7Mq2D', 'id': opp, 'pipeline_name': 'Sales pipeline',
        'workflow': {'id': 'f3b1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d', 'name': 'Won deal to ClickUp'},
        'customData': {'webhook_secret': SECRET, 'package': 'Scale'},
    }
    FAIL['task_429_once'] = {'Week 1 check-in'}
    mark = len(LOG)
    status, body, ex = main.post(ghl, header=False)
    node, st, err = ended(ex)
    lst = created_list(f'[{opp}]')
    lid = lst['id'] if lst else ''
    mail = since(mark, 'postmark')
    creates = since(mark, 'clickup', 'POST', r'/task$')
    first = creates[0]['body'] if creates else {}
    R.check(S, 'read the GoHighLevel shape (secret in customData, company, contact, lead_value, package)',
            status == 202 and lst and lst['name'] == f'Ortiz & Daughters Roofing Onboarding [{opp}]'
            and {'id': field_id(lid, 'Deal Value'), 'value': 6200} in first.get('custom_fields', [])
            and {'id': field_id(lid, 'Package'), 'value': option_id(lid, 'Package', 'Scale')} in first.get('custom_fields', [])
            and len(mail) == 1 and mail[0]['body']['To'] == 'elena.ortiz@example.com' and mail[0]['body']['Subject'].startswith('Welcome aboard, Elena!'),
            lst['name'] if lst else f'{status}, no list')
    start = add_bd(today_ny(), 5)
    tmpl = {t['name']: t['id'] for t in tasks_of(lid)} if lid else {}
    kick = [e['body'] for e in since(mark, 'clickup', 'PUT') if e['path'].endswith(tmpl.get('Kickoff call', '?'))]
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    R.check(S, 'no start date: set to 5 business days from today and flagged in Slack',
            kick and kick[0]['due_date'] == noon_ms(start)
            and f':warning: No start date on the deal, so it was set to {short(start)} (5 business days from today)' in s, f'start {start}')
    week1 = [e for e in creates if e['body']['name'] == 'Week 1 check-in']
    R.check(S, 'a 429 from ClickUp is retried and no task is created twice',
            [e['status'] for e in week1] == [429, 200] and len(creates) == 4 and lid and len(tasks_of(lid)) == 6
            and len({t['name'] for t in tasks_of(lid)}) == 6, f"Week 1 check-in answers {[e['status'] for e in week1]}, {len(tasks_of(lid)) if lid else 0} tasks")
    html = mail[0]['body'].get('HtmlBody', '') if mail else ''
    R.check(S, 'the "&" in the company name is escaped for Slack and the email HTML',
            'Onboarding ready: Ortiz &amp; Daughters Roofing*' in s and 'Ortiz & Daughters' not in s
            and 'for Ortiz &amp; Daughters Roofing.' in html and 'for Ortiz & Daughters Roofing.' in mail[0]['body'].get('TextBody', '') and node == 'Done',
            s.splitlines()[0] if s else 'no message')

    # 9. ClickUp already has a list for this deal, and this workflow has no record of it
    S = 'List already in ClickUp'
    mark = len(LOG)
    bayview = deal('D-2001', 'Bayview Physio', 'Noah Park', 'noah.park@example.com')
    status, body, ex = main.post(bayview)
    node, _, _ = ended(ex)
    existing = created_list('[D-2001]')
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    R.check(S, 'nothing created, no email, Slack links the existing list',
            not since(mark, 'clickup', 'POST') and not since(mark, 'postmark') and len(lists_named('[D-2001]')) == 1
            and s.startswith(':information_source: *A ClickUp list already exists for Bayview Physio*')
            and f"<https://app.clickup.com/{WORKSPACE}/v/li/{existing['id']}|" in s and 'send the welcome email by hand' in s
            and main.memory().get('D-2001', {}).get('adopted') is True and node == 'Done', s.splitlines()[0] if s else 'no message')
    mark = len(LOG)
    status, body, ex = main.post(bayview)
    R.check(S, 'sent again: now stopped by the workflow memory, no API call',
            status == 200 and body.get('status') == 'duplicate' and not since(mark), f'{status} {body.get("status")}')

    # 10. ClickUp rejects the token
    S = 'ClickUp token rejected'
    northwind = deal('D-3001', 'Northwind Pet Clinic', 'Ava Brooks', 'ava.brooks@example.com')
    FAIL['auth'] = True
    mark = len(LOG)
    status, body, ex = main.post(northwind)
    node, st, err = ended(ex)
    FAIL['auth'] = False
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    R.check(S, 'retried 3 times, alert in Slack, no email, run failed for the error workflow',
            len(since(mark, 'clickup', 'GET')) == 3 and not since(mark, 'clickup', 'POST') and not since(mark, 'postmark')
            and s.startswith(':x: *ClickUp project NOT created: Northwind Pet Clinic*') and 'Error: 401 Token invalid (OAUTH_025)' in s
            and node == 'Stop: Onboarding Failed' and st == 'error' and 'D-3001' in err, f'ended at {node}')
    mark = len(LOG)
    status, body, ex = main.post(northwind)
    node, _, _ = ended(ex)
    R.check(S, 'token fixed, same webhook again: the project is created normally',
            body.get('action') == 'create' and len(since(mark, 'clickup', 'POST', r'/list_template/')) == 1 and len(since(mark, 'postmark')) == 1
            and node == 'Done', f'ended at {node}')

    # 11. The template call fails and no list was made
    S = 'ClickUp template call fails (500)'
    FAIL['template_status'] = 500
    mark = len(LOG)
    status, body, ex = main.post(deal('D-3002', 'Cedar Lane Bakery', 'Omar Haddad', 'omar.haddad@example.com'))
    node, st, _ = ended(ex)
    FAIL['template_status'] = 0
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    tries = [e['status'] for e in since(mark, 'clickup', 'POST', r'/list_template/')]
    R.check(S, 'tried a second time only after a check found no list, then alert, no email',
            tries == [500, 500] and len(since(mark, 'clickup', 'GET', r'/list$')) == 3
            and not since(mark, 'clickup', path=r'/task') and not since(mark, 'postmark') and 'NOT created: Cedar Lane Bakery' in s
            and 'Error: 500 Internal server error (TEMPLATE_500). The call was made twice, and after each try no list for this deal existed.' in s
            and not lists_named('[D-3002]') and node == 'Stop: Onboarding Failed', f'template answers {tries}, ended at {node}')

    # 11a. The template call is refused with a 4xx (a wrong template id): no second try, because nothing will change
    S = 'ClickUp refuses the template call (404)'
    FAIL['template_status'] = 404
    mark = len(LOG)
    status, body, ex = main.post(deal('D-3013', 'Juniper Lane Dental', 'Iris Moore', 'iris.moore@example.com'))
    node, st, err = ended(ex)
    FAIL['template_status'] = 0
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    tries = [e['status'] for e in since(mark, 'clickup', 'POST', r'/list_template/')]
    R.check(S, 'one try only, a check for the list, then alert, no email, run failed',
            tries == [404] and len(since(mark, 'clickup', 'GET', r'/list$')) == 2 and not since(mark, 'clickup', path=r'/task')
            and not since(mark, 'postmark') and s.startswith(':x: *ClickUp project NOT created: Juniper Lane Dental*')
            and 'Step: ClickUp: Create From Template' in s
            and 'Error: 404 Template not found (TEMPLATE_001). Checked again afterwards: no list for this deal exists.' in s
            and not lists_named('[D-3013]') and node == 'Stop: Onboarding Failed' and st == 'error' and 'D-3013' in err,
            f'template answers {tries}, ended at {node}')

    # 11b. The template call fails, and ClickUp is also down when the workflow checks for the list again
    S = 'ClickUp down during the re-check'
    FAIL['template_status'], FAIL['lists_ok_left'] = 500, 1
    mark = len(LOG)
    status, body, ex = main.post(deal('D-3010', 'Granite Peak Fitness', 'Leo Grant', 'leo.grant@example.com'))
    node, st, _ = ended(ex)
    FAIL['template_status'], FAIL['lists_ok_left'] = 0, None
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    R.check(S, 'both errors reported, no email, run failed',
            len(since(mark, 'clickup', 'POST', r'/list_template/')) == 1 and [e['status'] for e in since(mark, 'clickup', 'GET', r'/list$')] == [200, 503, 503, 503]
            and 'Step: ClickUp: Find List Again' in s and 'Creating the list failed (500 Internal server error (TEMPLATE_500))' in s
            and 'checking for the list failed: 503 Service unavailable (APP_503)' in s and not since(mark, 'postmark') and node == 'Stop: Onboarding Failed',
            f'ended at {node}')

    # 12. The template call takes longer than the timeout, but ClickUp builds the list anyway
    S = 'Template slower than the timeout'
    FAIL['template_delay'] = 5
    mark = len(LOG)
    status, body, ex = main.post(deal('D-3003', 'Summit Ridge Dental', 'Grace Liu', 'grace.liu@example.com'))
    node, st, _ = ended(ex)
    FAIL['template_delay'] = 0
    lst = created_list('[D-3003]')
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    R.check(S, 'one template call, the list is found again and the setup finishes on it',
            len(since(mark, 'clickup', 'POST', r'/list_template/')) == 1 and len(lists_named('[D-3003]')) == 1 and lst
            and len(tasks_of(lst['id'])) == 6 and len(since(mark, 'postmark')) == 1 and 'needed longer than the timeout' in s and node == 'Done',
            f'{len(lists_named("[D-3003]"))} list(s), ended at {node}')

    # 12b. The template call is lost: no answer in time and no list made. The re-check finds nothing, so one more try
    S = 'Template call lost'
    FAIL['template_hang_once'] = True
    mark = len(LOG)
    status, body, ex = main.post(deal('D-3012', 'Silver Pine Dentistry', 'Owen Reyes', 'owen.reyes@example.com'))
    node, st, _ = ended(ex)
    FAIL['template_hang_once'] = False
    lst = created_list('[D-3012]')
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    calls = [(e['method'], e['path'].split('/')[-1]) for e in since(mark, 'clickup')][:4]
    R.check(S, 'one more template call after the check found no list, one list, setup and email done',
            calls == [('GET', 'list'), ('POST', TEMPLATE), ('GET', 'list'), ('POST', TEMPLATE)]
            and len(lists_named('[D-3012]')) == 1 and lst and len(tasks_of(lst['id'])) == 6 and len(since(mark, 'postmark')) == 1
            and s.startswith(':white_check_mark: *Onboarding ready: Silver Pine Dentistry*') and 'so the workflow tried once more' in s
            and node == 'Done', f'first ClickUp calls {calls}, ended at {node}')

    # 13. One task cannot be created: partial setup, then the same webhook finishes it
    S = 'Partial setup, then resumed'
    maple = deal('D-3004', 'Maple Street Physio', 'Liam Novak', 'liam.novak@example.com')
    FAIL['task_fail'] = {'Set up CRM pipeline'}
    mark = len(LOG)
    status, body, ex = main.post(maple)
    node, st, err = ended(ex)
    FAIL['task_fail'] = set()
    lst = created_list('[D-3004]')
    lid = lst['id'] if lst else ''
    crm = [e for e in since(mark, 'clickup', 'POST', r'/task$') if e['body']['name'] == 'Set up CRM pipeline']
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    R.check(S, 'failed task retried 3 times, Slack lists what exists and what failed, no email',
            len(crm) == 3 and lid and len(tasks_of(lid)) == 5 and not since(mark, 'postmark')
            and s.startswith(':warning: *Onboarding only partly set up: Maple Street Physio*') and 'In the list now:' in s and s.count('\n• ') == 5
            and '• "Set up CRM pipeline": create task: 500 Internal server error (ITEM_500)' in s and 'NOT been sent' in s
            and node == 'Stop: Onboarding Failed' and 'setup is incomplete (1 change failed)' in err, f'ended at {node}')
    rec = main.memory().get('D-3004', {})
    R.check(S, 'deal memory after the failed run: "setup incomplete", the list id and the failed change, kept although the run failed',
            rec.get('status') == 'setup_incomplete' and rec.get('listId') == lid and 'setup' not in rec
            and rec.get('lastFailures') == [{'label': '"Set up CRM pipeline": create task', 'error': '500 Internal server error (ITEM_500)'}],
            f"{rec.get('status')}, {len(rec.get('lastFailures') or [])} failure(s) recorded")
    mark = len(LOG)
    status, body, ex = main.post(maple)
    node, st, _ = ended(ex)
    creates = [e['body']['name'] for e in since(mark, 'clickup', 'POST', r'/task$')]
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    names = [t['name'] for t in tasks_of(lid)] if lid else []
    R.check(S, 'sent again: same list, only the missing task created, then the email',
            body.get('action') == 'resume_setup' and not since(mark, 'clickup', 'POST', r'/list_template/') and not since(mark, 'clickup', 'GET', r'/list$')
            and creates == ['Set up CRM pipeline'] and len(since(mark, 'clickup', 'PUT')) == 4 and len(names) == 6 and len(set(names)) == 6
            and len(lists_named('[D-3004]')) == 1 and len(since(mark, 'postmark')) == 1 and 'Picked up where the last run stopped' in s
            and '• Send welcome packet, due Fri, Mar 1, 2030 (already in the list)' in s and '• Set up CRM pipeline, due Fri, Mar 8, 2030\n' in s
            and node == 'Done', f'created {creates}, {len(names)} tasks, ended at {node}')
    rec = main.memory().get('D-3004', {})
    R.check(S, 'deal memory after the resume: "done" on the same list, the old failure and the setup copy dropped',
            rec.get('status') == 'done' and rec.get('listId') == lid and 'lastFailures' not in rec and 'setup' not in rec and rec.get('messageId'),
            f"{rec.get('status')}, keys {', '.join(sorted(rec))}")

    # 13b. A half finished list is deleted in ClickUp before the webhook is sent again
    S = 'Half finished list deleted in ClickUp'
    willow = deal('D-3009', 'Willow Creek Spa', 'Nina Ford', 'nina.ford@example.com')
    FAIL['task_fail'] = {'Week 1 check-in'}
    main.post(willow)
    FAIL['task_fail'] = set()
    gone = created_list('[D-3009]')
    with LOCK:
        STATE['lists'].pop(gone['id'], None)
        for tid in [t['id'] for t in tasks_of(gone['id'])]:
            STATE['tasks'].pop(tid)
    mark = len(LOG)
    status, body, ex = main.post(willow)
    node, st, _ = ended(ex)
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    R.check(S, 'sent again: the missing list is reported and the deal is cleared',
            body.get('action') == 'resume_setup' and [e['status'] for e in since(mark, 'clickup')] == [404, 404, 404]
            and '• ClickUp: Get Custom Fields: 404 List not found (SUBCAT_013)' in s and 'so this deal was cleared' in s
            and not since(mark, 'postmark') and node == 'Stop: Onboarding Failed', f'ended at {node}')
    mark = len(LOG)
    status, body, ex = main.post(willow)
    node, st, _ = ended(ex)
    fresh = created_list('[D-3009]')
    R.check(S, 'sent a third time: a new list is built from scratch',
            body.get('action') == 'create' and fresh and fresh['id'] != gone['id'] and len(tasks_of(fresh['id'])) == 6
            and len(since(mark, 'postmark')) == 1 and node == 'Done', f'ended at {node}')

    # 13b2. The same, but ClickUp answers the deleted list id with "Team not authorized" instead of a 404
    S = 'Half finished list deleted, ClickUp says "Team not authorized"'
    hollow = deal('D-3014', 'Hollow Oak Pilates', 'Ada Price', 'ada.price@example.com')
    FAIL['task_fail'] = {'Week 1 check-in'}
    main.post(hollow)
    FAIL['task_fail'] = set()
    gone = created_list('[D-3014]')
    with LOCK:
        STATE['lists'].pop(gone['id'], None)
        for tid in [t['id'] for t in tasks_of(gone['id'])]:
            STATE['tasks'].pop(tid)
    FAIL['gone'] = 'oauth'
    mark = len(LOG)
    status, body, ex = main.post(hollow)
    node, st, _ = ended(ex)
    FAIL['gone'] = ''
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    R.check(S, 'sent again: the 401 OAUTH_027 is read as a list that is gone, the deal is cleared',
            body.get('action') == 'resume_setup' and [e['status'] for e in since(mark, 'clickup')] == [401, 401, 401]
            and '• ClickUp: Get Custom Fields: 401 Team not authorized (OAUTH_027)' in s and 'so this deal was cleared' in s
            and 'D-3014' not in main.memory() and not since(mark, 'postmark') and node == 'Stop: Onboarding Failed', f'ended at {node}')
    mark = len(LOG)
    status, body, ex = main.post(hollow)
    node, st, _ = ended(ex)
    fresh = created_list('[D-3014]')
    R.check(S, 'sent a third time: a new list is built from scratch',
            body.get('action') == 'create' and fresh and fresh['id'] != gone['id'] and len(tasks_of(fresh['id'])) == 6
            and len(since(mark, 'postmark')) == 1 and node == 'Done', f'ended at {node}')

    # 13c. The list is made, but ClickUp is down when the workflow reads its tasks (the paginated call)
    S = 'ClickUp down while reading the tasks'
    copper = deal('D-3011', 'Copper Kettle Cafe', 'Hana Sato', 'hana.sato@example.com')
    FAIL['tasks_get_status'] = 503
    mark = len(LOG)
    status, body, ex = main.post(copper)
    node, st, err = ended(ex)
    FAIL['tasks_get_status'] = 0
    lst = created_list('[D-3011]')
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    reads = [(e['status'], e['query'].get('page', [''])[0]) for e in since(mark, 'clickup', 'GET', r'/task$')]
    R.check(S, 'retried 3 times, Slack names the step and the 503, nothing changed in the list, no email',
            lst and reads == [(503, '0')] * 3 and not since(mark, 'clickup', 'PUT') and not since(mark, 'clickup', 'POST', r'/task$|/field/')
            and not since(mark, 'postmark') and s.startswith(':warning: *Onboarding only partly set up: Copper Kettle Cafe*')
            and '• ClickUp: Get Tasks: 503 Service unavailable (APP_503)' in s and 'In the list now:' not in s and 'NOT been sent' in s
            and node == 'Stop: Onboarding Failed' and st == 'error', f'reads {reads}, ended at {node}')
    # A wrong token on the resume must not be mistaken for a deleted list: the deal and its list are kept
    FAIL['auth'] = True
    mark = len(LOG)
    status, body, ex = main.post(copper)
    node, st, _ = ended(ex)
    FAIL['auth'] = False
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    rec = main.memory().get('D-3011', {})
    R.check(S, 'sent again with a revoked token: 401 OAUTH_025 reported, the deal and its list kept for the next try',
            body.get('action') == 'resume_setup' and [e['status'] for e in since(mark, 'clickup')] == [401, 401, 401]
            and '• ClickUp: Get Custom Fields: 401 Token invalid (OAUTH_025)' in s and 'cleared' not in s and 'NOT been sent' in s
            and rec.get('status') == 'setup_incomplete' and lst and rec.get('listId') == lst['id'] and node == 'Stop: Onboarding Failed',
            f"{rec.get('status')}, ended at {node}")
    mark = len(LOG)
    status, body, ex = main.post(copper)
    node, st, _ = ended(ex)
    R.check(S, 'sent again: the setup finishes on the same list, then the email',
            body.get('action') == 'resume_setup' and not since(mark, 'clickup', 'POST', r'/list_template/') and lst
            and len(lists_named('[D-3011]')) == 1 and len(tasks_of(lst['id'])) == 6 and len({t['name'] for t in tasks_of(lst['id'])}) == 6
            and len(since(mark, 'postmark')) == 1 and node == 'Done', f"{body.get('action')}, ended at {node}")

    # 14. Postmark refuses the email: keep the project, warn, then retry only the email
    S = 'Welcome email fails'
    bright = deal('D-3005', 'Brightside Accounting', 'Zoe Martin', 'zoe.martin@example.com')
    FAIL['postmark'] = 'inactive'
    mark = len(LOG)
    status, body, ex = main.post(bright)
    node, st, err = ended(ex)
    FAIL['postmark'] = ''
    lst = created_list('[D-3005]')
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    R.check(S, 'project kept, Slack warns with the Postmark reason, run failed',
            lst and len(tasks_of(lst['id'])) == 6 and len(since(mark, 'postmark')) == 3
            and s.startswith(':warning: *Onboarding ready, but the welcome email failed: Brightside Accounting*')
            and '422 You tried to send to recipient(s) that have been marked as inactive' in s and '(Postmark error 406)' in s
            and 'The ClickUp project is kept.' in s and node == 'Stop: Onboarding Failed', f'ended at {node}')
    rec = main.memory().get('D-3005', {})
    R.check(S, 'deal memory: "email pending" with the setup kept for the email retry and the Postmark reason',
            rec.get('status') == 'email_pending' and rec.get('listId') == (lst or {}).get('id')
            and [t['name'] for t in (rec.get('setup') or {}).get('tasks', [])] == [t['name'] for t in TASKS]
            and '(Postmark error 406)' in rec.get('lastEmailError', ''), f"{rec.get('status')}, {len((rec.get('setup') or {}).get('tasks', []))} tasks kept")
    mark = len(LOG)
    status, body, ex = main.post(bright)
    node, st, _ = ended(ex)
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    mail = since(mark, 'postmark')
    rec = main.memory().get('D-3005', {})
    R.check(S, 'sent again: only the email is retried, ClickUp is not touched, the deal is then "done"',
            body.get('action') == 'resume_email' and not since(mark, 'clickup') and len(mail) == 1
            and 'Monday, March 4, 2030: Kickoff call with your account team' in mail[0]['body'].get('TextBody', '')
            and s.startswith(':white_check_mark: *Welcome email sent: Brightside Accounting*') and node == 'Done'
            and rec.get('status') == 'done' and 'setup' not in rec and 'lastEmailError' not in rec, f"ended at {node}, memory {rec.get('status')}")

    # 15. Edge cases: no email, start date today, a package that is not a dropdown option, value as text
    S = 'Edge cases in the deal data'
    today = today_ny()
    riverbend = deal('D-3006', 'Riverbend Yoga', 'Ivy Chen', '', start_date=today, package='Enterprise', deal_value='$1,250.50')
    mark = len(LOG)
    status, body, ex = main.post(riverbend)
    node, st, _ = ended(ex)
    lst = created_list('[D-3006]')
    lid = lst['id'] if lst else ''
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    creates = {e['body']['name']: e['body'] for e in since(mark, 'clickup', 'POST', r'/task$')}
    R.check(S, 'no email address: project created, Slack says so, run still a success',
            lst and not since(mark, 'postmark') and 'No valid email address on the deal, so no welcome email was sent' in s and node == 'Done', f'ended at {node}')
    moved_to = on_business_day(today)    # today, or the Monday after when the test runs on a weekend
    R.check(S, 'a due date that would be in the past is moved to today (the next business day on a weekend)',
            creates.get('Send welcome packet', {}).get('due_date') == noon_ms(moved_to)
            and f'"Send welcome packet" would have been due {short(add_bd(today, -1))}, which has passed, so it is due '
                f"{'today' if moved_to == today else short(moved_to)}" in s,
            f"due {'today' if creates.get('Send welcome packet', {}).get('due_date') == noon_ms(today) else creates.get('Send welcome packet', {}).get('due_date')} ({today})")
    R.check(S, 'unknown package left empty with a warning; "$1,250.50" read as 1250.5',
            creates.get('Week 1 check-in', {}).get('custom_fields') == [
                {'id': field_id(lid, 'Start Date'), 'value': noon_ms(today), 'value_options': {'time': False}}, {'id': field_id(lid, 'Deal Value'), 'value': 1250.5}]
            and '"Enterprise" is not an option of the "Package" field, so it was left empty' in s,
            'fields sent: ' + ', '.join(f"{next((f['name'] for f in lst['fields'] if f['id'] == c['id']), '?')} = {c['value']}"
                                        for c in creates.get('Week 1 check-in', {}).get('custom_fields', [])) if lst else 'no list')
    mark = len(LOG)
    status, body, ex = main.post({**riverbend, 'contact_email': 'ivy.chen@example.com'})
    node, st, _ = ended(ex)
    mail = since(mark, 'postmark')
    R.check(S, 'email added in the CRM and sent again: only the welcome email goes out',
            body.get('action') == 'resume_email' and not since(mark, 'clickup') and len(mail) == 1 and mail[0]['body']['To'] == 'ivy.chen@example.com'
            and node == 'Done', f"{body.get('action')}, ended at {node}")

    # 15b. A start date on a Saturday: the tasks due on the start date itself move to Monday, the others count business days
    S = 'Start date on a Saturday'
    sat = '2030-03-02'
    mark = len(LOG)
    status, body, ex = main.post(deal('D-3015', 'Foxglove Florist', 'Rosa Diaz', 'rosa.diaz@example.com', start_date=sat))
    node, st, _ = ended(ex)
    lst = created_list('[D-3015]')
    tmpl = {t['name']: t['id'] for t in tasks_of(lst['id'])} if lst else {}
    due = {e['body']['name']: e['body']['due_date'] for e in since(mark, 'clickup', 'POST', r'/task$')}
    due.update({next((n for n, i in tmpl.items() if e['path'].endswith('/' + i)), '?'): e['body']['due_date'] for e in since(mark, 'clickup', 'PUT')})
    want = {t['name']: noon_ms(on_business_day(add_bd(sat, t['offset_days']))) for t in TASKS}
    s = (since(mark, 'slack') or [{'body': {'text': ''}}])[0]['body']['text']
    mail = since(mark, 'postmark')
    text = mail[0]['body'].get('TextBody', '') if mail else ''
    start_field = [e['body'] for e in since(mark, 'clickup', 'POST', r'/field/') if lst and e['path'].endswith(field_id(lst['id'], 'Start Date'))]
    R.check(S, 'every due date on a weekday, the Saturday kickoff moved to Monday and flagged, the start date itself kept',
            date.fromisoformat(sat).weekday() == 5 and due == want and want['Kickoff call'] == noon_ms('2030-03-04')
            and want['Collect logins and brand assets'] == noon_ms('2030-03-05')
            and all(datetime.fromtimestamp(v / 1000, TZ).weekday() < 5 for v in due.values())
            and s.count(':warning:') == 1
            and ':warning: "Kickoff call" would have been due Sat, Mar 2, 2030, which is on a weekend, so it is due Mon, Mar 4, 2030' in s
            and 'Your onboarding starts on Saturday, March 2, 2030.' in text and 'Monday, March 4, 2030: Kickoff call with your account team' in text
            and start_field and all(b['value'] == noon_ms(sat) for b in start_field) and node == 'Done',
            ', '.join(f"{n.split()[0]} {datetime.fromtimestamp(v / 1000, TZ):%a %b %d}" for n, v in due.items()))

    # 16. Two copies of the same webhook at the same moment
    S = 'Two copies at the same moment'
    lake = deal('D-3007', 'Lakeview Chiropractic', 'Ethan Cole', 'ethan.cole@example.com')
    FAIL['lists_delay'] = 4    # long enough that both lookups are in flight together, even when n8n starts one late
    mark = len(LOG)
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(main.send(lake))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    runs = main.wait()[-2:]
    FAIL['lists_delay'] = 0
    ends = sorted(ended(e)[0] for e in runs)
    tpl = since(mark, 'clickup', 'POST', r'/list_template/')
    lake_list = created_list('[D-3007]')
    R.check(S, 'one list, one email, one Slack message; the second run stops quietly',
            sorted(a[0] for a in answers) == [202, 202] and sorted(e['status'] for e in tpl) == [200, 400] and lake_list
            and len(tasks_of(lake_list['id'])) == 6 and len({t['name'] for t in tasks_of(lake_list['id'])}) == 6
            and len(since(mark, 'postmark')) == 1 and len(since(mark, 'slack')) == 1 and ends == ['Another Run Has It', 'Done'],
            f"template answers {sorted(e['status'] for e in tpl)}, runs ended at {ends}")
    mark = len(LOG)
    status, body, ex = main.post(lake)
    R.check(S, 'a third copy afterwards: the deal memory survived the race, answered "duplicate", no API call',
            status == 200 and body.get('status') == 'duplicate' and not since(mark) and ended(ex)[0] == 'Nothing To Do',
            f"{status} {body.get('status')}, {len(since(mark))} API calls")

    # 16b. A deal id that is also the name of a built-in JavaScript object property
    S = 'Deal id "constructor"'
    odd = deal('constructor', 'Northgate Print Shop', 'Paul Kent', 'paul.kent@example.com')
    mark = len(LOG)
    status, body, ex = main.post(odd)
    node, _, _ = ended(ex)
    R.check(S, 'treated as a new deal, not as a record that already exists',
            status == 202 and body.get('action') == 'create' and len(lists_named('[constructor]')) == 1
            and len(since(mark, 'postmark')) == 1 and node == 'Done', f"{status} {body.get('status')} {body.get('action', '')}, ended at {node}")
    mark = len(LOG)
    status, body, ex = main.post(odd)
    R.check(S, 'sent again: answered "duplicate", no API call',
            status == 200 and body.get('status') == 'duplicate' and not since(mark), f"{status} {body.get('status')}")

    # 17. Slack is down
    S = 'Slack webhook is gone'
    FAIL['slack'] = True
    mark = len(LOG)
    status, body, ex = main.post(deal('D-3008', 'Oakwood Vet Care', 'Mia Rossi', 'mia.rossi@example.com'))
    node, st, err = ended(ex)
    FAIL['slack'] = False
    R.check(S, 'project and email done, Slack retried 3 times, run failed with the message in the error',
            created_list('[D-3008]') and len(since(mark, 'postmark')) == 1 and len(since(mark, 'slack')) == 3 and node == 'Stop: Slack Failed'
            and 'Onboarding ready: Oakwood Vet Care' in err, f'ended at {node}')

    # 18. A folder template instead of a space
    S = 'Folder instead of space'
    mark = len(LOG)
    status, body, ex = folder.post(deal('D-4001', 'Pinecrest Law Office', 'Ruth Adams', 'ruth.adams@example.com'))
    node, _, _ = ended(ex)
    lst = created_list('[D-4001]')
    R.check(S, 'lists read and created in the folder, setup completes (KICKOFF_TASKS_JSON still in single quotes)',
            [e['path'] for e in since(mark, 'clickup', 'GET', r'/list$')] == [f'/api/v2/folder/{FOLDER}/list']
            and [e['path'] for e in since(mark, 'clickup', 'POST', r'/list_template/')] == [f'/api/v2/folder/{FOLDER}/list_template/{TEMPLATE}']
            and lst and lst['kind'] == 'folder' and len(tasks_of(lst['id'])) == 6 and len(since(mark, 'postmark')) == 1
            and node == 'Done', f'ended at {node}')

    # 19. Every failed run reaches the error alert workflow
    S = 'Error alert workflow'
    failed = []
    for f in flows.values():
        for e in f.finished():
            if e.get('status') == 'error':
                failed.append(ended(e)[0])
    end = time.time() + 30
    while time.time() < end and len(since(0, 'alerts')) < len(failed):
        time.sleep(0.5)
    time.sleep(2)   # make sure no extra alert follows
    alerts = [e['body']['text'] for e in since(0, 'alerts')]
    stops = {n: failed.count(n) for n in sorted(set(failed))}
    R.check(S, 'one Slack alert per failed run, naming the Stop node and the reason',
            len(alerts) == len(failed) and all(sum(f'failed at *{n}*' in a for a in alerts) == c for n, c in stops.items())
            and any('Onboarding for Maple Street Physio (deal D-3004): the setup is incomplete (1 change failed)' in a for a in alerts),
            f'{len(alerts)} alerts for {len(failed)} failed runs: ' + ', '.join(f'{n} x{c}' for n, c in stops.items()))


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    # Lists that exist before the test: one for deal D-2001, and one whose id only looks similar to D-1042.
    new_list('Bayview Physio Onboarding [D-2001]', 'space', SPACE)
    new_list('Old Client Onboarding [D-104]', 'space', SPACE)
    server = MockServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    flows, R, err_id, lost = {}, Results(), None, []
    try:
        # The error alert workflow first, so every temporary copy can name it as its Error Workflow.
        alert = json.loads((WORKFLOW.parent / 'error-alert-slack.json').read_text(encoding='utf-8'))
        for node in alert['nodes']:
            node['parameters'] = json.loads(json.dumps(node.get('parameters', {})).replace('$env.SLACK_WEBHOOK_URL', f"'{MOCK}/slack/errors'"))
        err_id = api('POST', '/api/v1/workflows', {'name': f'TEST {SLUG} (temporary)', 'nodes': alert['nodes'],
                                                   'connections': alert['connections'], 'settings': alert.get('settings', {})})['id']
        api('POST', f'/api/v1/workflows/{err_id}/activate')    # n8n 2.x only runs an error workflow that is active
        for variant in VARIANTS:
            wf = test_copy(variant)
            wid = api('POST', '/api/v1/workflows', {'name': f'TEST {SLUG} (temporary)', 'nodes': wf['nodes'], 'connections': wf['connections'],
                                                    'settings': {**wf.get('settings', {}), 'errorWorkflow': err_id}})['id']
            flows[variant] = Flow(variant, wid)
            api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(2)
        run(flows, R)
        lost = network_timeouts(flows)
    finally:
        if err_id:
            try:
                api('POST', f'/api/v1/workflows/{err_id}/deactivate')
            except Exception:
                pass
            finally:
                api('DELETE', f'/api/v1/workflows/{err_id}')
        for f in flows.values():
            try:
                api('POST', f'/api/v1/workflows/{f.wid}/deactivate')
            except Exception:
                pass
            finally:
                api('DELETE', f'/api/v1/workflows/{f.wid}')
        server.shutdown()

    passed = sum(ok for _, _, ok, _ in R.rows)
    last = None
    for scenario, label, ok, detail in R.rows:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(R.rows)} checks passed in {len(dict.fromkeys(r[0] for r in R.rows))} scenarios, {len(LOG)} requests to the mocks')
    for note in lost:
        print(f'NOTE: {note}')
    # A lost request that a retry covered leaves no error in n8n, only a silence of about 17 seconds in the mock log
    # (the 15 second timeout plus the 2 second wait before the retry). No scenario waits that long on purpose.
    for a, b in zip(LOG, LOG[1:]):
        if 14 <= b['t'] - a['t'] <= 20:
            print(f"NOTE: {b['t'] - a['t']:.1f}s without any request before {b['method']} {b['path']}: "
                  'probably a request lost on the way and sent again by the retry')
    if os.environ.get('E2E_DUMP'):
        Path(os.environ['E2E_DUMP']).write_text(json.dumps(LOG, indent=1, default=str), encoding='utf-8')
    sys.exit(0 if passed == len(R.rows) else 1)


if __name__ == '__main__':
    main()
