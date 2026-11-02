#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""在隔离进程中检验 OpenResty #1154 的 Lua balancer 通知冲突。"""

import argparse
import concurrent.futures
import secrets
import threading
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


BALANCER_RE = re.compile(r"lua balancer: (get|free) peer, tries: (\d+)")


def parse_balancer_events(log_text):
    """按顺序提取上游 debug 日志中的 get/free tries。"""
    return [(name, int(tries)) for name, tries in BALANCER_RE.findall(log_text)]


def ports(count=3):
    """同时占住端口，避免一次分配中出现重复值。"""
    holders = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            holders.append(sock)
        return [sock.getsockname()[1] for sock in holders]
    finally:
        for sock in holders:
            sock.close()


def config_text(case, frontend, backend1, backend2, module, workdir, otel_port=None):
    """每个场景使用独立 NGINX 配置与端口，禁止访问公网。"""
    common = f'''
load_module "{module}";
worker_processes 1;
daemon off;
master_process off;
pid "{workdir / 'nginx.pid'}";
error_log "{workdir / 'error.log'}" debug;
events {{ worker_connections 256; }}
http {{
    access_log off;
'''
    if otel_port is not None:
        common += f"""
    otel_exporter {{
        endpoint 127.0.0.1:{otel_port};
        interval 1ms;
        batch_size 16;
        batch_count 4;
    }}
    otel_trace on;
    otel_trace_context propagate;
    otel_upstream_span on;
"""
    if case == "success":
        upstream = f'''
    upstream backend {{
        server 127.0.0.1:9;
        balancer_by_lua_block {{
            assert(require("ngx.balancer").set_current_peer("127.0.0.1", {backend1}))
        }}
    }}
    server {{
        listen 127.0.0.1:{frontend};
        location /t {{ proxy_pass http://backend; }}
    }}
    server {{
        listen 127.0.0.1:{backend1};
        location / {{ return 200 "ok\\n"; }}
    }}
'''
    elif case == "keepalive":
        upstream = f'''
    upstream backend {{
        server 127.0.0.1:9;
        keepalive 1;
        balancer_by_lua_block {{
            assert(require("ngx.balancer").set_current_peer("127.0.0.1", {backend1}))
        }}
    }}
    server {{
        listen 127.0.0.1:{frontend};
        location /t {{
            content_by_lua_block {{
                for i = 1, 2 do
                    local res = ngx.location.capture("/proxy")
                    ngx.print(res.status, ": ", res.body)
                end
            }}
        }}
        location /proxy {{
            proxy_pass http://backend;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
        }}
    }}
    server {{
        listen 127.0.0.1:{backend1};
        location / {{ return 200 "$connection_requests\\n"; }}
    }}
'''
    elif case == "retry_502":
        upstream = f'''
    upstream backend {{
        server 127.0.0.1:9;
        balancer_by_lua_block {{
            local balancer = require "ngx.balancer"
            local tries = (ngx.ctx.tries or 0) + 1
            ngx.ctx.tries = tries
            if tries == 1 then
                assert(balancer.set_more_tries(1))
                assert(balancer.set_current_peer("127.0.0.1", {backend1}))
            else
                assert(balancer.set_current_peer("127.0.0.1", {backend2}))
            end
        }}
    }}
    server {{
        listen 127.0.0.1:{frontend};
        location /t {{
            proxy_pass http://backend;
            proxy_next_upstream http_502;
            proxy_next_upstream_tries 2;
        }}
    }}
    server {{
        listen 127.0.0.1:{backend1};
        location / {{ return 502; }}
    }}
    server {{
        listen 127.0.0.1:{backend2};
        location / {{ return 200 "ok\\n"; }}
    }}
'''
    else:
        raise ValueError(f"不支持的测试场景: {case}")
    return common + upstream + "}\n"


