# KubePulse MCP Server

KubePulse is a command line tool I wrote to diagnose Kubernetes clusters from a snapshot file. This project wraps it as a Model Context Protocol server, so an LLM client can call the diagnosis as a tool instead of a human running it in a terminal. The client hands over a snapshot, the server runs the `kubepulse` binary, and the report goes back as structured data the model can reason over.

The boundary from KubePulse carries over unchanged: the server can only diagnose a snapshot and diff two snapshots. There is no tool that changes a cluster, because the underlying CLI has no write paths either.

## Tools

* `diagnose_snapshot`: takes a snapshot object and an optional `fail_on` severity. Returns the full KubePulse report as JSON: findings grouped by severity counts, each with its evidence and a suggested next step.
* `diff_snapshots`: takes `before` and `after` snapshot objects and an optional `fail_on`. Returns the diff report: findings grouped into new, resolved, and unchanged.
* `list_detectors`: takes nothing. Returns the eight detectors with their severity and what each one looks for.

## Install

You need Python 3.11 or newer, and the `kubepulse` binary. Build that first:

```sh
git clone https://github.com/UdayKandyanam/kubepulse.git
cd kubepulse
make build
```

Then install this server:

```sh
git clone https://github.com/UdayKandyanam/kubepulse-mcp.git
cd kubepulse-mcp
pip install .
```

That puts a `kubepulse-mcp` command on your PATH. The server finds the binary through the `KUBEPULSE_BIN` environment variable when it is set, and otherwise looks for `kubepulse` on PATH. If neither works, it stops at startup with a message saying what to set.

## Quick start

Point an MCP client at the server over stdio. A client configuration looks like this:

```json
{
  "mcpServers": {
    "kubepulse": {
      "command": "kubepulse-mcp",
      "args": [],
      "env": { "KUBEPULSE_BIN": "/path/to/kubepulse" }
    }
  }
}
```

The server speaks newline delimited JSON-RPC 2.0 on stdin and stdout, which is the MCP stdio transport. It waits for an `initialize` request, answers `tools/list` and `tools/call`, and exits when stdin closes.

## A worked example

Captured from a run against the real binary. First the handshake:

```text
>>> {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "smoke-test", "version": "0.1.0"}}}
<<< {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "kubepulse-mcp", "version": "0.1.0"}}}
```

Then a `tools/call` for `diagnose_snapshot` with a one pod snapshot (a single container in CrashLoopBackOff, on an invented cluster):

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "diagnose_snapshot",
    "arguments": {
      "snapshot": {
        "cluster": "demo-cluster",
        "pods": [
          {
            "name": "api-7849c6f5d9-xk2lp",
            "namespace": "shop",
            "phase": "Running",
            "containers": [
              {
                "name": "api",
                "restartCount": 14,
                "state": { "waiting": { "reason": "CrashLoopBackOff" } }
              }
            ]
          }
        ],
        "deployments": [],
        "nodes": [],
        "events": []
      }
    }
  }
}
```

The response carries the report twice, once as a text block and once as `structuredContent`. The text block decodes to the report itself:

```json
{
  "cluster": "demo-cluster",
  "summary": { "critical": 0, "high": 1, "low": 0, "medium": 0 },
  "findings": [
    {
      "severity": "high",
      "detector": "crashloop",
      "object": "Pod shop/api-7849c6f5d9-xk2lp",
      "summary": "container api is in CrashLoopBackOff after 14 restarts",
      "evidence": "back-off 5m0s restarting failed container",
      "nextStep": "Read the previous container logs (kubectl logs with the previous flag) and fix the startup failure before raising restart counts further."
    }
  ]
}
```

With `fail_on` set, KubePulse exits 1 when the gate trips. That is not an error to this server: the caller still gets the full report, plus a short note saying the gate tripped and at what severity.

## How it works

```text
MCP client
  |  JSON-RPC 2.0 over stdio, one message per line
  v
kubepulse-mcp (this repo, Python, standard library only)
  |  writes the snapshot to a temp file, runs a subprocess
  v
kubepulse binary (Go, from the KubePulse repo)
  |  prints the report as JSON on stdout
  v
report parsed, checked, and returned as tool content
```

## Design choices

The protocol is implemented directly, with no MCP SDK. At this size the surface is small: an initialize handshake, ping, tools/list, and tools/call, all as newline delimited JSON-RPC. Writing it by hand keeps the dependency count at zero and makes the wire format something I can read in a test, which mattered more to me than saving an afternoon.

The server never imports diagnosis logic. It writes the snapshot argument to a temporary file, runs the CLI with `--format json`, and parses stdout. That keeps one source of truth for what a finding means: the Go detectors and their tests. If KubePulse gains a detector, this server gains it too, with no code change here.

Errors are mapped deliberately. A tool that fails, because the snapshot argument is the wrong shape or the binary rejected it or the binary is missing at call time, comes back as a normal tool result with `isError` set and a readable message, so the model can react to it and the server keeps running. Protocol problems, an unknown method or an unknown tool name, come back as JSON-RPC errors, because those are client bugs rather than tool results.

## Limits

* Snapshots travel through tool arguments, so very large cluster snapshots make for very large requests. This fits incident sized inputs, not a thousand pod fleet capture.
* The server is stdio only and serves one client at a time.
* Diagnosis quality is exactly KubePulse quality. The report is only as good as the snapshot, and KubePulse does not read logs, so a crashloop finding points at the container rather than the stack trace.
* `list_detectors` is static data that mirrors the KubePulse README. If the detectors change, this list needs a matching edit.

## Tests

```sh
pip install -e ".[dev]"
pytest
```

The suite covers the handshake, ping, tools/list, unknown methods, and notifications, plus the tools themselves. Tool tests run against a small stub shell script that stands in for the binary, prints a fixed report, and records the arguments it was called with, so the tests can check both the returned report and the exact command line the server built. A separate set covers the failure cases: a stub that exits 2, a binary missing at call time, and a binary missing at startup.

## Roadmap

* An HTTP transport option alongside stdio, for clients that do not spawn subprocesses.
* A collect tool that builds a snapshot from a live cluster through the CLI, so a client can go from cluster to diagnosis in two calls.
* Resource and prompt templates for the incident questions I reach for most, so clients can ask them the same way every time.

## License

MIT, see `LICENSE`.
