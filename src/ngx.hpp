// Modified downstream from nginx/nginx-otel v0.1.2 for SG5.1.
// See MODIFICATIONS-SG5.1.md; inherited SG4/SG5 history remains documented; Apache-2.0.
#pragma once

extern "C" {
#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>
#include <ngx_http_proxy_module.h>
}

// SG5.1 会读取 NGINX proxy 模块的私有 ngx_http_proxy_loc_conf_t。
// 正式构建仍只允许已验证的 1.31.1；1.31.8 仅能由隔离 CI 的显式实验开关放行。
// 实验放行不意味着 ABI/行为兼容，更不意味着允许用于生产构建或发布。
#if defined(NGX_OTEL_OPENRESTY_13181_LAB) && NGX_OTEL_OPENRESTY_13181_LAB != 1
#error "NGX_OTEL_OPENRESTY_13181_LAB must be exactly 1"
#endif
#if defined(NGX_OTEL_OPENRESTY_13181_LAB) && nginx_version != 1031008
#error "NGX_OTEL_OPENRESTY_13181_LAB is valid only for NGINX 1.31.8"
#endif
#if nginx_version == 1031001
// 已通过 SG5.1 稳定回归的 NGINX 1.31.1 / OpenResty 1.31.1.1。
#elif nginx_version == 1031008 && defined(NGX_OTEL_OPENRESTY_13181_LAB)
// 仅用于 OpenResty 1.31.8.1 的隔离兼容性验证。
#else
#error "nginx-otel SG5.1 requires NGINX 1.31.1, or explicit OpenResty 1.31.8.1 LAB opt-in"
#endif
