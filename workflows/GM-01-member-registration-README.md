# GM-01 - Gym Member Registration (Crush Lifestyle Fitness Studio)

Importable n8n 2.x workflow that powers front-desk gym member registration for **Crush Lifestyle Fitness Studio**, validates and normalizes incoming records for Indian mobile numbers and `Asia/Kolkata` dates, generates unique `MEM-XXXXXXXX` IDs, appends rows to Google Sheets, and renders clear success or failure pages back to the staff browser.

---

## 1. How GM-01 Fits Into the Gym Automation Suite

Based on [`n8n_Gym_Manager_Automation_Complete_Setup_Guide.md`](../n8n_Gym_Manager_Automation_Complete_Setup_Guide.md), the complete gym management system spans 4 coordinated workflows backed by one Google Sheets workbook (**Crush Lifestyle Fitness Studio**):

| Workflow ID | Purpose | Trigger | Primary Sheets Used |
| --- | --- | --- | --- |
| **GM-01 (This Workflow)** | New Member Registration | n8n Form Trigger | `Members` |
| **GM-02 (Planned)** | Expiry WhatsApp Reminder | Schedule Trigger (`09:00 IST`) | `Members`, `Automation Log` |
| **GM-03 (Planned)** | Payment Status & Renewal Update | Razorpay Webhook / Staff Form | `Members`, `Payments` |
| **GM-04 (Planned)** | Daily Owner Summary Report | Schedule Trigger (`IST`) | `Members`, `Payments`, `Attendance` |

### Key Alignments with the Setup Guide
- **Plain-Text ISO Dates (`YYYY-MM-DD`)**: Prevents Google Sheets locale auto-parsing bugs and ensures `SUMPRODUCT` / string comparisons work cleanly across GM-02 and GM-04.
- **Canonical Indian Mobile Format (`91XXXXXXXXXX`)**: Strips `+`, spaces, leading `0`, and validates `[6-9]\d{9}`, storing numbers directly in WhatsApp Cloud API format.
- **Server-Side ID Generation**: Never trusts user-supplied IDs; generates `MEM-XXXXXXXX` on the server.
- **Railway & `Asia/Kolkata` Timezone**: Workflow settings force `timezone: "Asia/Kolkata"`, matching IST date calculations.

---

## 2. Node Descriptions

| # | Node Name | Type (`typeVersion`) | Purpose |
| --- | --- | --- | --- |
| 1 | **n8n Form Trigger** | `n8n-nodes-base.formTrigger` (`2.2`) | Renders the front-desk form titled `Crush Lifestyle Fitness Studio - Member Registration` at path `/form/crush-member-registration` with 12 fields (5 required, 7 optional). Uses `responseMode: "lastNode"` so the browser receives the final completion/error screen. |
| 2 | **Code - Validate Registration** | `n8n-nodes-base.code` (`2`) | Validates required fields, Indian mobile numbers (`Phone Number` and optional `Emergency Contact Phone`), email syntax, optional `Gender` / `Date of Birth`, supported `Membership Plan`, non-negative `Membership Fee`, valid calendar `Start Date`, and `Payment Status`. Normalizes phones to `91XXXXXXXXXX` and strips any spoofed `member_id`. |
| 3 | **IF - Validation Passed** | `n8n-nodes-base.if` (`2.2`) | Routes valid submissions (`$json.is_valid === true`) to record generation, and invalid submissions (`false`) directly to **Respond - Validation Error** without touching Google Sheets. |
| 4 | **Code - Generate Member Record** | `n8n-nodes-base.code` (`2`) | Generates unique `MEM-XXXXXXXX` IDs, computes `expiry_date` from plan duration (`Monthly: 30`, `Quarterly: 90`, `Half-Yearly: 180`, `Yearly: 365` days), sets `member_status = "Active"`, sets `last_payment_date = start_date` when `Paid` (or `""` when `Pending`), initializes `last_reminder_date = ""`, and shapes the exact 18 `Members` columns. |
| 5 | **Google Sheets - Append Member** | `n8n-nodes-base.googleSheets` (`4.5`) | Appends the 18-column row into the `Members` tab of `Crush Lifestyle Fitness Studio` using `mappingMode: "defineBelow"`. Configured with `onError: "continueErrorOutput"` so Sheets errors route to the failure handler instead of falsely reporting success. |
| 6 | **Respond to Registration** | `n8n-nodes-base.form` (`1`) | Displays a confirmation screen containing Member Name, Member ID, Membership Plan, Start Date, Membership Expiry Date, Payment Status, and registration confirmation message. |
| 7 | **Respond - Validation Error** | `n8n-nodes-base.form` (`1`) | Displays numbered validation errors so front-desk staff can fix mistakes and resubmit; never writes invalid rows to Google Sheets. |
| 8 | **Respond - Storage Error** | `n8n-nodes-base.form` (`1`) | Executed only when **Google Sheets - Append Member** fails; displays the generated Member ID/name alongside the actual Sheets error message so staff know the write failed. |

