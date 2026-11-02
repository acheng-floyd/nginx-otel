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
from urllib.request import ProxyHandler, Request, build_opener


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
        # 普通复现保留上游的两次 Lua 子请求，以验证缓存连接返回事件。
        # OTel 模式必须使用两次主请求：SG5.1 故意不为 r!=r->main 的子请求生成 CLIENT。
        # 不能用“缺失的子请求 CLIENT”误判新版本不兼容。
        if otel_port is None:
            frontend_location = '''
        location /t {
            content_by_lua_block {
                for i = 1, 2 do
                    local res = ngx.location.capture("/proxy")
                    ngx.print(res.status, ": ", res.body)
                end
            }
        }
        location /proxy {
            proxy_pass http://backend;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
        }
'''
        else:
            frontend_location = '''
        location /t {
            proxy_pass http://backend;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
        }
'''
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
        {frontend_location}
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


def summarize_trace(spans, case, external_parent_id):
    """每次主请求必须得到严格且可追溯的 SERVER→CLIENT→SERVER 关系。"""
    from opentelemetry.proto.trace.v1 import trace_pb2
    clients = [s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT]
    servers = [s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_SERVER]
    expected_attempts = 2 if case == "retry_502" else 1
    if len(clients) != 1 or len(servers) != expected_attempts + 1:
        raise AssertionError(
            f"{case}：SERVER/CLIENT 数量不符，实测 {len(servers)}/{len(clients)}，"
            f"期望 {expected_attempts + 1}/1"
        )
    client = clients[0]
    root_servers = [s for s in servers if s.parent_span_id == external_parent_id]
    if len(root_servers) != 1:
        raise AssertionError(f"{case}：未找到唯一的入口 SERVER Span")
    root = root_servers[0]
    if client.parent_span_id != root.span_id:
        raise AssertionError(f"{case}：CLIENT Span 不是入口 SERVER 的子级")
    backends = [s for s in servers if s is not root]
    if any(s.parent_span_id != client.span_id for s in backends):
        raise AssertionError(f"{case}：上游 SERVER Span 与 CLIENT 父子关系不正确")

    attributes = span_attributes(client)
    events = [e for e in client.events if e.name == "nginx.upstream.attempt"]
    statuses = [span_attributes(e).get("http.response.status_code") for e in events]
    expected_statuses = [502, 200] if case == "retry_502" else [200]
    if attributes.get("http.response.status_code") != 200:
        raise AssertionError(f"{case}：CLIENT 缺少最终真实 HTTP 200 状态")
    if attributes.get("nginx.upstream.attempts") != expected_attempts:
        raise AssertionError(f"{case}：CLIENT attempts 非 {expected_attempts}")
    if len(events) != expected_attempts or statuses != expected_statuses:
        raise AssertionError(f"{case}：attempt 事件及真实 HTTP 状态不符：{statuses}")
    if [span_attributes(e).get("nginx.upstream.attempt") for e in events] != list(
            range(1, expected_attempts + 1)):
        raise AssertionError(f"{case}：attempt 序号不是连续的 1-based")
    if any(not span_attributes(e).get("nginx.upstream.peer") for e in events):
        raise AssertionError(f"{case}：存在缺失 peer 的虚假 attempt 事件")
    for key in ("error.type", "nginx.upstream.outcome", "nginx.downstream.status_code",
                "nginx.upstream.failure.type", "nginx.upstream.failure.stage"):
        if attributes.get(key) is not None:
            raise AssertionError(f"{case}：最终成功的 CLIENT 不应出现 {key}")
    if client.status.code == trace_pb2.Status.STATUS_CODE_ERROR:
        raise AssertionError(f"{case}：最终成功的 CLIENT 不应标记 ERROR")
    return {
        "span_count": len(spans),
        "client_count": 1,
        "client_status": attributes.get("http.response.status_code"),
        "client_attempts": expected_attempts,
        "attempt_event_statuses": statuses,
        "parentage": "verified",
    }


def run_case(case, nginx, module, output_dir, otel_enabled=False):
    workdir = Path(tempfile.mkdtemp(prefix=f"{case}-", dir=output_dir))
    frontend, backend1, backend2 = ports()
    receiver = TraceReceiver() if otel_enabled else None
    status, body = None, None
    traces = []
    try:
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
            raise RuntimeError(f"配置检查失败：{workdir / 'config-check.txt'}")

        stderr_path = workdir / "process-stderr.log"
        with stderr_path.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(
                [str(nginx), "-p", str(workdir), "-c", "nginx.conf"],
                stdout=subprocess.DEVNULL, stderr=stderr,
            )
            try:
                wait_for_port(proc, frontend)
                requests = 2 if case == "keepalive" and otel_enabled else 1
                responses = []
                opener = build_opener(ProxyHandler({}))
                for _ in range(requests):
                    headers = {}
                    trace = None
                    if receiver:
                        trace_id = secrets.token_bytes(16)
                        external_parent_id = secrets.token_bytes(8)
                        headers["traceparent"] = (
                            f"00-{trace_id.hex()}-{external_parent_id.hex()}-01"
                        )
                        trace = (trace_id, external_parent_id)
                    request = Request(f"http://127.0.0.1:{frontend}/t", headers=headers)
                    with opener.open(request, timeout=8) as response:
                        responses.append((response.status,
                                          response.read().decode("utf-8", errors="replace")))
                    if trace:
                        min_spans = 4 if case == "retry_502" else 3
                        received = receiver.get_trace(trace[0], minimum=min_spans)
                        traces.append(summarize_trace(received, case, trace[1]))
                status = responses[-1][0]
                body = "".join(part for _, part in responses)
                if any(http_status != 200 for http_status, _ in responses):
                    raise AssertionError(f"{case}：HTTP 响应码不全为 200：{responses}")
            except (URLError, OSError) as exc:
                raise RuntimeError(f"请求失败：{case}：{exc}") from exc
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
    finally:
        if receiver:
            receiver.stop()

    log_text = (workdir / "error.log").read_text(encoding="utf-8", errors="replace")
    result = {
        "case": case, "status": status, "body": body,
        "events": parse_balancer_events(log_text),
        "log_dir": str(workdir),
    }
    if otel_enabled:
        result["traces"] = traces
    return result

def assess(case, actual, expectation):
    """对照上游回归语义，发布原版仅允许显式确认已知冲突。"""
    events = [tuple(x) for x in actual["events"]]
    if case == "keepalive":
        expected_body = "1\n2\n" if "traces" in actual else "200: 1\n200: 2\n"
    else:
        expected_body = "ok\n"
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
    plain = config_text("keepalive", 18080, 18081, 18082,
                        Path("/tmp/mod.so"), Path("/tmp/test"))
    traced = config_text("keepalive", 18080, 18081, 18082,
                         Path("/tmp/mod.so"), Path("/tmp/test"), 14317)
    assert 'ngx.location.capture("/proxy")' in plain
    assert 'ngx.location.capture("/proxy")' not in traced
    assert 'proxy_pass http://backend;' in traced
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
