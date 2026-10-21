# Test report

**Date:** 2026-10-21
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 59 of 59 checks pass across 29 scenarios (420 requests to the mocks)

## How it was tested

No real ClickUp workspace, Postmark account, CRM or Slack workspace was used. The test script:

1. starts mock ClickUp, Postmark and Slack APIs on the test machine,
2. imports four temporary copies of the workflow into n8n, with every API host pointed at the mocks and fake keys: one that builds lists in a space, one that builds them in a folder (with `KICKOFF_TASKS_JSON` still wrapped in the single quotes of the `.env` example), one with an empty `WEBHOOK_SECRET` and one with a broken `KICKOFF_TASKS_JSON`,
3. imports the error alert workflow pointed at a mock Slack channel, activates it (n8n 2.x only runs an active error workflow) and sets it as the Error Workflow of every copy,
4. sends webhooks the way a CRM does: the generic JSON with the `X-Webhook-Secret` header, and the payload of GoHighLevel's Webhook action with the secret in `customData`,
5. waits for each n8n run to finish, then checks every request the workflow made (method, path, query, headers, body) and the node each run ended on,
6. reads the deal records the workflow keeps in its static data through the n8n API, after the happy path, after a failed and a resumed setup and after a failed and a retried email,
7. deletes the temporary copies.

Due dates are checked against values the test works out on its own with Python's time zone database, not against the workflow's own math.

### What the mocks refuse

The mocks answer with the response shapes from the ClickUp API v2 and Postmark docs, and they refuse what the real APIs refuse, so a passing check means the request was right.

| API | Refused |
|---|---|
| ClickUp | No `Authorization` header (400, OAUTH_017). A wrong token, or a personal token with "Bearer" in front (401, OAUTH_025). A body that is not JSON. An unknown space, folder or template (404). A list name already taken in the same place (400). Template options that are not booleans. Assignees that are not workspace members, or not `{"add": [...], "rem": [...]}` with both lists on an update. Due dates that are not Unix time in milliseconds. A priority outside 1 to 4. A custom field that is not on the list. A dropdown value that is not an option uuid, a date that is not milliseconds, a money value that is not a number |
| Postmark | No `Accept` or `Content-Type` of `application/json` (415). A wrong server token (401, ErrorCode 10). A `From` that is not a confirmed sender (422, 400). An invalid `To` (422, 300). No body (422, 300). An unknown message stream (422, 1235). Metadata values that are not strings |
| Slack | A body that is not JSON, or no `text` (400) |

The mock ClickUp also behaves like the real one where it matters: task pages hold 2 tasks instead of 100, so reading every page is tested; a template call without `custom_fields: true` gives a list without its custom fields; a template call without `return_immediately: false` gives a list whose template tasks only appear 3 seconds later, like a template that is still being applied.

