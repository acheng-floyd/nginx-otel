# SG5.1 与 OpenResty 1.31.8.1 兼容性隔离审计

- 审计日期：2026-11-01（UTC+8）
- 验证目标：`acheng-floyd/nginx-otel`，`v0.1.2-pr97-sg5.1`
- 当前主分支 HEAD：`bc7ae4a58c94e23eb4830cd6160570b580435106`
- 原有 CI：`.github/workflows/ubuntu.yml`；读取时的 blob SHA：`579221f72e8e7b944d1cf7abf0f8cddef5a22605`
- 新增独立 CI：`.github/workflows/openresty-1.31.8.1-compat.yml`
- 上次历史稳定成功：[Actions 33461844643](https://github.com/acheng-floyd/nginx-otel/actions/runs/33461844643)，2026-09-01，三项工作均为 success。
- 审计性质：**实验补丁**。此文件不代表已完成编译、验证或批准生产升级。

## 1. 事实与来源

1. 官方正式版本于 2026-10-26 发布：<https://openresty.org/en/ann-1031008001.html>，版本内核为 NGINX 1.31.8，官方列明 12 项 CVE 修复、OpenSSL 3.5.9、PCRE2 10.49、lua-nginx-module v0.10.32rc7。变更列表：<https://openresty.org/en/changelog-1031008.html>。
2. 官方下载：<https://openresty.org/download/openresty-1.31.8.1.tar.gz>。本补丁固定 SHA256：`b569c63b420f80a4ac8466f2deffe73ed93ab760026532e039c3658409a997f4`，初始参考为公开源码档案镜像 <https://fossies.org/linux/www/openresty-1.31.8.1.tar.gz/index.html>。**本次隔离工作环境无法实际取得该 tar.gz，尚未对本地文件完成 sha256sum 实测**；CI 会从官方站点下载并强制核验该值，失败即停止。官方发布页面列出签名密钥短 ID `A0E98066`，PGP 信任链独立复核仍为待办，不能把第三方 SHA256 等同于签名认证。
3. 上游风险 Issue：<https://github.com/openresty/openresty/issues/1154>。问题描述在新版 NGINX 中 `NGX_HTTP_UPSTREAM_NOTIFY_HEADER = 0x1` 与 OpenResty 缓存连接失败事件的 `0x1` 冲突，导致成功响应错误消费 Lua balancer 重试预算。
4. 官方发布标签 `v1.31.8.1` 指向 `265baf62de60397452f306d61b168ab2c1809d41`，而 2026-10-28 修复提交 `e98f69f8408c83d71dd370dc6de126600da9091d` 在发布后，将缓存连接故障通知值改成 `0x80` 并加入回归测试：<https://github.com/openresty/openresty/commit/e98f69f8408c83d71dd370dc6de126600da9091d>。公开归档的发布版头文件展示了两个 `0x1`：<https://fossies.org/linux/openresty/bundle/nginx-1.31.8/src/http/ngx_http_upstream.h>。
5. 发布版源码里的冲突，和运行中的实际负面影响是不同证据层次。CI 分别记录头文件证据与请求运行时日志；并非仅凭 commit 先后顺序推断全部线上请求失败。

## 2. 变更范围与不变量

**本补丁只新增 4 个文件（含本说明），不会修改现有源代码、`ubuntu.yml`、CMakeLists、现有测试文件或主分支。**

| 路径 | 用途 |
| --- | --- |
| `.github/workflows/openresty-1.31.8.1-compat.yml` | 仅专用分支推送/手工触发，双矩阵隔离验证 |
| `compat/openresty-1.31.8.1/balancer_notify_probe.py` | 原版/修复对照的三个独立 Lua balancer 场景与可选的本地 OTLP Span 核查 |
| `compat/openresty-1.31.8.1/check_junit.py` | 精确数量 JUnit 验证：6 和 56，0 fail/error/skip |
| `compat/openresty-1.31.8.1/README-审计说明.md` | 可追溯证据、测试状态、风险与放行标准 |

- 保留已有原生 NGINX 1.31.1 的 50 项回归和独立双运行时门禁；不重复原生 NGINX 1.31.8 兼容任务。
- OpenResty 正式版分支仍先执行旧的 **6 项专项测试**，再执行全部 **56 项回归测试**；旧测试数字不做调整。
- 验证 SG5.1 关键语义：CLIENT Span、真实 HTTP 状态码、失败来源分类、上游 attempts 计数/分隔状态过滤、Lua phase、TLS、keepalive、成功路径开销（对开销只做功能回归，不以一次 CI 测试充当生产性能基准）。
- 新的 #1154 检验脚本保存在 `compat/` 下，不会被 `pytest tests` 自动收集。
- 只在 ephemeral GitHub runner 的工作目录编译/安装，配置监听 `127.0.0.1`，不连接生产后端。代码不修改核心通知处理逻辑。

## 3. 双矩阵分离（切忌混同）

### `release`：真正的 OpenResty 1.31.8.1 正式源码

1. 官方站点下载原始 tar.gz，校验固定 SHA256；核对 `nginx.h` 核心版本。
2. 独立检查两个通知常量同为 `0x1`，将原版头文件与摘要保存至 CI artifact。
3. 执行 `--with-compat --with-debug` 构建，`--add-dynamic-module` 编译当前仓库 SG5.1；`-V` 核查身份与构建参数，`ldd` 检查运行库，`nginx -t` 验证能够加载 `.so`。
4. 采用 NGINX 核心源码树全文件哈希快照，比对构建前后是否发生意外变化。
5. 独立验证 #1154：正常 200 响应、keepalive 复用、上游首个 502 后重试。原始版 `success/keepalive` 预期会出现 `get tries:1; free tries:0`；**这是缺陷复现成功，而不是升级成功**。重试的详细轨迹保留 JSON 和 debug log 供人工审计，脚本仅要求原版实际进行了两次 peer 尝试。
6. 在每个场景再开启 `otel_trace on`、`otel_upstream_span on`，使用本地临时端口 gRPC TraceReceiver 收集实际 OTLP。校验最终成功 CLIENT Span 的 `http.response.status_code=200`、真实 attempts/attempt Event 数量以及不存在错误类型。收集证据位于独立 `probe-otel-report/`，但不代替现有全部链路语义回归。
7. 使用现有依赖版本和固定 otelcol，运行 6 项专项回归 + 全部 56 项回归。计数检查禁止跳过或降级失败。新 #1154 测试即使失败也不会直接阻止 6/56 用例继续执行：`continue-on-error` 仅用于保全证据，末尾统一门禁仍将任一失败判为失败。

### `upstream-fix-lab`：仅用于对照的局部修复实验

1. **从同一份原版 tar.gz 重新解压独立副本**，先验证其两个常量均为 `0x1`。
2. 在实验副本 `bundle/nginx-1.31.8/src/http/ngx_http_upstream.h` 中，且仅在该文件中，将缓存连接错误通知 `0x1` 改为 `0x80`。要求字节串唯一匹配、保存原始头文件，并产生可追溯单行 diff。
3. 该变更对应 2026-10-28 上游提交的语义，不宣称重新构建了该上游 commit 的全部补丁；不产生可直接投产的 OpenResty 包。
4. 重新编译与加载同一 SG5.1 动态模块，先检查三个 Lua balancer 场景的修复后精确 `tries` 轨迹，再运行启用 SG5.1 OTel 的局部 OTLP 事件核查：
   - 正常 200：`get 1 / free 1`
   - keepalive 连续两次：`get 1 / free 1` ×2，且响应中的 `$connection_requests` 为 1、2
   - 502 重试后成功：`get 1 / free 2 / get 1 / free 1`
5. `upstream-fix-lab` 不重跑 56 项，避免把改过核心的副本误报为“正式版 56 项通过”；所有 56 项仅用于验证正式版。完整双版本 OTel traces 对比属于后续增强门禁。

## 4. 测试结果分级（截至本补丁制作）

| 检查项 | 结果 | 说明 |
| --- | --- | --- |
| GitHub HEAD 与旧工作流内容 | 已核对 | 主分支 HEAD 与 ubuntu.yml SHA 均已独立读取 |
| 历史 OpenResty 1.31.1.1 56 项 | 历史通过 | 来自 2026-09-01 GitHub Actions，不代表新版本通过 |
| 历史 6 项 OpenResty 专项 | 历史通过 | 来自同一次 Actions 运行 |
| 新工作流 YAML 解析 | 本地通过 | Python YAML 基础解析；不代表 GitHub Actions 已执行 |
| 新 balancer 脚本 Python 编译及内置静态自检 | 本地通过 | 检查语法、日志解析与三个配置生成；未启动真正 OpenResty |
| 本地 gRPC TraceReceiver / OTLP 数据接收 | 本地通过 | 已用合成的 CLIENT Span 经真实 127.0.0.1 gRPC 通道完成发送、接收、attempts/事件校验；并非 OpenResty 输出 |
| 新 JUnit 校验脚本语法 | 本地通过 | 仍需实测 CI 产生的真实 XML |
| 正式源码下载与 SHA256、签名验证 | 未验证 | 当前容器无法联网取得归档；CI 待执行 |
| OpenResty 1.31.8.1 + SG5.1 动态编译、ABI、ldd | 未验证 | 本地无 OpenResty 正式包和全部构建依赖 |
| 新版 6 项专项 + 56 项完整回归 | 未验证 | 未获得真实测试 JUnit 结果，不填假数字 |
| #1154 运行时复现、0x80 对照及真实 OTLP Span | 未验证 | 尚未在真实二进制中执行，只验证了测试脚本结构 |
| CentOS 7 / Rocky 9 / Rocky 10 实机兼容 | 未验证 | 本工作流仅 Ubuntu 22.04 runner |

## 5. 操作步骤及禁止事项

从严格匹配上述 HEAD 的本地 git 工作树中执行：

```bash
git rev-parse HEAD
# 应等于 bc7ae4a58c94e23eb4830cd6160570b580435106
git switch -c compat/openresty-1.31.8.1-audit
git apply --check sg51-openresty-1.31.8.1-compat.patch
git apply sg51-openresty-1.31.8.1-compat.patch
git diff --check
git add .github/workflows/openresty-1.31.8.1-compat.yml compat/openresty-1.31.8.1
git commit -m 'test: isolate OpenResty 1.31.8.1 SG5.1 compatibility checks'
# 只有授权的隔离仓库环境才执行下一条；推送不会修改 main
git push -u origin compat/openresty-1.31.8.1-audit
```

隔离 CI workflow 仅监听 `compat/openresty-1.31.8.1-*` 推送并检查新增路径，不监听 `main` 和 pull_request。首次验证应由隔离分支 push 触发（GitHub Actions 的 `workflow_dispatch` 通常需要先在默认分支注册该工作流，不能依赖它启动首次隔离检查）。Actions 中下载两个独立 artifact：原版和实验副本。**成功重现 #1154 并不解除正式版的风险。**

禁止：手工覆盖原有 `.github/workflows/ubuntu.yml`；将 `0x80` 试验修补直接拷贝到现有生产 OpenResty；在验证完成前修改主分支/线上配置；把修补实验副本作为“原厂正式版”发布。

## 6. 平台兼容与动态 ABI 核查

| 平台 | 静态适用性判断 | 实测状态 / 注意点 |
| --- | --- | --- |
| CentOS 7.9 | 构建工具链老旧，系统 GCC 4.8、glibc 2.17，不能复用 Ubuntu 编译的 `.so`；EOL 主机优先减少暴露面 | **未验证**：需隔离主机原生编译、依赖与 libc 版本/符号验证、Nginx -t 和 56 项回归 |
| Rocky Linux 9.6 | 可独立编译，必须核对实际仓库/OpenSSL/PCRE2/运行库版本及模块编译参数 | **未验证**：需原生编译+ldd+ABI+全部功能用例 |
| Rocky Linux 10.2 | 与 Rocky 9 的工具链和系统加密库版本不同，不能把 Rocky 9 编译结果当作兼容证据 | **未验证**：需独立编译和完整功能用例 |

补充：OpenResty 官方公告提及 OpenSSL 3.5.9，但此 CI 使用 `ubuntu-22.04` 系统 `libssl-dev`。应以 `nginx -V`、`ldd` 和运行时输出判断**实际链接版本**，不能由公告推断本次源码构建一定运行 OpenSSL 3.5.9。部署到目标系统必须再核对 OpenSSL、glibc、libstdc++、libpcre2、LuaJIT 链接和编译选项。

## 7. 升级判定

- **当前判定：不得直接升级生产 OpenResty 1.31.8.1。** 源码级冲突已有可交叉核验的公开证据；尚无本仓库真实新版编译与运行结果。该结论不等同于所有生产配置都会触发故障。
- 若 56 项回归失败、模块加载失败、存在未解释的 `502`/重试预算错误或源码完整性不匹配：维持禁止升级。
- 即使 56 项通过，但旧包原版 #1154 复现成功：仍保持禁止升级，继续等待 OpenResty 重新发布包含修复的正式版，或另行走具有明确风险接受审批、额外回归及回滚方案的定制构建发布流程。
- 后续放行应至少具有：真实正式版源码/签名验证、SG5.1 56/56 + 6/6、独立 balancer 成功/缓存连接/失败重试的正确日志和业务行为、OTel trace 对应的 CLIENT/attempt/error/status 真实性、目标 OS 原生验证、监控指标与回滚演练。

## 8. 尚需人工检查的行为差异

- #1154 诊断脚本先以 OTel 关闭状态进行纯 balancer debug 轨迹核验，再单独启用 OTel exporter 向本地临时 gRPC Receiver 发送真实 Span，检查 CLIENT Span 的状态、attempts 和 attempt Events。不过它不是全量 Span 时序/父子关系审计；这些需与 56 项回归结合，必要时进一步加强 Lua 动态重试 parentage 断言。
- 源码本体公钥签名、第三方 SHA256 与正式站点实际归档的一致性，必须待 Actions 下载后核实。
- 尤其关注 `proxy_next_upstream`、upstream keepalive、Lua 请求子请求与正常返回路径下的 OTel Span 真实性及性能；不能凭小范围本地脚本通过判断线上可升级。
- 不自动合并 PR、不启用 auto-merge、不修改线上主机或监控阈值。
