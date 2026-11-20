/**
 * Comprehensive structural and JavaScript runtime validator for all n8n workflows
 * (GM-01 through GM-05) in Crush Lifestyle Fitness Studio.
 */

const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..');
const WORKFLOWS_DIR = path.join(REPO_ROOT, 'workflows');
const TEST_DATA_PATH = path.join(WORKFLOWS_DIR, 'GM-01-member-registration-test-data.json');

const EXPECTED_MEMBERS_COLUMNS = [
  'member_id', 'name', 'phone', 'email', 'gender', 'date_of_birth', 'address',
  'emergency_contact_name', 'emergency_contact_phone', 'plan', 'fee',
  'start_date', 'expiry_date', 'payment_status', 'last_payment_date',
  'last_reminder_date', 'member_status', 'notes',
];

const WORKFLOW_SPECS = [
  {
    file: 'GM-01-member-registration.json',
    name: 'GM-01 - Gym Member Registration',
    requiredNodes: [
      'n8n Form Trigger',
      'Code - Validate Registration',
      'IF - Validation Passed',
      'Code - Generate Member Record',
      'Sync Master PostgreSQL Database (FastAPI)',
      'Google Sheets - Append Member',
      'Respond to Registration',
      'Respond - Validation Error',
      'Respond - Storage Error',
    ],
  },
  {
    file: 'GM-02-expiry-and-followup-cadence.json',
    name: 'GM-02 - Smart Expiry, Dues & Onboarding Follow-Up Cadence',
    requiredNodes: [
      'Schedule Trigger - Daily 9AM IST',
      'Google Sheets - Get All Members',
      'Code - Evaluate Follow-Up Cadence',
      'Email - Send Follow-Up Notification',
      'Google Sheets - Update Reminder & Status',
      'Google Sheets - Append Automation Log',
      'HTTP Request - Notify Python AI Agent',
    ],
  },
  {
    file: 'GM-03-payment-and-renewal-webhook.json',
    name: 'GM-03 - Payment Webhook & Membership Renewal Engine',
    requiredNodes: [
      'Webhook - Payment Event',
      'Code - Parse & Validate Payment',
      'Google Sheets - Lookup Member',
      'Code - Compute Renewal & Invoice',
      'Google Sheets - Update Member Status',
      'Google Sheets - Append Payment Ledger',
      'HTTP Request - Sync Payment to Python Accounts Ledger',
      'Respond to Webhook',
    ],
  },
  {
    file: 'GM-04-attendance-streak-and-owner-digest.json',
    name: 'GM-04 - Daily Owner Digest & Inactivity Follow-Up',
    requiredNodes: [
      'Schedule Trigger - Daily 9PM IST',
      'Google Sheets - Read Members',
      'Code - Compute Studio KPIs & Drop-Off List',
      'HTTP Request - Run Python AI Autopilot & P&L Audit',
      'Google Sheets - Log Daily Digest',
    ],
  },
  {
    file: 'GM-05-ai-gym-manager-agent.json',
    name: 'GM-05 - Autonomous AI Gym Manager & Accounts Agent',
    requiredNodes: [
      'Webhook - AI Agent Command & Email Inbound',
      'Code - Normalize Inbound Agent Request',
      'HTTP Request - Execute Python AI Gym Agent',
      'Code - Prepare Sheets Audit & Reply',
      'Google Sheets - Log AI Agent Action',
      'Respond to Webhook',
    ],
  },
];

