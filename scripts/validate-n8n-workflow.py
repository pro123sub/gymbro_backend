"""Validation script for the GM-01 n8n Gym Member Registration workflow."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / "workflows" / "GM-01-member-registration.json"
TEST_DATA_PATH = REPO_ROOT / "workflows" / "GM-01-member-registration-test-data.json"

EXPECTED_WORKFLOW_NAME = "GM-01 - Gym Member Registration"
EXPECTED_FORM_TITLE = "Crush Lifestyle Fitness Studio - Member Registration"
EXPECTED_FORM_DESCRIPTION = "Register a new gym member."

EXPECTED_FORM_FIELDS = [
    ("Full Name", "text", True, None),
    ("Phone Number", "text", True, None),
    ("Email", "email", False, None),
    ("Gender", "dropdown", False, ["Male", "Female", "Other", "Prefer not to say"]),
    ("Date of Birth", "date", False, None),
    ("Address", "text", False, None),
    ("Emergency Contact Name", "text", False, None),
    ("Emergency Contact Phone", "text", False, None),
    ("Membership Plan", "dropdown", True, ["Monthly", "Quarterly", "Half-Yearly", "Yearly"]),
    ("Membership Fee", "number", True, None),
    ("Start Date", "date", True, None),
    ("Payment Status", "dropdown", True, ["Paid", "Pending"]),
]

EXPECTED_MEMBERS_COLUMNS = [
    "member_id",
    "name",
    "phone",
    "email",
    "gender",
    "date_of_birth",
    "address",
    "emergency_contact_name",
    "emergency_contact_phone",
    "plan",
    "fee",
    "start_date",
    "expiry_date",
    "payment_status",
    "last_payment_date",
    "last_reminder_date",
    "member_status",
    "notes",
]

REQUIRED_NODE_NAMES = [
    "n8n Form Trigger",
    "Code - Validate Registration",
    "IF - Validation Passed",
    "Code - Generate Member Record",
    "Google Sheets - Append Member",
    "Respond to Registration",
    "Respond - Validation Error",
    "Respond - Storage Error",
]

SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"ya29\.[0-9A-Za-z\-_]+"),
    re.compile(r"GOCSPX-[0-9A-Za-z\-_]+"),
    re.compile(r"sk-[0-9A-Za-z]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
]


class ValidationError(Exception):
    """Raised when one or more workflow checks fail."""


def assert_true(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_workflow_structure(workflow: dict[str, Any], raw_text: str) -> list[str]:
    errors: list[str] = []

    assert_true(
        workflow.get("name") == EXPECTED_WORKFLOW_NAME,
        f"Workflow name must be {EXPECTED_WORKFLOW_NAME!r}, got {workflow.get('name')!r}.",
        errors,
    )
    assert_true(
        workflow.get("active") is False,
        "Workflow 'active' should be false on export/import before credentials are configured.",
        errors,
    )

    settings = workflow.get("settings") or {}
    assert_true(
        settings.get("timezone") == "Asia/Kolkata",
        f"Workflow settings.timezone must be 'Asia/Kolkata', got {settings.get('timezone')!r}.",
        errors,
    )
    assert_true(
        settings.get("executionOrder") == "v1",
        f"Workflow settings.executionOrder must be 'v1', got {settings.get('executionOrder')!r}.",
        errors,
    )

    for pattern in SECRET_PATTERNS:
        assert_true(
            not pattern.search(raw_text),
            f"Potential secret detected matching pattern {pattern.pattern}.",
            errors,
        )

    nodes = workflow.get("nodes")
    assert_true(isinstance(nodes, list) and len(nodes) == 8, "Expected exactly 8 workflow nodes.", errors)
    if not isinstance(nodes, list):
        return errors

    node_by_name: dict[str, dict[str, Any]] = {}
    node_ids: set[str] = set()

    for idx, node in enumerate(nodes):
        name = node.get("name")
        node_id = node.get("id")
        assert_true(bool(name) and isinstance(name, str), f"Node[{idx}] missing valid 'name'.", errors)
        assert_true(bool(node_id) and isinstance(node_id, str), f"Node {name!r} missing valid 'id'.", errors)
        assert_true(node_id not in node_ids, f"Duplicate node id {node_id!r}.", errors)
        assert_true(name not in node_by_name, f"Duplicate node name {name!r}.", errors)
        assert_true(isinstance(node.get("type"), str) and node["type"].startswith("n8n-nodes-base."), f"Node {name!r} has invalid type {node.get('type')!r}.", errors)
        assert_true(isinstance(node.get("typeVersion"), (int, float)), f"Node {name!r} missing numeric typeVersion.", errors)
        pos = node.get("position")
        assert_true(isinstance(pos, list) and len(pos) == 2 and all(isinstance(v, (int, float)) for v in pos), f"Node {name!r} has invalid position {pos!r}.", errors)
        assert_true(isinstance(node.get("parameters"), dict), f"Node {name!r} missing parameters dict.", errors)
        assert_true("credentials" not in node, f"Node {name!r} must not embed credentials.", errors)
        if isinstance(name, str):
            node_by_name[name] = node
        if isinstance(node_id, str):
            node_ids.add(node_id)

    for req_name in REQUIRED_NODE_NAMES:
        assert_true(req_name in node_by_name, f"Required node {req_name!r} is missing.", errors)

    # Validate Form Trigger
    form_node = node_by_name.get("n8n Form Trigger")
    if form_node:
        assert_true(form_node.get("type") == "n8n-nodes-base.formTrigger", "n8n Form Trigger must have type n8n-nodes-base.formTrigger.", errors)
        assert_true(bool(form_node.get("webhookId")), "n8n Form Trigger must have a non-empty webhookId.", errors)
        fp = form_node.get("parameters", {})
        assert_true(fp.get("formTitle") == EXPECTED_FORM_TITLE, f"Unexpected formTitle: {fp.get('formTitle')!r}.", errors)
        assert_true(fp.get("formDescription") == EXPECTED_FORM_DESCRIPTION, f"Unexpected formDescription: {fp.get('formDescription')!r}.", errors)
        assert_true(fp.get("responseMode") == "lastNode", f"Form Trigger responseMode must be 'lastNode', got {fp.get('responseMode')!r}.", errors)
        fields = (fp.get("formFields") or {}).get("values") or []
        assert_true(len(fields) == len(EXPECTED_FORM_FIELDS), f"Expected {len(EXPECTED_FORM_FIELDS)} form fields, got {len(fields)}.", errors)
        for field_obj, (exp_label, exp_type, exp_req, exp_opts) in zip(fields, EXPECTED_FORM_FIELDS):
            assert_true(field_obj.get("fieldLabel") == exp_label, f"Expected fieldLabel {exp_label!r}, got {field_obj.get('fieldLabel')!r}.", errors)
            assert_true(field_obj.get("fieldType") == exp_type, f"Field {exp_label!r} expected type {exp_type!r}, got {field_obj.get('fieldType')!r}.", errors)
            assert_true(field_obj.get("requiredField") is exp_req, f"Field {exp_label!r} requiredField must be {exp_req}.", errors)
            if exp_opts is not None:
                actual_opts = [o.get("option") for o in (field_obj.get("fieldOptions") or {}).get("values") or []]
                assert_true(actual_opts == exp_opts, f"Field {exp_label!r} options mismatch: {actual_opts!r} != {exp_opts!r}.", errors)

    # Validate Code nodes
    for code_name in ("Code - Validate Registration", "Code - Generate Member Record"):
        code_node = node_by_name.get(code_name)
        if code_node:
            assert_true(code_node.get("type") == "n8n-nodes-base.code", f"{code_name} must have type n8n-nodes-base.code.", errors)
            cp = code_node.get("parameters", {})
            assert_true(cp.get("mode") == "runOnceForAllItems", f"{code_name} mode must be 'runOnceForAllItems'.", errors)
            assert_true(isinstance(cp.get("jsCode"), str) and len(cp["jsCode"].strip()) > 50, f"{code_name} jsCode is empty or too short.", errors)

    # Validate IF node
    if_node = node_by_name.get("IF - Validation Passed")
    if if_node:
        assert_true(if_node.get("type") == "n8n-nodes-base.if", "IF - Validation Passed must have type n8n-nodes-base.if.", errors)
        conds = ((if_node.get("parameters") or {}).get("conditions") or {}).get("conditions") or []
        assert_true(len(conds) == 1 and conds[0].get("leftValue") == "={{ $json.is_valid }}", "IF node must inspect $json.is_valid.", errors)

    # Validate Google Sheets node
    gs_node = node_by_name.get("Google Sheets - Append Member")
    if gs_node:
        assert_true(gs_node.get("type") == "n8n-nodes-base.googleSheets", "Google Sheets node must have type n8n-nodes-base.googleSheets.", errors)
        assert_true(gs_node.get("onError") == "continueErrorOutput", "Google Sheets node must set onError='continueErrorOutput'.", errors)
        gp = gs_node.get("parameters", {})
        assert_true(gp.get("operation") == "append", f"Google Sheets operation must be 'append', got {gp.get('operation')!r}.", errors)
        doc_val = (gp.get("documentId") or {}).get("value")
        assert_true(doc_val == "REPLACE_WITH_GOOGLE_SHEET_ID", f"Google Sheets documentId must be placeholder 'REPLACE_WITH_GOOGLE_SHEET_ID', got {doc_val!r}.", errors)
        sheet_val = (gp.get("sheetName") or {}).get("value")
        assert_true(sheet_val == "Members", f"Google Sheets sheetName must be 'Members', got {sheet_val!r}.", errors)
        cols = gp.get("columns") or {}
        assert_true(cols.get("mappingMode") == "defineBelow", "Google Sheets columns.mappingMode must be 'defineBelow'.", errors)
        col_values = list((cols.get("value") or {}).keys())
        assert_true(col_values == EXPECTED_MEMBERS_COLUMNS, f"Google Sheets columns.value keys mismatch:\n  actual:   {col_values}\n  expected: {EXPECTED_MEMBERS_COLUMNS}", errors)
        schema_ids = [s.get("id") for s in (cols.get("schema") or [])]
        assert_true(schema_ids == EXPECTED_MEMBERS_COLUMNS, f"Google Sheets columns.schema IDs mismatch.", errors)

    # Validate companion workflow JSON files (GM-02 to GM-05) if present
    for companion_name in (
        "GM-02-expiry-and-followup-cadence.json",
        "GM-03-payment-and-renewal-webhook.json",
        "GM-04-attendance-streak-and-owner-digest.json",
        "GM-05-ai-gym-manager-agent.json",
    ):
        companion_path = REPO_ROOT / "workflows" / companion_name
        assert_true(companion_path.exists(), f"Companion workflow {companion_name} is missing.", errors)
        if companion_path.exists():
            c_raw = companion_path.read_text(encoding="utf-8")
            c_wf = json.loads(c_raw)
            assert_true(
                (c_wf.get("settings") or {}).get("timezone") == "Asia/Kolkata",
                f"{companion_name} timezone must be Asia/Kolkata.",
                errors,
            )
            for node in c_wf.get("nodes") or []:
                assert_true("credentials" not in node, f"{companion_name}: node {node.get('name')} must not embed credentials.", errors)

    # Validate response Form Ending nodes
    for resp_name in ("Respond to Registration", "Respond - Validation Error", "Respond - Storage Error"):
        resp_node = node_by_name.get(resp_name)
        if resp_node:
            assert_true(resp_node.get("type") == "n8n-nodes-base.form", f"{resp_name} must have type n8n-nodes-base.form.", errors)
            rp = resp_node.get("parameters", {})
            assert_true(rp.get("operation") == "completion", f"{resp_name} operation must be 'completion'.", errors)
            assert_true(bool(rp.get("completionTitle")), f"{resp_name} missing completionTitle.", errors)
            assert_true(bool(rp.get("completionMessage")), f"{resp_name} missing completionMessage.", errors)

    # Validate Connections
    connections = workflow.get("connections")
    assert_true(isinstance(connections, dict), "Workflow missing 'connections' object.", errors)
    if isinstance(connections, dict):
        for src_name, conn_obj in connections.items():
            assert_true(src_name in node_by_name, f"Connection source {src_name!r} not found in nodes.", errors)
            main_branches = conn_obj.get("main")
            assert_true(isinstance(main_branches, list), f"Connection {src_name!r} missing 'main' branch list.", errors)
            if isinstance(main_branches, list):
                for b_idx, branch in enumerate(main_branches):
                    assert_true(isinstance(branch, list) and len(branch) > 0, f"Connection {src_name!r} branch {b_idx} is empty.", errors)
                    for target in branch:
                        t_node = target.get("node")
                        assert_true(t_node in node_by_name, f"Connection target {t_node!r} from {src_name!r} does not exist.", errors)

        def branch_targets(src: str, branch_idx: int) -> list[str]:
            branches = (connections.get(src) or {}).get("main") or []
            if branch_idx >= len(branches):
                return []
            return [item.get("node") for item in branches[branch_idx]]

        assert_true(branch_targets("n8n Form Trigger", 0) == ["Code - Validate Registration"], "Form Trigger must connect to Code - Validate Registration.", errors)
        assert_true(branch_targets("Code - Validate Registration", 0) == ["IF - Validation Passed"], "Validate Registration must connect to IF - Validation Passed.", errors)
        assert_true(branch_targets("IF - Validation Passed", 0) == ["Code - Generate Member Record"], "IF true branch must connect to Code - Generate Member Record.", errors)
        assert_true(branch_targets("IF - Validation Passed", 1) == ["Respond - Validation Error"], "IF false branch must connect to Respond - Validation Error.", errors)
        assert_true(branch_targets("Code - Generate Member Record", 0) == ["Google Sheets - Append Member"], "Generate Member Record must connect to Google Sheets - Append Member.", errors)
        assert_true(branch_targets("Google Sheets - Append Member", 0) == ["Respond to Registration"], "Google Sheets success output must connect to Respond to Registration.", errors)
        assert_true(branch_targets("Google Sheets - Append Member", 1) == ["Respond - Storage Error"], "Google Sheets error output must connect to Respond - Storage Error.", errors)

    return errors


def simulate_js_with_node(workflow: dict[str, Any], test_data: dict[str, Any]) -> tuple[bool, list[str]]:
    node_bin = shutil.which("node")
    if not node_bin:
        return False, ["Node.js binary not found on PATH; skipped JS Code-node runtime simulation."]

    node_map = {n["name"]: n for n in workflow["nodes"]}
    validate_js = node_map["Code - Validate Registration"]["parameters"]["jsCode"]
    generate_js = node_map["Code - Generate Member Record"]["parameters"]["jsCode"]

    harness = """
