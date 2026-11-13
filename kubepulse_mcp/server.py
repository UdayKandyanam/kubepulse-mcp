"""An MCP server that exposes the KubePulse CLI as tools.

The server speaks the Model Context Protocol over stdio: newline
delimited JSON-RPC 2.0 messages in, newline delimited responses out.
It is a thin boundary. All of the diagnosis work happens in the
kubepulse binary, which this server runs as a subprocess.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "kubepulse-mcp", "version": "0.1.0"}
SEVERITIES = ("critical", "high", "medium", "low")
SUBPROCESS_TIMEOUT_SECONDS = 30

DETECTORS = [
    {
        "name": "crashloop",
        "severity": "high",
        "description": "A container waiting with reason CrashLoopBackOff.",
    },
    {
        "name": "oomkilled",
        "severity": "high",
        "description": "A container whose last terminated state has reason OOMKilled.",
    },
    {
        "name": "image-pull",
        "severity": "high",
        "description": "A container waiting with reason ImagePullBackOff or ErrImagePull.",
    },
    {
        "name": "unschedulable",
        "severity": "medium",
        "description": "A Pending pod with a FailedScheduling event, or a PodScheduled condition set to False.",
    },
    {
        "name": "rollout-stuck",
        "severity": "high",
        "description": "Updated replicas behind the desired count and a Progressing condition set to False.",
    },
    {
        "name": "no-resource-limits",
        "severity": "low",
        "description": "A pod with a container missing a CPU or memory limit.",
    },
    {
        "name": "memory-close-to-limit",
        "severity": "medium",
        "description": "Memory usage at or above 90 percent of the container limit, from the snapshot metrics fields.",
    },
    {
        "name": "node-pressure",
        "severity": "critical",
        "description": "A node reporting MemoryPressure or DiskPressure.",
    },
]

SNAPSHOT_SCHEMA = {
    "type": "object",
    "description": (
        "A KubePulse snapshot: one JSON object with cluster, capturedAt, "
        "pods, deployments, nodes, and events, in the shape the KubePulse "
        "README documents."
    ),
}

FAIL_ON_SCHEMA = {
    "type": "string",
    "enum": list(SEVERITIES),
    "description": (
        "Optional severity gate. The report is still returned in full; "
        "the result says whether a finding at or above this severity "
        "tripped the gate."
    ),
}

TOOLS = [
    {
        "name": "diagnose_snapshot",
        "description": (
            "Run the KubePulse detectors over one cluster snapshot and "
            "return the diagnosis report as JSON. The report groups "
            "findings by severity and gives the evidence and a suggested "
            "next step for each one. Read only: nothing is changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot": SNAPSHOT_SCHEMA,
                "fail_on": FAIL_ON_SCHEMA,
            },
            "required": ["snapshot"],
            "additionalProperties": False,
        },
    },
    {
        "name": "diff_snapshots",
        "description": (
            "Diagnose two snapshots and compare the findings. The "
            "report groups findings into new, resolved, and unchanged, "
            "which is the shape I want after a fix or between two "
            "scheduled captures. Read only: nothing is changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "before": SNAPSHOT_SCHEMA,
                "after": SNAPSHOT_SCHEMA,
                "fail_on": FAIL_ON_SCHEMA,
            },
            "required": ["before", "after"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_detectors",
        "description": (
            "List the eight KubePulse detectors with their severity and "
            "what each one looks for. Static data; no cluster input."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


class KubepulseNotFoundError(RuntimeError):
    """The kubepulse binary could not be located."""


class ToolError(RuntimeError):
    """A tool call failed in a way the client should see as isError."""


class JsonRpcError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def find_binary():
    """Resolve the kubepulse binary from KUBEPULSE_BIN or PATH."""
    override = os.environ.get("KUBEPULSE_BIN")
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        raise KubepulseNotFoundError(
            "KUBEPULSE_BIN is set to "
            + override
            + ", but that file is not an executable."
        )
    found = shutil.which("kubepulse")
    if found:
        return found
    raise KubepulseNotFoundError(
        "kubepulse binary not found. Set KUBEPULSE_BIN to its path, or "
        "build it from the KubePulse repository and put it on PATH."
    )


def run_kubepulse(binary, args):
    try:
        proc = subprocess.run(
            [binary] + args,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise ToolError(
            "kubepulse binary not found at "
            + binary
            + ". Set KUBEPULSE_BIN or put kubepulse on PATH."
        )
    except subprocess.TimeoutExpired:
        raise ToolError(
            "kubepulse took longer than "
            + str(SUBPROCESS_TIMEOUT_SECONDS)
            + " seconds and was stopped."
        )
    return proc.returncode, proc.stdout, proc.stderr


def parse_report(stdout, context):
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        raise ToolError("kubepulse did not return valid JSON for " + context + ".")


def checked_run(binary, args, fail_on, context):
    """Run kubepulse and map its exit codes onto tool results.

    Exit 0 is a plain success. Exit 1 only happens when a fail_on gate
    trips, and the report is on stdout in that case too. Exit 2 is bad
    input or bad flags, and anything else is unexpected.
    """
    returncode, stdout, stderr = run_kubepulse(binary, args)
    detail = stderr.strip()
    if returncode == 0:
        return parse_report(stdout, context), False
    if returncode == 1 and fail_on:
        return parse_report(stdout, context), True
    if detail:
        raise ToolError(
            "kubepulse exited with code "
            + str(returncode)
            + " for "
            + context
            + ": "
            + detail
        )
    raise ToolError(
        "kubepulse exited with code " + str(returncode) + " for " + context + "."
    )


def write_snapshot(directory, filename, snapshot):
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)
    return path


def text_result(payload, gate_tripped=None, extra_note=None):
    content = [{"type": "text", "text": json.dumps(payload, indent=2)}]
    if extra_note:
        content.append({"type": "text", "text": extra_note})
    result = {"content": content, "isError": False}
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    return result


def error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def check_fail_on(arguments):
    fail_on = arguments.get("fail_on")
    if fail_on is not None and fail_on not in SEVERITIES:
        raise ToolError(
            "fail_on must be one of "
            + ", ".join(SEVERITIES)
            + "; got "
            + repr(fail_on)
            + "."
        )
    return fail_on


def check_snapshot(arguments, key, tool_name):
    snapshot = arguments.get(key)
    if not isinstance(snapshot, dict):
        raise ToolError(
            tool_name + " needs a " + key + " argument holding a snapshot object."
        )
    return snapshot


class Server:
    def __init__(self, binary):
        self.binary = binary

    def handle(self, message):
        """Return a response dict, or None when no response is due."""
        if isinstance(message, list):
            responses = []
            for item in message:
                response = self.handle(item)
                if response is not None:
                    responses.append(response)
            return responses or None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            if isinstance(message, dict) and "id" in message:
                return self.error(message.get("id"), -32600, "Invalid request.")
            return None
        method = message.get("method")
        if not isinstance(method, str):
            if "id" in message:
                return self.error(message.get("id"), -32600, "Invalid request.")
            return None
        if "id" not in message:
            return None
        request_id = message["id"]
        params = message.get("params") or {}
        try:
            result = self.dispatch(method, params)
        except JsonRpcError as err:
            return self.error(request_id, err.code, err.message)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def error(request_id, code, message):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def dispatch(self, method, params):
        if method == "initialize":
            return self.initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self.call_tool(params)
        raise JsonRpcError(-32601, "Method not found: " + method)

    @staticmethod
    def initialize(params):
        requested = params.get("protocolVersion")
        version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }

    def call_tool(self, params):
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            raise JsonRpcError(-32602, "tools/call needs a tool name.")
        name = params["name"]
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(-32602, "tools/call arguments must be an object.")
        try:
            if name == "diagnose_snapshot":
                return self.tool_diagnose(arguments)
            if name == "diff_snapshots":
                return self.tool_diff(arguments)
            if name == "list_detectors":
                return text_result({"detectors": DETECTORS})
        except ToolError as err:
            return error_result(str(err))
        raise JsonRpcError(-32602, "Unknown tool: " + name)

    def tool_diagnose(self, arguments):
        snapshot = check_snapshot(arguments, "snapshot", "diagnose_snapshot")
        fail_on = check_fail_on(arguments)
        with tempfile.TemporaryDirectory(prefix="kubepulse-mcp-") as tmp:
            path = write_snapshot(tmp, "snapshot.json", snapshot)
            args = ["diagnose", "--format", "json"]
            if fail_on:
                args += ["--fail-on", fail_on]
            args.append(path)
            report, tripped = checked_run(self.binary, args, fail_on, "diagnose")
        note = None
        if tripped:
            note = (
                "The fail_on gate tripped: at least one finding is at or "
                "above severity " + fail_on + "."
            )
        return text_result(report, extra_note=note)

    def tool_diff(self, arguments):
        before = check_snapshot(arguments, "before", "diff_snapshots")
        after = check_snapshot(arguments, "after", "diff_snapshots")
        fail_on = check_fail_on(arguments)
        with tempfile.TemporaryDirectory(prefix="kubepulse-mcp-") as tmp:
            before_path = write_snapshot(tmp, "before.json", before)
            after_path = write_snapshot(tmp, "after.json", after)
            args = ["diff", "--format", "json"]
            if fail_on:
                args += ["--fail-on", fail_on]
            args += [before_path, after_path]
            report, tripped = checked_run(self.binary, args, fail_on, "diff")
        note = None
        if tripped:
            note = (
                "The fail_on gate tripped: at least one new finding is at "
                "or above severity " + fail_on + "."
            )
        return text_result(report, extra_note=note)


def serve(stdin, stdout, binary):
    server = Server(binary)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = Server.error(None, -32700, "Parse error.")
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
            continue
        response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main(argv=None):
    try:
        binary = find_binary()
    except KubepulseNotFoundError as err:
        print("kubepulse-mcp: " + str(err), file=sys.stderr)
        return 1
    serve(sys.stdin, sys.stdout, binary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