const SECRET_PATTERNS = [
  /AIza[0-9A-Za-z\-_]{35}/,
  /ya29\.[0-9A-Za-z\-_]+/,
  /GOCSPX-[0-9A-Za-z\-_]+/,
  /sk-[0-9A-Za-z]{20,}/,
  /-----BEGIN (?:RSA |EC )?PRIVATE KEY-----/,
];

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function validateWorkflowFile(spec) {
  const fullPath = path.join(WORKFLOWS_DIR, spec.file);
  const raw = fs.readFileSync(fullPath, 'utf8');
  for (const pat of SECRET_PATTERNS) {
    assert(!pat.test(raw), `${spec.file}: potential secret matched ${pat}`);
  }

  const wf = JSON.parse(raw);
  assert(wf.name === spec.name, `${spec.file}: expected name "${spec.name}", got "${wf.name}"`);
  assert(wf.active === false, `${spec.file}: active must be false on export`);
  assert(wf.settings?.timezone === 'Asia/Kolkata', `${spec.file}: timezone must be Asia/Kolkata`);
  assert(Array.isArray(wf.nodes) && wf.nodes.length === spec.requiredNodes.length, `${spec.file}: node count mismatch`);

  const byName = {};
  const ids = new Set();
  for (const node of wf.nodes) {
    assert(node.id && !ids.has(node.id), `${spec.file}: duplicate or missing node id ${node.id}`);
    ids.add(node.id);
    assert(node.name && !byName[node.name], `${spec.file}: duplicate or missing node name ${node.name}`);
    byName[node.name] = node;
    assert(typeof node.type === 'string' && node.type.startsWith('n8n-nodes-base.'), `${spec.file}: invalid type on ${node.name}`);
    assert(typeof node.typeVersion === 'number', `${spec.file}: missing typeVersion on ${node.name}`);
    assert(Array.isArray(node.position) && node.position.length === 2, `${spec.file}: invalid position on ${node.name}`);
    assert(!('credentials' in node), `${spec.file}: node "${node.name}" must not embed credentials`);
  }

  for (const req of spec.requiredNodes) {
    assert(Boolean(byName[req]), `${spec.file}: missing required node "${req}"`);
  }

  assert(wf.connections && typeof wf.connections === 'object', `${spec.file}: missing connections object`);
  for (const [src, obj] of Object.entries(wf.connections)) {
    assert(Boolean(byName[src]), `${spec.file}: connection source "${src}" not found`);
    assert(Array.isArray(obj.main), `${spec.file}: connection "${src}" missing main array`);
    for (const branch of obj.main) {
      assert(Array.isArray(branch) && branch.length > 0, `${spec.file}: empty branch on "${src}"`);
      for (const edge of branch) {
        assert(Boolean(byName[edge.node]), `${spec.file}: target "${edge.node}" not found`);
      }
    }
  }

  return { wf, byName };
}

function runCodeNode(jsCode, items, namedOutputs = {}) {
  const $input = {
    all: () => items,
    first: () => items[0],
  };
  const $ = (nodeName) => ({
    first: () => namedOutputs[nodeName]?.[0],
    item: namedOutputs[nodeName]?.[0],
    all: () => namedOutputs[nodeName] || [],
  });
  const fn = new Function('$input', '$', jsCode);
  return fn($input, $);
}

function testGm01(byName) {
  const gsNode = byName['Google Sheets - Append Member'];
  assert(gsNode.onError === 'continueErrorOutput', 'GM-01: Google Sheets node must have continueErrorOutput');
  assert(
    JSON.stringify(Object.keys(gsNode.parameters.columns.value)) === JSON.stringify(EXPECTED_MEMBERS_COLUMNS),
    'GM-01: Google Sheets 18-column mapping mismatch'
  );

  const td = JSON.parse(fs.readFileSync(TEST_DATA_PATH, 'utf8'));
  const valJs = byName['Code - Validate Registration'].parameters.jsCode;
  const genJs = byName['Code - Generate Member Record'].parameters.jsCode;

  for (const tc of td.valid_payloads) {
    const vOut = runCodeNode(valJs, [{ json: tc.form_submission }]);
    assert(vOut[0]?.json?.is_valid === true, `GM-01 ${tc.test_id}: expected valid`);
    const gOut = runCodeNode(genJs, vOut);
    const rec = gOut[0].json;
    assert(
      JSON.stringify(Object.keys(rec)) === JSON.stringify(EXPECTED_MEMBERS_COLUMNS),
      `GM-01 ${tc.test_id}: output keys mismatch`
    );
    const exp = tc.expected_record;
    if (exp.member_id_pattern) {
      assert(new RegExp(exp.member_id_pattern).test(rec.member_id), `GM-01 ${tc.test_id}: bad member_id ${rec.member_id}`);
    }
    if (exp.member_id_not_equals) {
      assert(rec.member_id !== exp.member_id_not_equals, `GM-01 ${tc.test_id}: accepted spoofed member_id`);
    }
    for (const [k, v] of Object.entries(exp)) {
      if (k.startsWith('member_id_')) continue;
      assert(rec[k] === v, `GM-01 ${tc.test_id}: ${k} expected ${v}, got ${rec[k]}`);
    }
  }

  for (const tc of td.invalid_payloads) {
    const vOut = runCodeNode(valJs, [{ json: tc.form_submission }]);
    assert(vOut[0]?.json?.is_valid === false, `GM-01 ${tc.test_id}: expected invalid`);
    if (tc.expected_error_substring) {
      assert(vOut[0].json.error_message.includes(tc.expected_error_substring), `GM-01 ${tc.test_id}: missing error substring`);
    }
  }
  console.log(`PASS GM-01: validated ${td.valid_payloads.length} valid + ${td.invalid_payloads.length} invalid payloads.`);
}

