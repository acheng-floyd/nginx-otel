// Modified downstream from nginx/nginx-otel v0.1.2 for SG5.
// See MODIFICATIONS-SG5.md; inherited SG4 history remains documented; Apache-2.0.
#pragma once

extern "C" {
#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>
#include <ngx_http_proxy_module.h>
}

// sg4 reads ngx_http_proxy_loc_conf_t from the NGINX source tree.  That is
// intentionally pinned to the exact NGINX core audited with OpenResty
// 1.31.1.1.  Future NGINX/OpenResty upgrades must be re-audited instead of
// silently reusing this module against a changed private module ABI.
#if nginx_version != 1031001
#error "nginx-otel sg4 is audited only for NGINX 1.31.1 / OpenResty 1.31.1.1"
#endif
