import io
import json
import os

import pytest

from kubepulse_mcp import server as server_module
from kubepulse_mcp.server import KubepulseNotFoundError, Server, find_binary, serve

FAKE_REPORT = {
    "cluster": "stub-cluster",
    "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0},
    "findings": [
        {
            "severity": "high",
            "detector": "crashloop",
            "object": "Pod shop/api-7849c6f5d9-xk2lp",
            "summary": "container api is in CrashLoopBackOff after 14 restarts",
            "evidence": "back-off restarting failed container",
            "nextStep": "Read the previous container logs.",
        }
    ],
}

FAKE_DIFF = {
    "beforeCluster": "stub-cluster",
    "afterCluster": "stub-cluster",
    "summary": {"new": 1, "resolved": 0, "unchanged": 0},
    "new": FAKE_REPORT["findings"],
    "resolved": [],
    "unchanged": [],
}

EXPECTED_DETECTORS = [
    "crashloop",
    "oomkilled",
    "image-pull",
    "unschedulable",
    "rollout-stuck",
    "no-resource-limits",
    "memory-close-to-limit",
    "node-pressure",
]

SNAPSHOT = {"cluster": "stub-cluster", "pods": [], "deployments": [], "nodes": []}


def write_stub(tmp_path):
    """A shell script that stands in for the kubepulse binary."""
    args_log = tmp_path / "args.log"
    content_log = tmp_path / "content.log"
    script = tmp_path / "kubepulse"
    body = "\n".join(
        [
            "#!/bin/sh",
            "printf '%s\\n' \"$@\" >> " + str(args_log),
            "for last do :; done",
            'cat "$last" >> ' + str(content_log) + " 2>/dev/null",
            'if [ "$1" = "diagnose" ]; then',
            "  printf '%s\\n' '" + json.dumps(FAKE_REPORT) + "'",
            "  exit 0",
            "fi",
            'if [ "$1" = "diff" ]; then',
            "  printf '%s\\n' '" + json.dumps(FAKE_DIFF) + "'",
            "  exit 0",
            "fi",
            'echo "kubepulse: unexpected arguments" >&2',
            "exit 2",
            "",
        ]
    )
    script.write_text(body)
    script.chmod(0o755)
    return str(script), args_log, content_log


def write_failing_stub(tmp_path):
    script = tmp_path / "kubepulse"
    script.write_text(
        "#!/bin/sh\n"
        'echo "kubepulse: snapshot file is not valid JSON" >&2\n'
        "exit 2\n"
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture()
def stub(tmp_path):
    binary, args_log, content_log = write_stub(tmp_path)
    return Server(binary), args_log, content_log


def request(server, method, params=None, req_id=1):
    message = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def call_tool(server, name, arguments=None, req_id=1):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return request(server, "tools/call", params, req_id=req_id)


def first_text_payload(response):
    result = response["result"]
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def test_initialize_handshake(stub):
    server, _, _ = stub
    response = request(
        server,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.0.1"},
        },
    )
    result = response["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"] == {"name": "kubepulse-mcp", "version": "0.1.0"}
    assert "tools" in result["capabilities"]


def test_ping(stub):
    server, _, _ = stub
    response = request(server, "ping", req_id=7)
    assert response == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_tools_list(stub):
    server, _, _ = stub
    response = request(server, "tools/list")
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "diagnose_snapshot",
        "diff_snapshots",
        "list_detectors",
    ]
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]
    diagnose = tools[0]
    assert diagnose["inputSchema"]["required"] == ["snapshot"]
    assert "snapshot" in diagnose["inputSchema"]["properties"]
    diff = tools[1]
    assert diff["inputSchema"]["required"] == ["before", "after"]


def test_unknown_method_returns_jsonrpc_error(stub):
    server, _, _ = stub
    response = request(server, "resources/list", req_id=9)
    assert response["id"] == 9
    assert response["error"]["code"] == -32601


def test_notification_gets_no_response(stub):
    server, _, _ = stub
    message = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert server.handle(message) is None


def test_list_detectors(stub):
    server, _, _ = stub
    payload = first_text_payload(call_tool(server, "list_detectors", {}))
    detectors = payload["detectors"]
    assert [d["name"] for d in detectors] == EXPECTED_DETECTORS
    for detector in detectors:
        assert detector["severity"] in ("critical", "high", "medium", "low")
        assert detector["description"]