function formatIstOffsetDays(offsetDays) {
  const todayStr = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date());
  const [y, m, d] = todayStr.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d) + offsetDays * 86400000);
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth() + 1).padStart(2, '0')}-${String(dt.getUTCDate()).padStart(2, '0')}`;
}

function testGm02(byName) {
  const emailNode = byName['Email - Send Follow-Up Notification'];
  assert(emailNode.type === 'n8n-nodes-base.gmail', 'GM-02: Email node must use n8n-nodes-base.gmail');

  const code = byName['Code - Evaluate Follow-Up Cadence'].parameters.jsCode;
  const sampleMembers = [
    { json: { member_id: 'MEM-TEST0001', name: 'Aarav', phone: '919876543210', email: 'aarav@example.com', plan: 'Monthly', fee: 1500, start_date: formatIstOffsetDays(-27), expiry_date: formatIstOffsetDays(3), payment_status: 'Paid', last_reminder_date: '', member_status: 'Active', notes: '' } },
    { json: { member_id: 'MEM-TEST0002', name: 'Diya', phone: '919876543211', email: 'diya@example.com', plan: 'Quarterly', fee: 4000, start_date: formatIstOffsetDays(-3), expiry_date: formatIstOffsetDays(87), payment_status: 'Pending', last_reminder_date: '', member_status: 'Active', notes: '' } },
    { json: { member_id: 'MEM-TEST0003', name: 'Karan', phone: '919876543212', email: 'karan@example.com', plan: 'Monthly', fee: 1500, start_date: formatIstOffsetDays(-33), expiry_date: formatIstOffsetDays(-3), payment_status: 'Paid', last_reminder_date: '', member_status: 'Active', notes: '' } },
    { json: { member_id: 'MEM-TEST0004', name: 'SkipMe', phone: '919876543213', email: 'skip@example.com', plan: 'Monthly', fee: 1500, start_date: formatIstOffsetDays(-10), expiry_date: formatIstOffsetDays(0), payment_status: 'Paid', last_reminder_date: formatIstOffsetDays(0), member_status: 'Active', notes: '' } },
  ];
  const out = runCodeNode(code, sampleMembers);
  assert(out.length === 3, `GM-02: expected 3 follow-ups (1 skipped already-reminded), got ${out.length}`);
  assert(out[0].json.followup_type === 'EXPIRY_T3_URGENT', 'GM-02: expected EXPIRY_T3_URGENT');
  assert(
    out[0].json.email === 'aarav@example.com' &&
      Boolean(out[0].json.email_subject) &&
      out[0].json.email_html.includes('<div'),
    'GM-02: expected generated email subject & HTML body'
  );
  assert(out[1].json.followup_type === 'PAYMENT_PENDING_FOLLOWUP', 'GM-02: expected PAYMENT_PENDING_FOLLOWUP');
  assert(out[2].json.followup_type === 'WINBACK_EXPIRED_T3' && out[2].json.new_member_status === 'Expired', 'GM-02: expected WINBACK_EXPIRED_T3 + Expired');
  console.log('PASS GM-02: email follow-up cadence evaluation, HTML/subject rendering & skip-same-day guard verified.');
}

function testGm03(byName) {
  const parseJs = byName['Code - Parse & Validate Payment'].parameters.jsCode;
  const computeJs = byName['Code - Compute Renewal & Invoice'].parameters.jsCode;

  const rzpEvent = {
    body: {
      event: 'payment_link.paid',
      payload: { payment_link: { entity: { id: 'plink_999', reference_id: 'MEM-TEST0001', amount_paid: 150000 } } },
    },
  };
  const parsed = runCodeNode(parseJs, [{ json: rzpEvent }]);
  assert(parsed[0].json.member_id === 'MEM-TEST0001' && parsed[0].json.amount_paid === 1500, 'GM-03: Razorpay parse failed');

  const memberRow = {
    member_id: 'MEM-TEST0001',
    name: 'Aarav',
    phone: '919876543210',
    plan: 'Monthly',
    expiry_date: formatIstOffsetDays(5),
    payment_status: 'Paid',
    last_payment_date: formatIstOffsetDays(-25),
    notes: '',
  };
  const computed = runCodeNode(
    computeJs,
    [{ json: memberRow }],
    { 'Code - Parse & Validate Payment': parsed }
  );
  assert(computed[0].json.already_processed === false, 'GM-03: expected new payment');
  assert(computed[0].json.new_expiry_date === formatIstOffsetDays(35), 'GM-03: expected expiry extended by 30 days from remaining expiry');

  const dupRow = { ...memberRow, notes: 'Ref:plink_999' };
  const dupOut = runCodeNode(
    computeJs,
    [{ json: dupRow }],
    { 'Code - Parse & Validate Payment': parsed }
  );
  assert(dupOut[0].json.already_processed === true, 'GM-03: duplicate payment should be idempotent');
  console.log('PASS GM-03: Razorpay webhook parsing, renewal extension, and idempotency verified.');
}

function testGm04(byName) {
  const code = byName['Code - Compute Studio KPIs & Drop-Off List'].parameters.jsCode;
  const today = formatIstOffsetDays(0);
  const rows = [
    { json: { member_id: 'MEM-1', name: 'A', plan: 'Monthly', fee: 1500, start_date: today, expiry_date: formatIstOffsetDays(30), payment_status: 'Paid', last_payment_date: today, member_status: 'Active' } },
    { json: { member_id: 'MEM-2', name: 'B', plan: 'Quarterly', fee: 4000, start_date: formatIstOffsetDays(-5), expiry_date: formatIstOffsetDays(4), payment_status: 'Pending', last_payment_date: '', member_status: 'Active' } },
  ];
  const out = runCodeNode(code, rows);
  const kpi = out[0].json;
  assert(kpi.total_members === 2 && kpi.active_count === 2 && kpi.expiring_in_7_days_count === 1, 'GM-04: KPI counts mismatch');
  assert(kpi.pending_dues_count === 1 && kpi.pending_dues_amount === 4000 && kpi.collections_today_amount === 1500, 'GM-04: revenue/dues mismatch');
  console.log('PASS GM-04: daily owner digest KPI aggregation verified.');
}

function testGm05(byName) {
  const normJs = byName['Code - Normalize Inbound Agent Request'].parameters.jsCode;
  const prepJs = byName['Code - Prepare Sheets Audit & Reply'].parameters.jsCode;
  const httpNode = byName['HTTP Request - Execute Python AI Gym Agent'];
  assert(
    httpNode.type === 'n8n-nodes-base.httpRequest' &&
      String(httpNode.parameters?.url || '').includes('/api/v1/agent/chat'),
    'GM-05: HTTP Request node must target /api/v1/agent/chat'
  );

  // 1. Email inbound webhook shape -> member role
  const emailPayload = {
    body: {
      from: 'aarav@example.com',
      subject: 'Freeze Plan',
      text: 'Check my membership status and freeze balance',
    },
  };
  const emailNorm = runCodeNode(normJs, [{ json: emailPayload }]);
  assert(
    emailNorm[0].json.actor_role === 'member' &&
      emailNorm[0].json.actor_email === 'aarav@example.com' &&
      emailNorm[0].json.sync_to_n8n === false &&
      emailNorm[0].json.message.includes('Freeze Plan'),
    'GM-05: Email inbound normalization failed'
  );

  // 2. WhatsApp Cloud API backward-compat inbound message shape -> member role
  const waPayload = {
    body: {
      entry: [
        {
          changes: [
            {
              value: {
                messages: [
                  {
                    from: '919876543210',
                    text: { body: 'Check my membership status and freeze balance' },
                  },
                ],
              },
            },
          ],
        },
      ],
    },
  };
  const waNorm = runCodeNode(normJs, [{ json: waPayload }]);
  assert(
    waNorm[0].json.actor_role === 'member' &&
      waNorm[0].json.actor_phone === '919876543210' &&
      waNorm[0].json.sync_to_n8n === false &&
      waNorm[0].json.message.includes('Check my membership status'),
    'GM-05: WhatsApp inbound normalization failed'
  );

  // 2. Direct owner command shape -> owner role
  const ownerNorm = runCodeNode(normJs, [
    { json: { body: { message: 'Show financial summary and pending dues' } } },
  ]);
  assert(
    ownerNorm[0].json.actor_role === 'owner' &&
      ownerNorm[0].json.message === 'Show financial summary and pending dues' &&
      ownerNorm[0].json.sync_to_n8n === false,
    'GM-05: Owner command normalization failed'
  );

  // 3. Prepare Sheets Audit & Reply - success response
  const okPrepared = runCodeNode(
    prepJs,
    [
      {
        json: {
          run_id: 'AGT-123456',
          timestamp_ist: '2026-11-15 15:00:00 IST',
          intent: 'ACCOUNTS_CFO_AUDIT',
          reply: 'Net profit ₹12,500',
          tools_executed: [{ tool: 'get_accounts_summary' }],
          google_sheets_sync: { ok: true },
        },
      },
    ],
    { 'Code - Normalize Inbound Agent Request': ownerNorm }
  );
  assert(
    okPrepared[0].json.log_id === 'AGT-123456' &&
      okPrepared[0].json.status === 'COMPLETED' &&
      okPrepared[0].json.action === 'ACCOUNTS_CFO_AUDIT' &&
      okPrepared[0].json.workflow === 'GM-05',
    'GM-05: Prepare Sheets Audit success branch failed'
  );

  // 4. Prepare Sheets Audit & Reply - HTTP error fallback
  const errPrepared = runCodeNode(
    prepJs,
    [{ json: { error: { message: 'ECONNREFUSED' } } }],
    { 'Code - Normalize Inbound Agent Request': ownerNorm }
  );
  assert(
    errPrepared[0].json.status === 'FAILED' &&
      errPrepared[0].json.action === 'AGENT_ERROR' &&
      errPrepared[0].json.details.includes('ECONNREFUSED'),
    'GM-05: Prepare Sheets Audit error fallback failed'
  );

  console.log('PASS GM-05: WhatsApp/owner normalization, FastAPI bridge, and Sheets audit formatting verified.');
}

function main() {
  const loaded = {};
  for (const spec of WORKFLOW_SPECS) {
    loaded[spec.file] = validateWorkflowFile(spec);
  }
  testGm01(loaded['GM-01-member-registration.json'].byName);
  testGm02(loaded['GM-02-expiry-and-followup-cadence.json'].byName);
  testGm03(loaded['GM-03-payment-and-renewal-webhook.json'].byName);
  testGm04(loaded['GM-04-attendance-streak-and-owner-digest.json'].byName);
  testGm05(loaded['GM-05-ai-gym-manager-agent.json'].byName);
  console.log('ALL 5 WORKFLOWS (GM-01 TO GM-05) PASSED STRUCTURAL & JS RUNTIME VALIDATION.');
}

main();
