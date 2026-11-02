#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""不依赖 OpenResty 运行时的 #1154 诊断逻辑单元测试。"""

import importlib.util
from pathlib import Path
import unittest

from opentelemetry.proto.trace.v1 import trace_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
import grpc


SRC = Path(__file__).with_name("balancer_notify_probe.py")
spec = importlib.util.spec_from_file_location("balancer_notify_probe", SRC)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def add_int_attr(span_or_event, name, value):
    attr = span_or_event.attributes.add()
    attr.key = name
    attr.value.int_value = value


def add_str_attr(span_or_event, name, value):
    attr = span_or_event.attributes.add()
    attr.key = name
    attr.value.string_value = value


def make_trace(case="success"):
    """生成精确结构的合成 OTel 数据，只测试审计判断逻辑。"""
    trace_id = bytes(range(16))
    external_parent = b"external"
    frontend_id = b"frontend"
    client_id = b"client01"
    frontend = trace_pb2.Span(
        trace_id=trace_id, span_id=frontend_id, parent_span_id=external_parent,
        kind=trace_pb2.Span.SPAN_KIND_SERVER,
    )
    client = trace_pb2.Span(
        trace_id=trace_id, span_id=client_id, parent_span_id=frontend_id,
        kind=trace_pb2.Span.SPAN_KIND_CLIENT,
    )
    attempts = 2 if case == "retry_502" else 1
    add_int_attr(client, "http.response.status_code", 200)
    add_int_attr(client, "nginx.upstream.attempts", attempts)
    for i in range(attempts):
        event = client.events.add()
        event.name = "nginx.upstream.attempt"
        add_int_attr(event, "nginx.upstream.attempt", i + 1)
        add_int_attr(event, "http.response.status_code", 502 if case == "retry_502" and i == 0 else 200)
        add_str_attr(event, "nginx.upstream.peer", f"127.0.0.1:{18080 + i}")
    backend = [trace_pb2.Span(
        trace_id=trace_id, span_id=bytes([i + 1]) * 8,
        parent_span_id=client_id,
        kind=trace_pb2.Span.SPAN_KIND_SERVER,
    ) for i in range(attempts)]
    return [frontend, client] + backend, external_parent


class ProbeLogicTests(unittest.TestCase):
    def test_trace_success_and_keepalive(self):
        for case in ("success", "keepalive"):
            with self.subTest(case=case):
                spans, parent = make_trace(case)
                value = probe.summarize_trace(spans, case, parent)
                self.assertEqual(value["client_attempts"], 1)
                self.assertEqual(value["attempt_event_statuses"], [200])
                self.assertEqual(value["parentage"], "verified")

    def test_trace_retry_parentage_and_event_status(self):
        spans, parent = make_trace("retry_502")
        value = probe.summarize_trace(spans, "retry_502", parent)
        self.assertEqual(value["client_attempts"], 2)
        self.assertEqual(value["attempt_event_statuses"], [502, 200])

    def test_reject_missing_client(self):
        spans, parent = make_trace()
        with self.assertRaises(AssertionError):
            probe.summarize_trace([s for s in spans if s.kind != trace_pb2.Span.SPAN_KIND_CLIENT], "success", parent)

    def test_reject_extra_client(self):
        spans, parent = make_trace()
        spans.append(trace_pb2.Span(kind=trace_pb2.Span.SPAN_KIND_CLIENT))
        with self.assertRaises(AssertionError):
            probe.summarize_trace(spans, "success", parent)

    def test_reject_broken_parentage(self):
        spans, parent = make_trace()
        spans[1].parent_span_id = b"badparnt"
        with self.assertRaises(AssertionError):
            probe.summarize_trace(spans, "success", parent)

    def test_reject_fake_attempt_separator(self):
        spans, parent = make_trace()
        spans[1].events[0].attributes[2].value.string_value = ""
        with self.assertRaises(AssertionError):
            probe.summarize_trace(spans, "success", parent)

    def test_reject_wrong_attempt_status(self):
        spans, parent = make_trace("retry_502")
        spans[1].events[0].attributes[1].value.int_value = 200
        with self.assertRaises(AssertionError):
            probe.summarize_trace(spans, "retry_502", parent)

    def test_reject_final_error_attribute(self):
        spans, parent = make_trace()
        add_str_attr(spans[1], "error.type", "502")
        with self.assertRaises(AssertionError):
            probe.summarize_trace(spans, "success", parent)

    def test_reject_failure_footprint_on_success(self):
        spans, parent = make_trace()
        add_str_attr(spans[1], "nginx.upstream.outcome", "http_response")
        with self.assertRaises(AssertionError):
            probe.summarize_trace(spans, "success", parent)

    def test_release_success_collision_and_fixed_success(self):
        base = {"status": 200, "body": "ok\n"}
        base["events"] = [("get", 1), ("free", 0)]
        self.assertTrue(probe.assess("success", base, "release")[0])
        self.assertFalse(probe.assess("success", base, "fixed")[0])
        base["events"] = [("get", 1), ("free", 1)]
        self.assertFalse(probe.assess("success", base, "release")[0])
        self.assertTrue(probe.assess("success", base, "fixed")[0])

    def test_keepalive_subrequest_and_main_request(self):
        plain = {"status": 200, "body": "200: 1\n200: 2\n",
                 "events": [("get", 1), ("free", 1)] * 2}
        self.assertTrue(probe.assess("keepalive", plain, "fixed")[0])
        traced = dict(plain, body="1\n2\n", traces=[{}, {}])
        self.assertTrue(probe.assess("keepalive", traced, "fixed")[0])
        traced["body"] = "1\n1\n"
        self.assertFalse(probe.assess("keepalive", traced, "fixed")[0])

    def test_retry_events(self):
        result = {"status": 200, "body": "ok\n", "events":
                  [("get", 1), ("free", 2), ("get", 1), ("free", 1)]}
        self.assertTrue(probe.assess("retry_502", result, "fixed")[0])
        result["events"] = [("get", 1), ("free", 0)]
        self.assertFalse(probe.assess("retry_502", result, "fixed")[0])

    def test_local_grpc_receiver_roundtrip_with_synthetic_spans(self):
        # 这是 OTLP 接收器协议自检，不能冒充真实 OpenResty 执行结果。
        receiver = probe.TraceReceiver()
        try:
            spans, parent = make_trace("retry_502")
            message = trace_service_pb2.ExportTraceServiceRequest()
            scope_spans = message.resource_spans.add().scope_spans.add()
            for span in spans:
                scope_spans.spans.add().CopyFrom(span)
            with grpc.insecure_channel(f"127.0.0.1:{receiver.port}") as channel:
                stub = trace_service_pb2_grpc.TraceServiceStub(channel)
                stub.Export(message, timeout=3)
            observed = receiver.get_trace(spans[0].trace_id, minimum=4, timeout=3)
            self.assertEqual(len(observed), 4)
            self.assertEqual(probe.summarize_trace(observed, "retry_502", parent)["parentage"],
                             "verified")
        finally:
            receiver.stop()

    def test_generated_keepalive_config_has_escaped_newline(self):
        common = ("keepalive", 18080, 18081, 18082,
                  Path("/tmp/otel-mod.so"), Path("/tmp/nginx-test"))
        plain = probe.config_text(*common)
        traced = probe.config_text(*common, 14317)
        self.assertIn('ngx.location.capture("/proxy")', plain)
        self.assertNotIn('ngx.location.capture("/proxy")', traced)
        self.assertIn('return 200 "$connection_requests\\n";', traced)
        self.assertIn('proxy_pass http://backend;', traced)


if __name__ == "__main__":
    unittest.main()
