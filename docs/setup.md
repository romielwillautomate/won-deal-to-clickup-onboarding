# Setup: ClickUp, Postmark, Slack, your CRM and n8n

You need four things before the workflow can run: a ClickUp list template, a Postmark sender, a Slack incoming webhook and your CRM pointed at n8n. Plan about 30 minutes, most of it for the ClickUp template.

## 1. ClickUp

### Build the onboarding template

1. In the space (or folder) where the onboarding lists should live, create a list, for example "Template: Client Onboarding".
2. Set up its statuses and views the way your team works.
3. Add three custom fields to the list. The names can be different, as long as you put them in the `CLICKUP_FIELD_*` variables.

   | Field | ClickUp type | Notes |
   |---|---|---|
   | Package | Dropdown | One option per package, spelled the way your CRM sends it (the match ignores case) |
   | Start Date | Date | |
   | Deal Value | Money | Pick your currency |

4. Add the tasks every onboarding has: the kickoff call, the internal handover, whatever you always do. The tasks you also name in `KICKOFF_TASKS_JSON` get their assignees, due date and priority per client. Other template tasks are left as they are.
5. Save the list as a template (the list's menu → Templates → Save as Template) and make sure custom fields and tasks are included.

The workflow names each new list `<client> Onboarding [<deal id>]`. Keep the bracket part if you rename a list by hand, because that is how the workflow recognizes it.

### API token and ids

1. **Token** (`CLICKUP_API_TOKEN`): click your avatar → Settings → Apps → API Token → Generate. It starts with `pk_` and does not expire. It acts as you, so the person who makes it needs access to the space.
2. **Workspace id** (`CLICKUP_WORKSPACE_ID`): open ClickUp in the browser. It is the first number after `app.clickup.com/`.
3. Everything else comes from four API calls. Run them in a terminal with your token:

```bash
# Workspace members: user ids for "assignees" are in teams[].members[].user.id
curl -H "Authorization: pk_YOUR_TOKEN" https://api.clickup.com/api/v2/team

# Spaces in your workspace: spaces[].id goes into CLICKUP_SPACE_ID
curl -H "Authorization: pk_YOUR_TOKEN" https://api.clickup.com/api/v2/team/WORKSPACE_ID/space

# Folders in a space, only if the lists should live in a folder: folders[].id goes into CLICKUP_FOLDER_ID
curl -H "Authorization: pk_YOUR_TOKEN" https://api.clickup.com/api/v2/space/SPACE_ID/folder

# List templates: templates[].id (it starts with "t-") goes into CLICKUP_TEMPLATE_ID
curl -H "Authorization: pk_YOUR_TOKEN" https://api.clickup.com/api/v2/team/WORKSPACE_ID/list_template
```

ClickUp personal tokens are sent as they are, without "Bearer". Templates shared publicly by someone else have to be added to your workspace before the API can use them.

### Kickoff tasks

Write `KICKOFF_TASKS_JSON` as one line. In a `.env` file, wrap it in single quotes so the double quotes inside survive. Docker Compose removes those quotes; `docker run --env-file` passes them on, and the workflow then removes them itself:

```bash
KICKOFF_TASKS_JSON='[{"name":"Send welcome packet","offset_days":-1,"assignees":[111111],"priority":3},{"name":"Kickoff call","offset_days":0,"assignees":[111111,222222],"priority":2,"in_email":true,"email_label":"Kickoff call with your account team"},{"name":"Collect logins and brand assets","offset_days":2,"assignees":[222222],"in_email":true,"email_label":"Share your logins and brand assets"},{"name":"Set up CRM pipeline","offset_days":4,"assignees":[222222]},{"name":"Week 1 check-in","offset_days":5,"assignees":[111111],"in_email":true,"email_label":"Week 1 check-in call"}]'
```

`offset_days` counts business days from the client's start date, so Saturdays and Sundays are skipped (public holidays are not). With a start date of Monday, March 4, the example gives Friday, March 1 for the welcome packet and Monday, March 11 for the week 1 check-in. If the CRM sends a start date on a weekend, the tasks with `offset_days` 0 are due the Monday after, and Slack says so. Every key is described in the README.

## 2. Postmark

1. In Postmark, open (or create) the server for your transactional email. Its default message stream is called `outbound`.
2. Under **Sender Signatures**, add and confirm the address you want to send from, or verify your whole domain. That address goes into `POSTMARK_FROM`, with a name if you like: `Acme Growth <onboarding@acmegrowth.example.com>`.
3. Open the server → **API Tokens** and copy the Server API token into `POSTMARK_SERVER_TOKEN`.
4. A new Postmark account starts in test mode: until Postmark approves it, you can only send to addresses on your own domain. Request approval before the first real client.

## 3. Slack

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) (from scratch), open **Incoming Webhooks** and switch them on.
2. Click **Add New Webhook to Workspace**, pick the channel for onboarding updates and copy the URL into `SLACK_WEBHOOK_URL`.