const fs = require('fs');
const testData = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const validateCode = process.argv[2];
const generateCode = process.argv[3];

function runCodeNode(jsCode, items) {
  const fn = new Function('$input', jsCode);
  const $input = { all: () => items };
  return fn($input);
}

const failures = [];

for (const tc of testData.valid_payloads) {
  const valOut = runCodeNode(validateCode, [{ json: tc.form_submission }]);
  if (!valOut || valOut.length !== 1 || !valOut[0].json.is_valid) {
    failures.push(`${tc.test_id}: expected valid=true, got errors=${JSON.stringify(valOut?.[0]?.json?.validation_errors)}`);
    continue;
  }
  const genOut = runCodeNode(generateCode, valOut);
  const rec = genOut[0].json;
  const cols = Object.keys(rec);
  const expCols = ${JSON.stringify(EXPECTED_MEMBERS_COLUMNS)};
  if (JSON.stringify(cols) !== JSON.stringify(expCols)) {
    failures.push(`${tc.test_id}: column keys mismatch: ${cols.join(',')}`);
  }
  const exp = tc.expected_record;
  if (exp.member_id_pattern && !(new RegExp(exp.member_id_pattern).test(rec.member_id))) {
    failures.push(`${tc.test_id}: invalid member_id ${rec.member_id}`);
  }
  if (exp.member_id_not_equals && rec.member_id === exp.member_id_not_equals) {
    failures.push(`${tc.test_id}: user-supplied member_id was not rejected`);
  }
  for (const [k, v] of Object.entries(exp)) {
    if (k.startsWith('member_id_')) continue;
    if (rec[k] !== v) {
      failures.push(`${tc.test_id}: expected ${k}=${JSON.stringify(v)}, got ${JSON.stringify(rec[k])}`);
    }
  }
}