---

## 3. Import Instructions (n8n 2.x)

1. Open your n8n instance on Railway.
2. Click **Create Workflow** (or open an existing empty workflow).
3. Open the top-right workflow menu (`...`) and choose **Import from File...**.
4. Select `workflows/GM-01-member-registration.json`.
5. Keep the workflow **Inactive** (`active: false`) until you complete Google Sheets credential and spreadsheet configuration below.

---

## 4. Google Sheets Credential & Workbook Setup

### A. Create Google Sheets OAuth2 Credential in n8n
No credentials are embedded in the workflow JSON. After import:
1. In n8n, go to **Credentials -> Add Credential -> Google Sheets OAuth2 API**.
2. In **Google Cloud Console** (for the Google account owning the gym spreadsheet):
   - Enable the **Google Sheets API** (and **Google Drive API** if you want the spreadsheet picker list to populate automatically).
   - Configure the OAuth Consent Screen and create an **OAuth 2.0 Client ID** (Web application).
   - Add your Railway n8n OAuth Redirect URI (copied from the n8n credential modal, e.g., `https://<your-railway-domain>/rest/oauth2-credential/callback`).
3. Paste the **Client ID** and **Client Secret** into n8n and click **Sign in with Google** to authorize access.
4. Save the credential (for example, `Crush Fitness Google Sheets`).

### B. Prepare the `Crush Lifestyle Fitness Studio` Workbook
Verify your Google Sheets workbook has all 6 tabs:
1. `Members`
2. `Membership Plans`
3. `Payments`
4. `Attendance`
5. `Automation Log`
6. `Gym Settings`

Ensure Row 1 of the **`Members`** tab contains these exact 18 headers in order (columns `A` through `R`):
```text
member_id | name | phone | email | gender | date_of_birth | address | emergency_contact_name | emergency_contact_phone | plan | fee | start_date | expiry_date | payment_status | last_payment_date | last_reminder_date | member_status | notes
```

> **Important Formatting Tip (from the Setup Guide):** Select columns `date_of_birth`, `start_date`, `expiry_date`, `last_payment_date`, and `last_reminder_date` (as well as `phone` and `emergency_contact_phone`) in Google Sheets and choose **Format -> Number -> Plain text**. This prevents Sheets from reformatting `YYYY-MM-DD` dates into locale dates or turning `919876543210` into scientific notation.

Ensure Row 1 of **`Membership Plans`** has:
```text
plan_id | plan_name | duration_days | price | description | is_active
```
And seed the 4 plans (`Monthly` = `30`, `Quarterly` = `90`, `Half-Yearly` = `180`, `Yearly` = `365`).

