"""Static checks for n8n workflow JSON files (n8n itself is not needed).

Checks: valid JSON, unique node names, connections and $('Node') references point to real nodes,
every node reachable from the trigger, error outputs wired, HTTP URLs match real routes in the
ai-worker / mock API, and all JavaScript (Code nodes + {{ expressions }}) compiles (needs `node`).

Run from repo root:  pytest automation/n8n -q
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((ROOT / "automation" / "n8n" / "workflows").glob("*.json"))
ROUTE_FILES = {
    "AI_WORKER_URL": ROOT / "automation" / "ai" / "worker" / "app.py",
    "MEDCHAIN_API_URL": ROOT / "automation" / "mocks" / "mock_api" / "app.py",
    "SOLANA_CLIENT_URL": ROOT / "automation" / "mocks" / "mock_api" / "app.py",
}
SOLANA_PREFIX = "/solana"  # mock serves the Solana client under /solana
NODE = shutil.which("node")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def routes(py_file: Path) -> set[tuple[str, str]]:
    text = py_file.read_text(encoding="utf-8")
    return {(m[0].upper(), m[1]) for m in re.findall(r'@app\.(get|post|put|patch|delete)\("([^"]+)"', text)}


def route_matches(path: str, templates: set[tuple[str, str]], method: str) -> bool:
    for meth, tpl in templates:
        rx = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(tpl)) + "$"
        if meth == method and re.match(rx, path):
            return True
    return False


def expressions(value):
    """Yield the JS inside every {{ ... }} of an n8n parameter value (recursively)."""
    if isinstance(value, str) and value.startswith("="):
        yield from re.findall(r"\{\{(.*?)\}\}", value, flags=re.S)
    elif isinstance(value, dict):
        for v in value.values():
            yield from expressions(v)
    elif isinstance(value, list):
        for v in value:
            yield from expressions(v)


def js_compiles(src: str) -> tuple[bool, str]:
    r = subprocess.run([NODE, "--check", "-"], input=src, capture_output=True, text=True, timeout=30)
    return r.returncode == 0, r.stderr


@pytest.fixture(params=WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def wf(request):
    return load(request.param)


def test_there_are_workflows():
    assert WORKFLOWS, "no workflow JSON files found"


def test_names_unique_and_connections_valid(wf):
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    for src, outs in wf["connections"].items():
        assert src in names, f"connection from unknown node {src}"
        for branch in outs["main"]:
            for c in branch:
                assert c["node"] in names, f"{src} -> unknown {c['node']}"


def test_node_references_exist(wf):
    names = {n["name"] for n in wf["nodes"]}
    text = json.dumps(wf, ensure_ascii=False)
    for ref in re.findall(r"\$\('([^']+)'\)", text.replace('\\"', '"')):
        assert ref in names, f"$('{ref}') refers to a node that does not exist"


def test_every_node_reachable_from_trigger(wf):
    triggers = [n["name"] for n in wf["nodes"] if n["type"].endswith(("webhook", "Trigger", "whatsAppTrigger"))]
    seen, todo = set(), list(triggers)
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for branch in wf["connections"].get(cur, {}).get("main", []):
            todo += [c["node"] for c in branch]
    unreachable = {n["name"] for n in wf["nodes"] if n["type"] != "n8n-nodes-base.stickyNote"} - seen
    assert not unreachable, f"unreachable nodes: {unreachable}"


def test_error_outputs_are_wired(wf):
    for n in wf["nodes"]:
        if n.get("onError") == "continueErrorOutput":
            outs = wf["connections"].get(n["name"], {}).get("main", [])
            assert len(outs) >= 2 and outs[1], f"{n['name']} has an error output that goes nowhere"


def test_http_nodes_send_auth_and_hit_real_routes(wf):
    for n in wf["nodes"]:
        if n["type"] != "n8n-nodes-base.httpRequest":
            continue
        p = n["parameters"]
        url = p["url"]
        env = re.search(r"\$env\.([A-Z_]+)", url)
        if not env or env.group(1) not in ROUTE_FILES:
            continue  # external API (e.g. WhatsApp Graph API)
        assert "INTERNAL_SERVICE_TOKEN" in json.dumps(p.get("headerParameters", {})), f"{n['name']} sends no token"
        path = re.sub(r"\{\{.*?\}\}", "X", url.split("}}", 1)[1])  # drop the base URL, replace expressions
        path = path.split("?")[0]
        if env.group(1) == "SOLANA_CLIENT_URL":
            path = SOLANA_PREFIX + path
        assert route_matches(path, routes(ROUTE_FILES[env.group(1)]), p["method"]), \
            f"{n['name']}: {p['method']} {path} is not a route in {ROUTE_FILES[env.group(1)].name}"


@pytest.mark.skipif(NODE is None, reason="node.js not installed")
def test_code_nodes_compile(wf):
    for n in wf["nodes"]:
        if n["type"] == "n8n-nodes-base.code":
            ok, err = js_compiles("(async function () {\n" + n["parameters"]["jsCode"] + "\n});")
            assert ok, f"Code node '{n['name']}' has a JS syntax error:\n{err}"


@pytest.mark.skipif(NODE is None, reason="node.js not installed")
def test_expressions_compile(wf):
    for n in wf["nodes"]:
        for expr in expressions(n["parameters"]):
            ok, err = js_compiles("(function () { return (" + expr + "); });")
            assert ok, f"Expression in '{n['name']}' does not compile: {expr}\n{err}"