for (const tc of testData.invalid_payloads) {
  const valOut = runCodeNode(validateCode, [{ json: tc.form_submission }]);
  const res = valOut[0].json;
  if (res.is_valid !== false) {
    failures.push(`${tc.test_id}: expected is_valid=false, got true`);
    continue;
  }
  if (tc.expected_error_substring && !res.error_message.includes(tc.expected_error_substring)) {
    failures.push(`${tc.test_id}: error_message ${JSON.stringify(res.error_message)} missing ${JSON.stringify(tc.expected_error_substring)}`);
  }
}

if (failures.length > 0) {
  console.error(failures.join('\\n'));
  process.exit(1);
}
console.log(`JS simulation passed: ${testData.valid_payloads.length} valid + ${testData.invalid_payloads.length} invalid test cases.`);
""".replace("${JSON.stringify(EXPECTED_MEMBERS_COLUMNS)}", json.dumps(EXPECTED_MEMBERS_COLUMNS))

    proc = subprocess.run(
        [node_bin, "-e", harness, str(TEST_DATA_PATH), validate_js, generate_js],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return True, [f"JS Code-node simulation failed:\n{proc.stderr.strip() or proc.stdout.strip()}"]
    print(proc.stdout.strip())
    return True, []


def main() -> int:
    if not WORKFLOW_PATH.exists():
        print(f"ERROR: Workflow file not found: {WORKFLOW_PATH}", file=sys.stderr)
        return 1
    if not TEST_DATA_PATH.exists():
        print(f"ERROR: Test data file not found: {TEST_DATA_PATH}", file=sys.stderr)
        return 1

    raw_workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = json.loads(raw_workflow)
    test_data = json.loads(TEST_DATA_PATH.read_text(encoding="utf-8"))

    errors = validate_workflow_structure(workflow, raw_workflow)
    if not test_data.get("valid_payloads") or not test_data.get("invalid_payloads"):
        errors.append("Test data file must include non-empty valid_payloads and invalid_payloads arrays.")

    if errors:
        print("VALIDATION FAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(f"Structural validation passed: {WORKFLOW_PATH.name} ({len(workflow['nodes'])} nodes, 18 Members columns, 2 error branches).")

    ran_js, js_msgs = simulate_js_with_node(workflow, test_data)
    if ran_js and js_msgs:
        print("JS SIMULATION FAILED:")
        for msg in js_msgs:
            print(f"  - {msg}")
        return 1
    if not ran_js:
        print(f"NOTE: {js_msgs[0]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