def wait_for_port(proc, port, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"OpenResty 进程提前退出，退出码={proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"OpenResty 未能监听端口 {port}")


class TraceReceiver:
    """仅在专项实验进程中启用的本地 OTLP/gRPC 接收器。"""

    def __init__(self):
        import grpc
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

        self.spans = []
        self.lock = threading.Lock()
        receiver = self

        class Service(trace_service_pb2_grpc.TraceServiceServicer):
            def Export(self, request, context):
                incoming = [span for resource in request.resource_spans
                            for scope in resource.scope_spans
                            for span in scope.spans]
                with receiver.lock:
                    receiver.spans.extend(incoming)
                return trace_service_pb2.ExportTraceServiceResponse()

        self.server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Service(), self.server)
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        if not self.port:
            raise RuntimeError("无法分配隔离 OTLP/gRPC 接收端口")
        self.server.start()

    def get_trace(self, trace_id, minimum=3, timeout=8):
        # 至少获得所需数量且短时间内无新增，避免过早读取未完全冲刷的批次。
        deadline = time.monotonic() + timeout
        previous_count = -1
        last_change = time.monotonic()
        matches = []
        while time.monotonic() < deadline:
            with self.lock:
                matches = [span for span in self.spans if span.trace_id == trace_id]
            if len(matches) != previous_count:
                previous_count = len(matches)
                last_change = time.monotonic()
            if len(matches) >= minimum and time.monotonic() - last_change >= 0.25:
                return matches
            time.sleep(0.05)
        return matches

    def stop(self):
        self.server.stop(grace=0).wait(timeout=5)


def span_attributes(span):
    """只提取用于判定的 Span 属性，保留真值类型。"""
    result = {}
    for attr in span.attributes:
        value_type = attr.value.WhichOneof("value")
        if value_type:
            result[attr.key] = getattr(attr.value, value_type)
    return result


def summarize_trace(spans, case):
    from opentelemetry.proto.trace.v1 import trace_pb2
    clients = [s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT]
    attributes = [span_attributes(s) for s in clients]
    result = {
        "span_count": len(spans),
        "client_count": len(clients),
        "client_statuses": [a.get("http.response.status_code") for a in attributes],
        "client_attempts": [a.get("nginx.upstream.attempts") for a in attributes],
        "client_error_types": [a.get("error.type") for a in attributes],
        "client_attempt_events": [len([e for e in s.events if e.name == "nginx.upstream.attempt"])
                                  for s in clients],
    }
    if not clients:
        raise AssertionError(f"{case}：未收到任何 CLIENT Span")
    if any(error_type is not None for error_type in result["client_error_types"]):
        raise AssertionError(f"{case}：HTTP 最终成功但 CLIENT Span 带有错误类型")
    if case in ("success", "retry_502"):
        if 200 not in result["client_statuses"]:
            raise AssertionError(f"{case}：CLIENT Span 缺少最终 HTTP 200 状态")
        expected_attempts = 2 if case == "retry_502" else 1
        if expected_attempts not in result["client_attempts"]:
            raise AssertionError(f"{case}：CLIENT attempts 未包含 {expected_attempts}")
        if not any(a == expected_attempts for a in result["client_attempt_events"]):
            raise AssertionError(f"{case}：CLIENT 真实 attempt 事件数量不正确")
    else:
        if not all(status == 200 for status in result["client_statuses"]):
            raise AssertionError("keepalive：CLIENT Span 存在非 200 状态")
    return result


def run_case(case, nginx, module, output_dir, otel_enabled=False):
    workdir = Path(tempfile.mkdtemp(prefix=f"{case}-", dir=output_dir))
    frontend, backend1, backend2 = ports()
    receiver = TraceReceiver() if otel_enabled else None
    trace_id = secrets.token_bytes(16) if otel_enabled else None
    config = config_text(case, frontend, backend1, backend2, module, workdir,
                         receiver.port if receiver else None)
    (workdir / "nginx.conf").write_text(config, encoding="utf-8")
    test_config = subprocess.run(
        [str(nginx), "-p", str(workdir), "-c", "nginx.conf", "-t"],
        capture_output=True, text=True, timeout=20,
    )
    (workdir / "config-check.txt").write_text(
        test_config.stdout + "\n" + test_config.stderr, encoding="utf-8"
    )
    if test_config.returncode != 0:
        if receiver:
            receiver.stop()
        raise RuntimeError(f"配置检查失败：{workdir / 'config-check.txt'}")

    stderr_path = workdir / "process-stderr.log"
    with stderr_path.open("w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(
            [str(nginx), "-p", str(workdir), "-c", "nginx.conf"],
            stdout=subprocess.DEVNULL, stderr=stderr,
        )
        status, body = None, None
        try:
            wait_for_port(proc, frontend)
            request_headers = {}
            if trace_id:
                parent_id = secrets.token_hex(8)
                request_headers["traceparent"] = f"00-{trace_id.hex()}-{parent_id}-01"
            request = Request(f"http://127.0.0.1:{frontend}/t",
                              headers=request_headers)
            with urlopen(request, timeout=8) as response:
                status = response.status
                body = response.read().decode("utf-8", errors="replace")
            if receiver:
                spans = receiver.get_trace(trace_id)
        except (URLError, OSError) as exc:
            raise RuntimeError(f"请求失败：{case}：{exc}") from exc
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            if receiver:
                receiver.stop()

    log_text = (workdir / "error.log").read_text(encoding="utf-8", errors="replace")
    events = parse_balancer_events(log_text)
    result = {
        "case": case, "status": status, "body": body,
        "events": events, "log_dir": str(workdir),
    }
    if receiver:
        result["trace"] = summarize_trace(spans, case)
    return result