### C. Connect the Credential & Spreadsheet in the Imported Workflow
1. Open the **Google Sheets - Append Member** node.
2. Under **Credential to connect with**, select the Google Sheets OAuth2 credential you created.
3. Under **Document**:
   - Either switch **From list** and select **Crush Lifestyle Fitness Studio**, **or**
   - Keep **By ID** and replace `REPLACE_WITH_GOOGLE_SHEET_ID` with the Spreadsheet ID from your Google Sheets URL (`https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`).
4. Confirm **Sheet** is set to `Members` (by name) or select `Members` from the dropdown.
5. Verify the 18 column mappings under **Values to Send** match your header row, then **Save** the workflow.

---

## 5. Form Testing Instructions

### Step 1: Test with Test URL (Before Activating)
1. Click **n8n Form Trigger** and expand **Form URLs**.
2. Copy the **Test URL** and click **Execute Workflow** (or **Test Step**).
3. Open the Test URL in a browser and submit a test payload (see `workflows/GM-01-member-registration-test-data.json` for sample valid and invalid payloads).
4. Verify:
   - **Valid Paid submission**: Completion page shows Member Name, `MEM-XXXXXXXX` ID, Plan, Expiry Date (`start_date + duration_days`), and `Paid`. In Google Sheets `Members`, `last_payment_date` equals `start_date`, `last_reminder_date` is empty, and `member_status` is `Active`.
   - **Valid Pending submission**: Same as above, except `payment_status` is `Pending` and `last_payment_date` is empty (`""`).
   - **Validation error submission** (e.g., phone `12345` or negative fee): Browser displays `Registration Validation Failed` with numbered errors, and NO row is added to Google Sheets.
   - **Storage error simulation** (temporarily put an invalid Sheet tab name or disconnect credentials): Browser displays `Registration Failed - Storage Error` with the Sheets error message instead of falsely showing success.

### Step 2: Activate for Front-Desk Production Use
1. Revert any temporary error test changes and **Save**.
2. Toggle the workflow **Active** (`Published`) in the top-right corner of n8n.
3. Open **n8n Form Trigger -> Production URL** (e.g., `https://<your-railway-domain>/form/crush-member-registration`) and bookmark it on the gym front-desk tablet/browser.

---

## 6. Railway Deployment Considerations

When running n8n on Railway behind Railway's reverse proxy, verify these environment variables in your Railway service settings:

| Variable | Recommended Value | Why It Matters |
| --- | --- | --- |
| `WEBHOOK_URL` | `https://<your-service>.up.railway.app/` | Ensures the Form Trigger Production URL and Google OAuth2 callback URL use your public HTTPS domain instead of `localhost:5678`. |
| `N8N_EDITOR_BASE_URL` | `https://<your-service>.up.railway.app/` | Keeps UI links and Form Trigger previews consistent with your public domain. |
| `GENERIC_TIMEZONE` | `Asia/Kolkata` | Sets the default timezone for all n8n expressions (`$now`, `$today`) and schedule triggers. |
| `TZ` | `Asia/Kolkata` | Ensures Node.js runtime `Date` operations and container logs run in IST. |
| `N8N_PROXY_HOPS` | `1` | Allows Express/n8n to trust Railway's `X-Forwarded-*` headers properly. |

### Optional Production Hardening
- **Error Workflow**: Following Chapter 8 of `n8n_Gym_Manager_Automation_Complete_Setup_Guide.md`, create a separate **Error Trigger -> Email/WhatsApp Alert** workflow and assign it under **Workflow Settings -> Error Workflow** for unhandled node/runtime failures.
- **Duplicate Phone Policy**: Currently **Google Sheets - Append Member** uses `operation: "append"` per specification. If gym staff later want to prevent accidental double-submissions for an already-registered phone number, add a `Google Sheets -> Get Row(s)` duplicate check on `phone` before appending, or log registration events to the `Automation Log` tab.
- **DPDP Act 2023 & WhatsApp Opt-In**: Before activating GM-02 WhatsApp reminders, ensure members give consent on registration (or add a WhatsApp consent checkbox to the Form Trigger if required by your studio policy).