def test_diagnose_snapshot_calls_binary_with_right_arguments(stub):
    server, args_log, content_log = stub
    response = call_tool(
        server,
        "diagnose_snapshot",
        {"snapshot": SNAPSHOT, "fail_on": "high"},
    )
    assert first_text_payload(response) == FAKE_REPORT
    args = args_log.read_text().splitlines()
    assert args[:5] == ["diagnose", "--format", "json", "--fail-on", "high"]
    assert args[5].endswith("snapshot.json")
    assert json.loads(content_log.read_text()) == SNAPSHOT


def test_diagnose_snapshot_without_fail_on(stub):
    server, args_log, _ = stub
    response = call_tool(server, "diagnose_snapshot", {"snapshot": SNAPSHOT})
    assert first_text_payload(response) == FAKE_REPORT
    args = args_log.read_text().splitlines()
    assert args[:3] == ["diagnose", "--format", "json"]
    assert "--fail-on" not in args


def test_diff_snapshots_calls_binary_with_right_arguments(stub):
    server, args_log, _ = stub
    before = dict(SNAPSHOT, cluster="before-cluster")
    after = dict(SNAPSHOT, cluster="after-cluster")
    response = call_tool(
        server, "diff_snapshots", {"before": before, "after": after}
    )
    assert first_text_payload(response) == FAKE_DIFF
    args = args_log.read_text().splitlines()
    assert args[:3] == ["diff", "--format", "json"]
    assert args[3].endswith("before.json")
    assert args[4].endswith("after.json")


def test_binary_error_surfaces_as_tool_error(tmp_path):
    server = Server(write_failing_stub(tmp_path))
    response = call_tool(server, "diagnose_snapshot", {"snapshot": SNAPSHOT})
    result = response["result"]
    assert result["isError"] is True
    assert "not valid JSON" in result["content"][0]["text"]
    ping = request(server, "ping", req_id=2)
    assert ping["result"] == {}


def test_bad_snapshot_argument_is_tool_error(stub):
    server, _, _ = stub
    response = call_tool(server, "diagnose_snapshot", {"snapshot": "not an object"})
    result = response["result"]
    assert result["isError"] is True
    assert "snapshot" in result["content"][0]["text"]


def test_bad_fail_on_is_tool_error(stub):
    server, _, _ = stub
    response = call_tool(
        server,
        "diagnose_snapshot",
        {"snapshot": SNAPSHOT, "fail_on": "whenever"},
    )
    assert response["result"]["isError"] is True


def test_missing_binary_at_call_time_is_tool_error():
    server = Server("/nonexistent/path/kubepulse")
    response = call_tool(server, "diagnose_snapshot", {"snapshot": SNAPSHOT})
    result = response["result"]
    assert result["isError"] is True
    assert "not found" in result["content"][0]["text"]


def test_unknown_tool_is_protocol_error(stub):
    server, _, _ = stub
    response = call_tool(server, "restart_pod", {})
    assert response["error"]["code"] == -32602


def test_find_binary_prefers_env_override(tmp_path, monkeypatch):
    binary, _, _ = write_stub(tmp_path)
    monkeypatch.setenv("KUBEPULSE_BIN", binary)
    assert find_binary() == binary


def test_missing_binary_at_startup_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBEPULSE_BIN", raising=False)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))
    with pytest.raises(KubepulseNotFoundError) as excinfo:
        find_binary()
    assert "KUBEPULSE_BIN" in str(excinfo.value)
    assert server_module.main() == 1


def test_serve_round_trip_with_stub(tmp_path):
    binary, _, _ = write_stub(tmp_path)
    lines = [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            }
        ),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_detectors", "arguments": {}},
            }
        ),
    ]
    stdout = io.StringIO()
    serve(io.StringIO("\n".join(lines) + "\n"), stdout, binary)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert responses[0]["result"]["serverInfo"]["name"] == "kubepulse-mcp"
    payload = json.loads(responses[2]["result"]["content"][0]["text"])
    assert [d["name"] for d in payload["detectors"]] == EXPECTED_DETECTORS


def test_serve_reports_parse_errors():
    stdout = io.StringIO()
    serve(io.StringIO("this is not json\n"), stdout, "/bin/true")
    response = json.loads(stdout.getvalue().strip())
    assert response["id"] is None
    assert response["error"]["code"] == -32700
