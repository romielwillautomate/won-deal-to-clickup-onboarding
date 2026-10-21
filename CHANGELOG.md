# Changelog

## v1 (2026-10-21)

First build, tested end to end against mock ClickUp, Postmark and Slack APIs on n8n 2.19.5 (29 scenarios, 59 checks, all passing in the final run).

### Added

- A webhook for won deals that reads two shapes: a generic JSON format for any CRM, Zapier or Make, and the payload of GoHighLevel's workflow Webhook action for the "Opportunity Status Changed" trigger.
- A shared secret on every request, from the `X-Webhook-Secret` header or GoHighLevel custom data, compared in constant time. An empty `WEBHOOK_SECRET` refuses everything.
- Clear answers to the caller: 202 accepted, 200 ignored or duplicate, 400 for a bad payload, 401 for a bad secret, 500 for a broken setting (without details).
- Two layers against duplicates: the deal id in the workflow's static data, and a ClickUp lookup for a list with `[deal id]` in its name before anything is created.
- The onboarding list is created from a ClickUp list template with `return_immediately: false`, so ClickUp answers once the template is fully applied. The HTTP node has no automatic retry on this call. After a timeout or a 5xx the list is looked up again, because ClickUp keeps building a list past a timeout and refuses a second list with the same name. Only when no list exists is the call made once more.
- Kickoff tasks from `KICKOFF_TASKS_JSON`: template tasks with the same name are adjusted (assignees, due date, priority), missing ones are created. Due dates are business days from the start date, set at noon in `BUSINESS_TIMEZONE` so the day is right across daylight saving changes. A date in the past moves to today.
- Package, start date and deal value written to the list's custom fields (dropdown, date, money, number and text fields are supported). Fields and dropdown options are found by name, so nothing depends on field ids.
- Every page of the list's tasks is read (ClickUp returns 100 per page).
- Task changes run one at a time in a loop, so a retry after a 429 repeats only that change and never creates a task twice.
- A welcome email through Postmark with the start date, the client facing steps and an optional booking link, sent only when the whole setup worked. Client data is escaped in the HTML.
- A Slack update with the ClickUp link and every kickoff task, or a message that says what exists and what failed. Client data is escaped for Slack.
- Resume after a failure: sending the same webhook again finishes a half done setup on the same list, or retries only the email. A list deleted in ClickUp clears the deal so the next webhook starts fresh.
- Stop nodes for failed runs, the error alert workflow, sticky notes on the canvas and an end to end mock test.

### Found by testing during the build

- The Slack messages did not show ClickUp's or Postmark's error reason at first. A probe showed how n8n 2.19.5 hands over an HTTP error (the status in `error.status`, the response body inside the message as `401 - "{...}"`), and the error parser was written for that shape. It also reads `error.statusCode` and `error.response.status`, where a paginated call keeps them.
- The error alert workflow sent nothing for 14 failed runs because it was not active. n8n 2.x only runs an active error workflow, so the setup guide says to activate it.
- A template call lost on the network ended as "ClickUp project NOT created". The workflow now makes the call once more when the check after it finds no list and the call got no answer or a 5xx.
- The test for two simultaneous webhooks did not always force the race on a busy n8n. The mock now holds both list lookups long enough that they always overlap.

### Found in review

- A deal id that is also the name of a built-in JavaScript property (`constructor`, `toString` and the like) was answered "duplicate" and nothing was built, because the deal memory was read with a plain lookup that also finds inherited properties. Records are now read and written as own properties only. New test scenario.
- `KICKOFF_TASKS_JSON` wrapped in the single quotes of the `.env` example was refused as "not valid JSON" when the quotes reached n8n (`docker run --env-file` keeps them). One pair of surrounding single quotes is now accepted. The folder test copy uses the quoted form.
- The README said a 4xx from the template call is not tried again, but no test covered it. New scenario: a 404 for a wrong template id gives one try, a check for the list and a Slack alert.
- Stricter checks: the happy path now counts every task update exactly (a repeated update would have passed before), the Postmark `Accept` header must be exactly `application/json` (n8n's default Accept header also contains it), the mock refuses an assignee update without both `add` and `rem` (both are required in ClickUp's schema), the race test counts the tasks and sends a third copy afterwards, and the empty secret test checks where each run ended.
- Docs: the GoHighLevel trigger filter is called "Moved To Status", custom field values are stored per task in ClickUp, the Mermaid chart shows the "another run has it" and 4xx branches, and three limitations were added (a template still building after the wait, two copies during a resume, alert noise from forged requests).

### Found in the second review

The new checks were first run against the workflow from before this pass: 52 of 59 passed, and the 7 failures were the problems below.

- A half finished list that was deleted in ClickUp only cleared the deal when ClickUp answered 404. ClickUp's docs list "Team not authorized" codes (OAUTH_023, OAUTH_026, OAUTH_027, OAUTH_029 to OAUTH_045) for ids a token cannot reach, and with that answer the deal stayed "setup incomplete" forever: every resend failed on the same missing list. Those codes now clear the deal too. A missing or revoked token (OAUTH_017, OAUTH_019, OAUTH_021, OAUTH_025, OAUTH_077) still keeps it, so the next webhook with a working token finishes on the same list. New scenario for each case.
- A start date on a Saturday or Sunday gave tasks with `offset_days` 0 a weekend due date, and a past date moved to "today" could also land on a weekend. Due dates on a weekend now move to the next business day, with a warning in Slack. The start date itself is kept as sent. New scenario.
- A finished deal kept the full copy of its setup (every task with its dates and links) and its old failures in the workflow's static data, which n8n rewrites as one block on every run. Once the email is out, only the list, the dates and Postmark's message id are kept.
- The "list already exists" Slack message did not say that such a list may be half finished (it can come from a run whose record was lost). It now asks the team to check the tasks and send the welcome email by hand if needed.
- New checks read the deal records from n8n's static data through the API: after the happy path, after a failed and a resumed setup, and after a failed and a retried email. A scenario for GoHighLevel opportunities that are lost or abandoned was added.
- Docs: the ClickUp rate limit line now gives the real call count per task, the setup guide's Postmark row no longer names an error code that Postmark's current docs do not list, and the troubleshooting table covers the cleared deal, a revoked token, the "already exists" message and how to build a finished deal again.