def assess(case, actual, expectation):
    """对照上游回归语义，发布原版仅允许显式确认已知冲突。"""
    events = [tuple(x) for x in actual["events"]]
    expected_body = "200: 1\n200: 2\n" if case == "keepalive" else "ok\n"
    if actual["status"] != 200 or actual["body"] != expected_body:
        return False, "HTTP 响应或 keepalive 连接复用与预期不符"

    if expectation == "fixed":
        fixed = {
            "success": [("get", 1), ("free", 1)],
            "keepalive": [("get", 1), ("free", 1)] * 2,
            "retry_502": [("get", 1), ("free", 2), ("get", 1), ("free", 1)],
        }
        return events == fixed[case], f"期望修复后 tries={fixed[case]}，实测={events}"

    if case in ("success", "keepalive"):
        collision = [("get", 1), ("free", 0)] * (2 if case == "keepalive" else 1)
        return events == collision, f"期望原版已知冲突 tries={collision}，实测={events}"
    # 不假定失败重试分支的精确错误轨迹；保留原始日志做人工差异审计。
    ok = len([v for v in events if v[0] == "get"]) >= 2
    ok &= len([v for v in events if v[0] == "free"]) >= 2
    return bool(ok), f"原版重试轨迹（仅检查执行次数，须人工审计）={events}"


def self_test():
    sample = "2026/10/28 debug lua balancer: get peer, tries: 1\n"
    sample += "2026/10/28 debug lua balancer: free peer, tries: 0\n"
    assert parse_balancer_events(sample) == [("get", 1), ("free", 0)]
    for case in ("success", "keepalive", "retry_502"):
        assert "balancer_by_lua_block" in config_text(
            case, 18080, 18081, 18082, Path("/tmp/mod.so"), Path("/tmp/test"))
    print("通过：日志解析与三个 Lua balancer 配置生成的静态自检")


def main():
    parser = argparse.ArgumentParser(description="独立校验 OpenResty #1154 Lua balancer 兼容性")
    parser.add_argument("--nginx", type=Path)
    parser.add_argument("--module", type=Path)
    parser.add_argument("--expect", choices=["release", "fixed"])
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--otel-check", action="store_true",
                        help="额外检查启用 SG5.1 链路追踪时收到的真实 CLIENT Span")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not all((args.nginx, args.module, args.expect, args.output_dir)):
        parser.error("运行集成测试时必须提供 --nginx、--module、--expect、--output-dir")
    nginx = args.nginx.resolve(strict=True)
    module = args.module.resolve(strict=True)
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    summary = {"expect": args.expect, "otel_enabled": args.otel_check,
               "results": [], "passed": False}
    failures = []
    for case in ("success", "keepalive", "retry_502"):
        try:
            result = run_case(case, nginx, module, out, otel_enabled=args.otel_check)
            passed, reason = assess(case, result, args.expect)
            result.update({"passed": passed, "reason": reason})
        except Exception as exc:
            result = {"case": case, "passed": False, "reason": str(exc)}
        summary["results"].append(result)
        print(f"{'通过' if result['passed'] else '失败'}：{case}：{result['reason']}")
        if not result["passed"]:
            failures.append(case)
    summary["passed"] = not failures
    summary["release_eligible"] = False if args.expect == "release" else None
    (out / "balancer-results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if args.expect == "release":
        print("注意：原版复现通过仅证明已知缺陷存在，不表示允许升级生产。")
    else:
        print("注意：隔离修复对照通过不等于正式发行包已包含修复。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