The error alert workflow posts to the same URL. If you want technical failures in a separate channel, give that workflow its own webhook URL.

## 4. n8n

1. Add the variables from `.env.example` to your n8n environment and restart n8n. Self-hosted n8n also needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`.
2. Import `workflow/won-deal-to-clickup-onboarding.json` and `workflow/error-alert-slack.json`.
3. Open the onboarding workflow's settings and choose the error alert workflow as its **Error Workflow**.
4. Activate the error alert workflow. n8n 2.x only runs an error workflow that is active; picking it in the settings is not enough.
5. Activate the onboarding workflow. Your webhook URL is `https://YOUR-N8N-DOMAIN/webhook/won-deal-onboarding`.

Your CRM has to reach n8n over HTTPS. A hosted n8n works as is. A local n8n needs a tunnel such as Cloudflare Tunnel or ngrok.

## 5. Connect your CRM

### GoHighLevel

1. Go to **Automation → Workflows** and create a workflow.
2. Trigger: **Opportunity Status Changed**, with the filter **Moved To Status** set to **Won** (add an **In Pipeline** filter too if you have several pipelines).
3. Action: **Webhook**, method POST, URL `https://YOUR-N8N-DOMAIN/webhook/won-deal-onboarding`.
4. Under **Custom Data**, add:

   | Key | Value |
   |---|---|
   | `webhook_secret` | the value of `WEBHOOK_SECRET` |
   | `package` | the package, typed in or picked from your opportunity custom fields |
   | `start_date` | optional, in the format `2030-03-04`. Without it the start is `DEFAULT_START_BUSINESS_DAYS` business days from today |

5. Save and publish the workflow.

The payload of this action already includes the opportunity id (`id`), `status`, `lead_value`, `company_name`, the contact's name and email, and your custom data under `customData`. The workflow uses the company name as the client name and falls back to the contact's full name. If your payload has no `id`, add a custom data key `deal_id` with the opportunity id.

GoHighLevel's standard Webhook action cannot set headers, which is why the secret travels as custom data. If your plan has the **Custom Webhook** action, you can send the secret in an `X-Webhook-Secret` header instead and write the body in the generic format below.

### Any other CRM, Zapier or Make

Send a POST with `Content-Type: application/json`, the header `X-Webhook-Secret: YOUR_SECRET` and this body:

```json
{
  "deal_id": "D-1042",
  "status": "won",
  "client_name": "Harbor Dental Studio",
  "contact_name": "Maya Chen",
  "contact_email": "maya.chen@example.com",
  "package": "Growth",
  "deal_value": 4800,
  "currency": "USD",
  "start_date": "2030-03-04"
}
```

Only `deal_id`, `status` and `client_name` are required. The deal id must stay the same for the same deal, because it is what stops duplicates.

## 6. Try it

Send a test deal with your own email address and a new deal id:

```bash
curl -X POST https://YOUR-N8N-DOMAIN/webhook/won-deal-onboarding \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOUR_SECRET" \
  -d '{"deal_id":"TEST-1","status":"won","client_name":"Test Client","contact_name":"Your Name","contact_email":"YOUR_EMAIL","package":"Growth","deal_value":1000,"start_date":"2030-03-04"}'
```

The answer is `{"status":"accepted","deal_id":"TEST-1","action":"create"}`. Within a few seconds the list appears in ClickUp, the email arrives and Slack gets the update. Send the same command again: the answer is `"duplicate"` and nothing else happens. The workflow remembers every deal id, so use a new one for each test.

## Troubleshooting

Open the workflow's **Executions** list in n8n. Every run ends on a node that tells you what happened.

| You see | Likely cause |
|---|---|
| No execution at all | The workflow is not active, or the CRM cannot reach your n8n URL |
| Webhook answers 401, run ends at "Stop: Bad Secret" | The secret does not match `WEBHOOK_SECRET`, or `WEBHOOK_SECRET` is empty |
| Webhook answers 500, run ends at "Stop: Rejected Deal" | A setting is missing or `KICKOFF_TASKS_JSON` is broken. The error message names it |
| Webhook answers 400 | The payload has no `deal_id` or no `client_name`. The answer lists what is missing |
| Webhook answers 200 "ignored" | The status is not won |
| Slack: "NOT created", `401 ... (OAUTH_...)` | `CLICKUP_API_TOKEN` is wrong, or it was pasted with "Bearer" in front |
| Slack: "NOT created", `404` | `CLICKUP_TEMPLATE_ID`, `CLICKUP_SPACE_ID` or `CLICKUP_FOLDER_ID` is wrong, or the token's user cannot see that space. A 4xx is not tried a second time |
| Slack: "NOT created", `400` from "ClickUp: Create From Template" | A list with exactly that name may already exist in the space or folder, possibly archived |
| Slack: "NOT created", "The call was made twice" | ClickUp failed or did not answer two times in a row, and no list was made. Wait a few minutes, then send the webhook again |
| Slack warning "The list has no custom field called ..." | The template was saved without its custom fields, or the names differ from the `CLICKUP_FIELD_*` settings |
| Slack warning "... is not an option of the Package field" | Add that package to the dropdown in ClickUp, or send the name exactly as it is in the dropdown |
| Slack: a task failed with `400 ... assignees` | A user id in `KICKOFF_TASKS_JSON` is not a member of the workspace |
| Slack: email failed, `401 ... (Postmark error 10)` | `POSTMARK_SERVER_TOKEN` is wrong |
| Slack: email failed, `422` with a message about the `From` address or a Sender Signature | `POSTMARK_FROM` is not a confirmed Sender Signature and not on a verified domain. Confirm it in Postmark |
| Slack: email failed, `422 ... (Postmark error 406)` | The client's address bounced before and Postmark marked it inactive. Reactivate it in Postmark |
| Slack: email failed, `422 ... (Postmark error 412)` | The Postmark account is still waiting for approval |
| Run ends at "Stop: Slack Failed" | `SLACK_WEBHOOK_URL` is wrong or the webhook was removed. The full message is in the error |
| Slack: "partly set up", `ClickUp: Get Custom Fields`, "this deal was cleared" | The list was deleted in ClickUp, or the token's user lost access to it (ClickUp answers 404 or "Team not authorized"). Send the webhook again to build a new list |
| Slack: "partly set up", `401 Token invalid (OAUTH_025)` | The token was revoked or regenerated. Put the new one in `CLICKUP_API_TOKEN`, then send the webhook again: the setup finishes on the same list |
| Slack: "A ClickUp list already exists" | ClickUp has a list with this deal id that the workflow has no record of (made by hand, or by a run whose record was lost). Nothing was changed. Check its tasks and send the welcome email by hand if needed |
| Webhook answers 200 "duplicate", but you want the project built again | The workflow remembers finished deals for 400 days. Send the deal with a new deal id |

After you fix the cause of a failed run, send the same webhook again. The workflow continues where it stopped and does not duplicate anything.