Failures the test switches on: a rejected token (on a new deal and on a resume), a 500 from the template call, a 404 from the template call (a wrong template id), a template answer that comes after the timeout (the list exists, the answer is late), a template call that gets no answer and makes no list (a lost request), list lookups that fail after the first one, one task that always fails with 500, one 429 with rate limit headers, a 503 on reading tasks, a half finished list deleted in ClickUp (answered with a 404, and with "Team not authorized", 401 OAUTH_027, which ClickUp's docs list for ids a token cannot reach), a slow list lookup so two runs overlap, an inactive Postmark recipient (422, ErrorCode 406) and a removed Slack webhook (404 `no_service`).

## Results (final run)

```
Request with wrong secret
  PASS  refused with 401, run stopped, no API called  (401, ended at Stop: Bad Secret)

Request with no secret
  PASS  refused with 401, run stopped, no API called  (401, ended at Stop: Bad Secret)

WEBHOOK_SECRET left empty
  PASS  every request refused (fail closed)  (statuses [401, 401], ended at ['Stop: Bad Secret', 'Stop: Bad Secret'])

Deal marked lost
  PASS  answered 200 "ignored", nothing created  (200 ignored, ended at Nothing To Do)

GoHighLevel opportunity not won
  PASS  lost and abandoned answered 200 "ignored" with the status, nothing created  ([(200, 'ignored'), (200, 'ignored')])

Won deal without a deal id
  PASS  refused with 400 and the reason, nothing created  (400, ended at Stop: Rejected Deal)

KICKOFF_TASKS_JSON is not valid JSON
  PASS  answered 500 without details, run stopped with the reason  (500, ended at Stop: Rejected Deal)

Happy path (generic JSON)
  PASS  answered 202 accepted right away  (202 {'status': 'accepted', 'deal_id': 'D-1042', 'action': 'create'})
  PASS  looked for an existing list first, with the token as is (no Bearer)  (1 lookup(s))
  PASS  one list from the template, waiting for the template to finish  ("Harbor Dental Studio Onboarding [D-1042]")
  PASS  read every page of the template tasks  (pages ['0', '1'])
  PASS  template tasks adjusted (due date, assignees), unrelated template task untouched  (2 updates)
  PASS  missing tasks created with assignees, business day due dates and custom fields  (3 created; Week 1 check-in due at noon New York time on Mon Mar 11 2030, which is 16:00 UTC because the clocks changed on Mar 10)
  PASS  custom fields set on the template tasks (package, start date, deal value)  (6 field values set)
  PASS  list holds exactly 6 tasks, none twice  (Collect logins and brand assets, Internal: review signed contract, Kickoff call, Send welcome packet, Set up CRM pipeline, Week 1 check-in)
  PASS  one welcome email through Postmark with the kickoff dates  (Welcome aboard, Maya! Your onboarding starts Mon, Mar 4, 2030)
  PASS  Slack gets the ClickUp link, the deal facts and every kickoff task  (:white_check_mark: *Onboarding ready: Harbor Dental Studio*)
  PASS  run ended as a success  (ended at Done (success))
  PASS  deal memory: one small "done" record with the list and the Postmark message id  (done, keys clientName, createdAt, emailSentAt, listId, listName, listUrl, messageId, setupDoneAt, status, updatedAt)

Same won deal sent again
  PASS  answered "duplicate", no ClickUp, email or Slack call  (200 duplicate, 0 API calls)

GoHighLevel opportunity webhook
  PASS  read the GoHighLevel shape (secret in customData, company, contact, lead_value, package)  (Ortiz & Daughters Roofing Onboarding [opp5Rk9Tz2Hx7Wc1Qf4J])
  PASS  no start date: set to 5 business days from today and flagged in Slack  (start 2026-10-27)
  PASS  a 429 from ClickUp is retried and no task is created twice  (Week 1 check-in answers [429, 200], 6 tasks)
  PASS  the "&" in the company name is escaped for Slack and the email HTML  (:white_check_mark: *Onboarding ready: Ortiz &amp; Daughters Roofing*)

List already in ClickUp
  PASS  nothing created, no email, Slack links the existing list  (:information_source: *A ClickUp list already exists for Bayview Physio*)
  PASS  sent again: now stopped by the workflow memory, no API call  (200 duplicate)

ClickUp token rejected
  PASS  retried 3 times, alert in Slack, no email, run failed for the error workflow  (ended at Stop: Onboarding Failed)
  PASS  token fixed, same webhook again: the project is created normally  (ended at Done)

ClickUp template call fails (500)
  PASS  tried a second time only after a check found no list, then alert, no email  (template answers [500, 500], ended at Stop: Onboarding Failed)

ClickUp refuses the template call (404)
  PASS  one try only, a check for the list, then alert, no email, run failed  (template answers [404], ended at Stop: Onboarding Failed)

ClickUp down during the re-check
  PASS  both errors reported, no email, run failed  (ended at Stop: Onboarding Failed)

Template slower than the timeout
  PASS  one template call, the list is found again and the setup finishes on it  (1 list(s), ended at Done)

Template call lost
  PASS  one more template call after the check found no list, one list, setup and email done  (first ClickUp calls [('GET', 'list'), ('POST', 't-90160001'), ('GET', 'list'), ('POST', 't-90160001')], ended at Done)

Partial setup, then resumed
  PASS  failed task retried 3 times, Slack lists what exists and what failed, no email  (ended at Stop: Onboarding Failed)
  PASS  deal memory after the failed run: "setup incomplete", the list id and the failed change, kept although the run failed  (setup_incomplete, 1 failure(s) recorded)
  PASS  sent again: same list, only the missing task created, then the email  (created ['Set up CRM pipeline'], 6 tasks, ended at Done)
  PASS  deal memory after the resume: "done" on the same list, the old failure and the setup copy dropped  (done, keys clientName, createdAt, emailSentAt, listId, listName, listUrl, messageId, setupDoneAt, status, updatedAt)

Half finished list deleted in ClickUp
  PASS  sent again: the missing list is reported and the deal is cleared  (ended at Stop: Onboarding Failed)
  PASS  sent a third time: a new list is built from scratch  (ended at Done)

Half finished list deleted, ClickUp says "Team not authorized"
  PASS  sent again: the 401 OAUTH_027 is read as a list that is gone, the deal is cleared  (ended at Stop: Onboarding Failed)
  PASS  sent a third time: a new list is built from scratch  (ended at Done)

ClickUp down while reading the tasks
  PASS  retried 3 times, Slack names the step and the 503, nothing changed in the list, no email  (reads [(503, '0'), (503, '0'), (503, '0')], ended at Stop: Onboarding Failed)
  PASS  sent again with a revoked token: 401 OAUTH_025 reported, the deal and its list kept for the next try  (setup_incomplete, ended at Stop: Onboarding Failed)
  PASS  sent again: the setup finishes on the same list, then the email  (resume_setup, ended at Done)

Welcome email fails
  PASS  project kept, Slack warns with the Postmark reason, run failed  (ended at Stop: Onboarding Failed)
  PASS  deal memory: "email pending" with the setup kept for the email retry and the Postmark reason  (email_pending, 5 tasks kept)
  PASS  sent again: only the email is retried, ClickUp is not touched, the deal is then "done"  (ended at Done, memory done)

Edge cases in the deal data
  PASS  no email address: project created, Slack says so, run still a success  (ended at Done)
  PASS  a due date that would be in the past is moved to today (the next business day on a weekend)  (due today (2026-10-20))
  PASS  unknown package left empty with a warning; "$1,250.50" read as 1250.5  (fields sent: Start Date = 1792512000000, Deal Value = 1250.5)
  PASS  email added in the CRM and sent again: only the welcome email goes out  (resume_email, ended at Done)

Start date on a Saturday
  PASS  every due date on a weekday, the Saturday kickoff moved to Monday and flagged, the start date itself kept  (Send Fri Mar 01, Set Thu Mar 07, Week Fri Mar 08, Kickoff Mon Mar 04, Collect Tue Mar 05)

Two copies at the same moment
  PASS  one list, one email, one Slack message; the second run stops quietly  (template answers [200, 400], runs ended at ['Another Run Has It', 'Done'])
  PASS  a third copy afterwards: the deal memory survived the race, answered "duplicate", no API call  (200 duplicate, 0 API calls)

Deal id "constructor"
  PASS  treated as a new deal, not as a record that already exists  (202 accepted create, ended at Done)
  PASS  sent again: answered "duplicate", no API call  (200 duplicate)

Slack webhook is gone
  PASS  project and email done, Slack retried 3 times, run failed with the message in the error  (ended at Stop: Slack Failed)

Folder instead of space
  PASS  lists read and created in the folder, setup completes (KICKOFF_TASKS_JSON still in single quotes)  (ended at Done)

Error alert workflow
  PASS  one Slack alert per failed run, naming the Stop node and the reason  (19 alerts for 19 failed runs: Stop: Bad Secret x4, Stop: Onboarding Failed x12, Stop: Rejected Deal x2, Stop: Slack Failed x1)

59/59 checks passed in 29 scenarios, 420 requests to the mocks
```

## What the workflow sent (happy path, deal D-1042)

Every ClickUp call carried `Authorization: pk_...` (the personal token as is, without "Bearer"), and every ClickUp POST and PUT also `Content-Type: application/json`. The Postmark call carried exactly `Accept: application/json`, `Content-Type: application/json` and `X-Postmark-Server-Token`.

**ClickUp: Find Existing List** (`GET /api/v2/space/90150001/list?archived=false`): the space had no list with `[D-1042]` in its name. It did have `Old Client Onboarding [D-104]`, which must not count as a match, and it did not.

**ClickUp: Create From Template** (`POST /api/v2/space/90150001/list_template/t-90160001`)

```json
{"name": "Harbor Dental Studio Onboarding [D-1042]", "options": {"return_immediately": false, "content": "Client onboarding for Harbor Dental Studio. Won deal D-1042 (from a CRM webhook). Package: Growth. Value: $4,800.00. Start: Monday, March 4, 2030. Contact: Maya Chen (maya.chen@example.com).", "custom_fields": true, "include_views": true, "old_statuses": true, "subtasks": true, "old_checklists": true, "old_tags": true, "priority": true, "automation": true, "old_due_date": false, "old_start_date": false}}
```

**ClickUp: Get Custom Fields** (`GET /api/v2/list/901100000003/field`), then **ClickUp: Get Tasks** (`GET /api/v2/list/901100000003/task?include_closed=true`) for pages 0, 1.

**ClickUp: Create Task** for a kickoff task the template does not have (`POST /api/v2/list/901100000003/task`)

```json
{"name": "Week 1 check-in", "assignees": [111111], "due_date": 1899475200000, "due_date_time": false, "custom_fields": [{"id": "9c27ba63-6430-5418-8f29-b3a807567d38", "value": "ff75d55c-47b1-511c-81d4-e055d910f74a"}, {"id": "76dbcd4a-7138-54db-b943-c6ad786f7d40", "value": 1898874000000, "value_options": {"time": false}}, {"id": "ace18442-3db7-57bb-a9b4-c3479a86108e", "value": 4800}]}
```

The due date is Mon Mar 11 2030, 12:00 in New York (16:00 UTC). The clocks in New York changed on March 10, which is why noon is 16:00 UTC here and 17:00 UTC a week earlier. The three custom fields are Package (the uuid of the "Growth" option), Start Date and Deal Value.

**ClickUp: Update Task** for the template's "Kickoff call" task (`PUT /api/v2/task/86mock0007`)

```json
{"due_date": 1898874000000, "due_date_time": false, "assignees": {"add": [111111, 222222], "rem": []}, "priority": 2}
```

followed by one **ClickUp: Set Custom Field** per field (`POST /api/v2/task/86mock0007/field/{field_id}`):

```json
{"value": "ff75d55c-47b1-511c-81d4-e055d910f74a"}
{"value": 1898874000000, "value_options": {"time": false}}
{"value": 4800}
```

**Postmark: Send Welcome Email** (`POST https://api.postmarkapp.com/email`)

```json
{
  "From": "Acme Growth <onboarding@acmegrowth.example.com>",
  "To": "maya.chen@example.com",
  "Subject": "Welcome aboard, Maya! Your onboarding starts Mon, Mar 4, 2030",
  "TextBody": "Hi Maya,\n\nThank you for choosing Acme Growth. We are excited to get started on the Growth package for Harbor Dental Studio.\n\nYour onboarding starts on Monday, March 4, 2030. Here is what happens next:\n\nMonday, March 4, 2030: Kickoff call with your account team\nWednesday, March 6, 2030: Share your logins and brand assets\nMonday, March 11, 2030: Week 1 check-in call\n\nBook a time for your kickoff call here: https://calendar.example.com/acme/kickoff\n\nIf you have any questions, just reply to this email.\n\nTalk soon,\nThe Acme Growth team",
  "HtmlBody": "<html><body style=\"font-family: Arial, sans-serif; font-size: 15px; line-height: 1.5; color: #222;\"><p>Hi Maya,</p><p>Thank you for choosing Acme Growth. We are excited to get started on the Growth package for Harbor Dental Studio.</p><p>Your onboarding starts on <strong>Monday, March 4, 2030</strong>. Here is what happens next:</p><ul><li><strong>Monday, March 4, 2030:</strong> Kickoff call with your account team</li><li><strong>Wednesday, March 6, 2030:</strong> Share your logins and brand assets</li><li><strong>Monday, March 11, 2030:</strong> Week 1 check-in call</li></ul><p><a href=\"https://calendar.example.com/acme/kickoff\">Book a time for your kickoff call</a></p><p>If you have any questions, just reply to this email.</p><p>Talk soon,<br>The Acme Growth team</p></body></html>",
  "Tag": "client-onboarding",
  "Metadata": {
    "deal_id": "D-1042"
  },
  "MessageStream": "outbound",
  "ReplyTo": "success@acmegrowth.example.com"
}
```

**Slack: Post Update**

```
:white_check_mark: *Onboarding ready: Harbor Dental Studio*
Deal D-1042 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
ClickUp: <https://app.clickup.com/9012345/v/li/901100000003|Harbor Dental Studio Onboarding [D-1042]>
Kickoff tasks:
• Send welcome packet, due Fri, Mar 1, 2030
• Kickoff call, due Mon, Mar 4, 2030 (from the template)
• Collect logins and brand assets, due Wed, Mar 6, 2030 (from the template)
• Set up CRM pipeline, due Fri, Mar 8, 2030
• Week 1 check-in, due Mon, Mar 11, 2030
Welcome email sent to maya.chen@example.com
```

## Slack messages on the other paths

**ClickUp token rejected** (no list, no email):

```
:x: *ClickUp project NOT created: Northwind Pet Clinic*
Deal D-3001 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
Step: ClickUp: Find Existing List
Error: 401 Token invalid (OAUTH_025)
No welcome email was sent. Fix the cause, then send the same webhook again.
```

**Template call failed twice with a 500** (tried a second time only because the check after the first try found no list):

```
:x: *ClickUp project NOT created: Cedar Lane Bakery*
Deal D-3002 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
Step: ClickUp: Create From Template
Error: 500 Internal server error (TEMPLATE_500). The call was made twice, and after each try no list for this deal existed.
No welcome email was sent. Fix the cause, then send the same webhook again.
```

**Template call refused with a 404** (a wrong template id: no second try, because it would fail the same way):

```
:x: *ClickUp project NOT created: Juniper Lane Dental*
Deal D-3013 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
Step: ClickUp: Create From Template
Error: 404 Template not found (TEMPLATE_001). Checked again afterwards: no list for this deal exists.
No welcome email was sent. Fix the cause, then send the same webhook again.
```

**Template call lost** (no answer, no list made, so one more try, which worked):

```
:white_check_mark: *Onboarding ready: Silver Pine Dentistry*
Deal D-3012 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
ClickUp: <https://app.clickup.com/9012345/v/li/901100000007|Silver Pine Dentistry Onboarding [D-3012]>
:information_source: The first call to ClickUp failed and no list had been made, so the workflow tried once more.
Kickoff tasks:
• Send welcome packet, due Fri, Mar 1, 2030
• Kickoff call, due Mon, Mar 4, 2030 (from the template)
• Collect logins and brand assets, due Wed, Mar 6, 2030 (from the template)
• Set up CRM pipeline, due Fri, Mar 8, 2030
• Week 1 check-in, due Mon, Mar 11, 2030
Welcome email sent to owen.reyes@example.com
```

**Template call failed, and ClickUp was also down when the workflow looked for the list again:**

```
:x: *ClickUp project NOT created: Granite Peak Fitness*
Deal D-3010 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
Step: ClickUp: Find List Again
Error: Creating the list failed (500 Internal server error (TEMPLATE_500)), then checking for the list failed: 503 Service unavailable (APP_503)
No welcome email was sent. Fix the cause, then send the same webhook again.
```

**One task could not be created** (the welcome email waits):

```
:warning: *Onboarding only partly set up: Maple Street Physio*
Deal D-3004 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
ClickUp: <https://app.clickup.com/9012345/v/li/901100000008|Maple Street Physio Onboarding [D-3004]>
In the list now:
• Send welcome packet, due Fri, Mar 1, 2030
• Kickoff call, due Mon, Mar 4, 2030 (from the template)
• Collect logins and brand assets, due Wed, Mar 6, 2030 (from the template)
• Week 1 check-in, due Mon, Mar 11, 2030
Failed:
• "Set up CRM pipeline": create task: 500 Internal server error (ITEM_500)
The welcome email has NOT been sent. Fix the cause, then send the same webhook again: the workflow continues on this list and only does what is missing.
```

**The same webhook sent again** after the cause was fixed (only the missing task is created, then the email):

```
:white_check_mark: *Onboarding ready: Maple Street Physio*
Deal D-3004 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
ClickUp: <https://app.clickup.com/9012345/v/li/901100000008|Maple Street Physio Onboarding [D-3004]>
:information_source: Picked up where the last run stopped. Nothing was created twice.
Kickoff tasks:
• Send welcome packet, due Fri, Mar 1, 2030 (already in the list)
• Kickoff call, due Mon, Mar 4, 2030 (already in the list)
• Collect logins and brand assets, due Wed, Mar 6, 2030 (already in the list)
• Set up CRM pipeline, due Fri, Mar 8, 2030
• Week 1 check-in, due Mon, Mar 11, 2030 (already in the list)
Welcome email sent to liam.novak@example.com
```

**ClickUp down while the tasks were read** (nothing in the list was changed):

```
:warning: *Onboarding only partly set up: Copper Kettle Cafe*
Deal D-3011 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
ClickUp: <https://app.clickup.com/9012345/v/li/901100000013|Copper Kettle Cafe Onboarding [D-3011]>
Failed:
• ClickUp: Get Tasks: 503 Service unavailable (APP_503)
The welcome email has NOT been sent. Fix the cause, then send the same webhook again: the workflow continues on this list and only does what is missing.
```

**Postmark refused the email** (the project is kept):

```
:warning: *Onboarding ready, but the welcome email failed: Brightside Accounting*
Deal D-3005 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
ClickUp: <https://app.clickup.com/9012345/v/li/901100000014|Brightside Accounting Onboarding [D-3005]>
Kickoff tasks:
• Send welcome packet, due Fri, Mar 1, 2030
• Kickoff call, due Mon, Mar 4, 2030 (from the template)
• Collect logins and brand assets, due Wed, Mar 6, 2030 (from the template)
• Set up CRM pipeline, due Fri, Mar 8, 2030
• Week 1 check-in, due Mon, Mar 11, 2030
Email to zoe.martin@example.com failed: 422 You tried to send to recipient(s) that have been marked as inactive. Found inactive addresses: zoe.martin@example.com. Inactive recipients are ones that have generated a hard bounce, a spam complaint, or a manual suppression. (Postmark error 406)
The ClickUp project is kept. Send the welcome email by hand, or fix the cause and send the same webhook again to retry only the email.
```

**A list for the deal was already in ClickUp:**

```
:information_source: *A ClickUp list already exists for Bayview Physio*
Deal D-2001 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
ClickUp: <https://app.clickup.com/9012345/v/li/901100000001|Bayview Physio Onboarding [D-2001]>
Nothing was created or changed and no welcome email was sent. This workflow has no record of setting up that list, so check that its kickoff tasks are in place and send the welcome email by hand if the client has not had it.
```

**GoHighLevel payload without a start date** (the `&` is escaped for Slack, the start date is flagged):

```
:white_check_mark: *Onboarding ready: Ortiz &amp; Daughters Roofing*
Deal opp5Rk9Tz2Hx7Wc1Qf4J · Scale · $6,200.00 · starts Tue, Oct 27, 2026
ClickUp: <https://app.clickup.com/9012345/v/li/901100000004|Ortiz &amp; Daughters Roofing Onboarding [opp5Rk9Tz2Hx7Wc1Qf4J]>
Kickoff tasks:
• Send welcome packet, due Mon, Oct 26, 2026
• Kickoff call, due Tue, Oct 27, 2026 (from the template)
• Collect logins and brand assets, due Thu, Oct 29, 2026 (from the template)
• Set up CRM pipeline, due Mon, Nov 2, 2026
• Week 1 check-in, due Tue, Nov 3, 2026
Welcome email sent to elena.ortiz@example.com
:warning: No start date on the deal, so it was set to Tue, Oct 27, 2026 (5 business days from today)
```

**A half finished list deleted in ClickUp, answered with "Team not authorized"** (the deal is cleared, so the next webhook builds a new list):

```
:warning: *Onboarding only partly set up: Hollow Oak Pilates*
Deal D-3014 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
Failed:
• ClickUp: Get Custom Fields: 401 Team not authorized (OAUTH_027)
The list is gone from ClickUp, or this token can no longer open it, so this deal was cleared. Send the same webhook again to build a new list.
```

**The token revoked between two tries** (the deal and its list are kept, and the next webhook with a working token finishes on the same list):

```
:warning: *Onboarding only partly set up: Copper Kettle Cafe*
Deal D-3011 · Growth · $4,800.00 · starts Mon, Mar 4, 2030
ClickUp: <https://app.clickup.com/9012345/v/li/901100000013|Copper Kettle Cafe Onboarding [D-3011]>
Failed:
• ClickUp: Get Custom Fields: 401 Token invalid (OAUTH_025)
The welcome email has NOT been sent. Fix the cause, then send the same webhook again: the workflow continues on this list and only does what is missing.
```

**A start date on a Saturday** (the kickoff call is moved to Monday and flagged; the other tasks count business days):

```
:white_check_mark: *Onboarding ready: Foxglove Florist*
Deal D-3015 · Growth · $4,800.00 · starts Sat, Mar 2, 2030
ClickUp: <https://app.clickup.com/9012345/v/li/901100000016|Foxglove Florist Onboarding [D-3015]>
Kickoff tasks:
• Send welcome packet, due Fri, Mar 1, 2030
• Kickoff call, due Mon, Mar 4, 2030 (from the template)
• Collect logins and brand assets, due Tue, Mar 5, 2030 (from the template)
• Set up CRM pipeline, due Thu, Mar 7, 2030
• Week 1 check-in, due Fri, Mar 8, 2030
Welcome email sent to rosa.diaz@example.com
:warning: "Kickoff call" would have been due Sat, Mar 2, 2030, which is on a weekend, so it is due Mon, Mar 4, 2030
```

## Error alert workflow

Every failed run ended in a Stop node, and the error alert workflow posted one alert for each of the 19 failed runs, with a link to the execution. Two of them:

```
:rotating_light: *TEST won-deal-to-clickup-onboarding (temporary)* failed at *Stop: Onboarding Failed*
Onboarding for Maple Street Physio (deal D-3004): the setup is incomplete (1 change failed). The details were posted to Slack.
http://localhost:5678/workflow/YFro2qpxW3kiIdsO/executions/10033
```

```
:rotating_light: *TEST won-deal-to-clickup-onboarding (temporary)* failed at *Stop: Bad Secret*
Webhook call rejected: the shared secret is missing or wrong. If your CRM lands here, check WEBHOOK_SECRET and the X-Webhook-Secret header (or the webhook_secret custom data key).
http://localhost:5678/workflow/YFro2qpxW3kiIdsO/executions/10002
```

## Found by testing during the build

| Run | What happened | Fix |
|---|---|---|
| First run | 4 checks failed: the Slack messages did not show ClickUp's or Postmark's error reason | A probe showed how n8n 2.19.5 hands over an HTTP error: the status in `error.status` and the response body inside the message (`401 - "{...}"`). The error parser was written for that shape. It now also reads `error.statusCode` and `error.response.status`, where the paginated Get Tasks call keeps them |
| Fifth run | 0 alerts for 14 failed runs | The temporary error workflow was not active. n8n 2.x only runs an active error workflow, so the test activates it, and the README and setup guide say to activate it |
| Later runs | A scenario for a failure of the paginated Get Tasks call was missing | Added ("ClickUp down while reading the tasks") |
| Later runs | A template call lost on the network ended as "ClickUp project NOT created", because that call was never retried | The check after a failed call already proves whether a list exists. Now, when no list exists and the call got no answer or a 5xx, the call is made once more. A 4xx still stops at once. Added the "Template call lost" scenario, and the 500 scenario now expects two tries |
| Later runs | On a busy n8n the second of two simultaneous webhooks started 2 seconds late, saw the finished list and took the "already exists" path, so the race was not tested | The mock now holds both list lookups for 4 seconds instead of 1.5, so they always overlap |

## Found in review

A second pass checked every node against n8n 2.19.5 and every API call against the official ClickUp, Postmark and GoHighLevel docs, then tightened the tests. The new checks were first run against the workflow from before the review: 48 of 50 passed, and the 2 failures were the first two rows below.

| What was wrong | Fix |
|---|---|
| A new deal with the id `constructor` was answered "duplicate" and nothing was built. The deal memory was read with a plain lookup, which also finds built-in JavaScript properties such as `constructor` or `toString` | Records are read and written as own properties only. New scenario `Deal id "constructor"` |
| `KICKOFF_TASKS_JSON` with the single quotes of the `.env` example still around it was refused as "not valid JSON" (Docker Compose removes the quotes, `docker run --env-file` does not) | One pair of surrounding single quotes is accepted. The folder copy of the workflow is tested with the quoted form |
| The README said a 4xx from the template call is not tried again, but no test covered it | New scenario "ClickUp refuses the template call (404)" |
| The happy path collected task updates in a dictionary by task id, so a repeated update would have passed | Every update is now counted |
| The Postmark check accepted any `Accept` header containing `application/json`, which n8n's default header already does | The header must be exactly `application/json` |
| The mock accepted an assignee update without `rem`, which ClickUp's Update Task schema requires | Both `add` and `rem` are required |
| The race test did not count the tasks or check the deal memory afterwards | It counts the tasks and sends a third copy, which must be answered "duplicate" with no API call |

## Found in the second review

A further pass looked for paths that leave a deal stuck, dates the README promises but the code does not keep, and parts of the design the tests never looked at. The new checks were first run against the workflow from before this pass: 52 of 59 passed. These failed:

```
FAIL  deal memory: one small "done" record with the list and the Postmark message id
FAIL  nothing created, no email, Slack links the existing list
FAIL  deal memory after the resume: "done" on the same list, the old failure and the setup copy dropped
FAIL  sent again: the 401 OAUTH_027 is read as a list that is gone, the deal is cleared
FAIL  sent a third time: a new list is built from scratch
FAIL  sent again: only the email is retried, ClickUp is not touched, the deal is then "done"
FAIL  every due date on a weekday, the Saturday kickoff moved to Monday and flagged, the start date itself kept
```

| What was wrong | Fix |
|---|---|
| A deleted half finished list only cleared the deal when ClickUp answered 404. ClickUp's docs list "Team not authorized" codes for ids a token cannot reach, and with that answer every resend failed on the same missing list, so the deal was stuck for good | Those codes (OAUTH_023, OAUTH_026, OAUTH_027, OAUTH_029 to OAUTH_045) and a 4xx that says "not found" or "deleted" also clear the deal. A missing or revoked token keeps it. New scenario for each |
| A start date on a Saturday or Sunday gave tasks with `offset_days` 0 a weekend due date, although the README promises business days | A due date on a weekend moves to the next business day, with a Slack warning. The start date is kept as sent. New scenario |
| A finished deal kept the full copy of its setup and its old failures in static data, which n8n rewrites as one block on every run | Once the email is out, only the list, the dates and Postmark's message id stay |
| The "already exists" Slack message did not say the list may be half finished | It asks the team to check the tasks and send the welcome email by hand if needed |
| No check looked at the deal memory itself, the thing that stops duplicates | The test reads the records through the n8n API at five points and checks their exact fields |
| No GoHighLevel payload with a status other than won was tested | New scenario: lost and abandoned are ignored |

## Lost requests between Docker and the mocks

n8n runs in Docker and reaches the mocks through Docker Desktop's host networking (`host.docker.internal`), and other test runs used the same n8n at the same time. Across 22 full runs with request dumps (about 6,700 requests), at least 17 requests that n8n sent never reached the mock, in 11 of the runs. They were GETs, POSTs and PUTs to all three mocks. n8n waited for its timeout each time and then carried on as it does when a real API does not answer. A separate probe sent 400 requests to the mock from the host and 400 from inside the n8n container with nothing else running, and none was lost. A bigger listen backlog on the mock did not change anything. n8n sends `Connection: close` on every call, so reused connections are not the cause either: in the review, four full runs with a mock that also closes every connection itself (HTTP/1.0) still lost one request in about 1,300.

In every case the workflow did what it is built to do:

- a lost list lookup, task update, custom field call, Postmark attempt or Slack post was retried, and the run finished normally,
- a lost template call led to the check for the list and, with the change listed under "Found by testing during the build", to one more try,
- when the last Postmark attempt was lost, Slack reported the email as failed with the timeout as the reason, and the project was kept.

In 6 of the 22 runs a check failed because of a lost request: the check expected an exact number of calls or the mock's own error text, and the network changed one of them. The test prints a NOTE that names the lost call, or the silence it left in the request log, so a failure like that is not mistaken for a workflow bug. In the second review it happened again in 1 of 4 full runs: the last of the three Postmark attempts for deal D-3005 never arrived, so Slack and the deal memory held the timeout instead of Postmark's 406, and 2 checks failed with a NOTE naming that call. Two more runs lost a request that a retry covered. The final run above had no lost request.

All names, companies, email addresses and ids above are made up.
