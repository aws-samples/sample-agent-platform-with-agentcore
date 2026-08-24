# Agent 平台安全性说明（面向安全团队）

本文回答安全团队对这套平台的核心疑问：session 之间怎么隔离、workspace 会不会跨
session 访问、网络上 AgentCore session 如何被保护、从 Dev Workbench 发布到线上
URL 的链路经过了什么。

考虑到读者可能还不熟悉 Amazon Bedrock AgentCore（AWS 托管 AI agent 运行环境
的服务），本文沿一条正常的使用流程展开：**登录门户 → 打开云端开发环境 →
编写并发布 agent → 外部系统调用线上 URL**，在每一步遇到安全机制时就地解释，
并附 AWS 官方文档引用。

平台自身 IAM 权限的逐条清单（每个角色的 action、资源范围、通配符原因）在
[permissions.md](permissions.md)，那份文档专为权限审批写的，本文不重复，只在
相关处引用。

---

## 目录

0. [两个核心概念：Runtime 和 Gateway](#0-两个核心概念runtime-和-gateway)
1. [第一步：登录门户（浏览器里没有任何 AWS 凭证）](#1-第一步登录门户)
2. [第二步：打开 Dev Workbench（一个 session 就是一台独立微型虚拟机）](#2-第二步打开-dev-workbench)
3. [Session 隔离的三层边界](#3-session-隔离的三层边界)
4. [Workspace 持久化与跨 session 访问的真实答案](#4-workspace-持久化与跨-session-访问)
5. [网络：AgentCore session 在网络上如何被保护](#5-网络agentcore-session-在网络上如何被保护)
6. [第三步：从 Dev Workbench 发布到线上 URL 的完整链路](#6-第三步从-dev-workbench-发布到线上-url)
7. [线上 URL 的调用方鉴权与治理](#7-线上-url-的调用方鉴权与治理)
8. [工具层：MCP、内置工具与 AgentCore Gateway 的鉴权](#8-工具层mcp内置工具与-agentcore-gateway)
9. [模型访问：密钥与路由](#9-模型访问密钥与路由)
10. [数据保护与审计](#10-数据保护与审计)
11. [已知边界与收紧选项](#11-已知边界与收紧选项)
12. [官方文档引用汇总](#12-官方文档引用汇总)

---

## 0. 两个核心概念：Runtime 和 Gateway

在进入流程之前，先解释贯穿全文的两个 AgentCore 服务组件。

**AgentCore Runtime** 是托管 agent 代码的无服务器运行环境。agent 被打包成一个
容器镜像注册上去，之后每个"用户会话"（session）由服务在一台**专属的 microVM**
（微型虚拟机，与 AWS Lambda / Fargate 同源的 Firecracker 隔离技术）里运行：
独立的内核、CPU、内存和文件系统。session 结束后整台 microVM 被销毁、内存被清理。
这是本平台所有隔离性的物理基础，官方描述见
[Security best practices for AgentCore Runtime][bp] 和
[Use isolated sessions for agents][sessions]。

**AgentCore Gateway** 是把企业已有的 API（Lambda、REST、其他 MCP 服务）转换成
agent 可调用的 MCP 工具的托管网关。它在入站方向校验调用者身份（IAM SigV4 或
JWT），在出站方向按目标配置注入凭证（IAM 角色、OAuth、API key），详见第 8 节。

本平台在 Runtime 上注册了三个容器镜像（内部称"内核"）：

| 内核 | 用途 |
|---|---|
| `claude_code_kernel` | 交互式：完整的 Claude Code CLI，通过浏览器 Web 终端使用，即 Dev Workbench |
| `agent_sdk_kernel` | 无头：Claude Agent SDK 封装在标准 `/invocations` API 后面，所有已发布 agent 共享它 |
| `mcp_tools_kernel` | 一个示例 MCP 工具服务，演示 AgentCore 托管 MCP server |

有一个关键事实会在第 6 节展开：**已发布的 agent 不是独立容器**，而是一份
版本化配置，由共享的无头内核按调用执行。正因如此，"发布"这个动作
不引入新的攻击面。

---

## 1. 第一步：登录门户

用户打开门户 URL（CloudFront 域名），点击"Sign in with corporate SSO"，跳转到
企业 OIDC IdP（本部署为 Keycloak）完成登录。登录后前端持有 IdP 签发的 access
token，此后每个 `/api` 请求都带这个 Bearer token。

平台的门户认证是可插拔的：默认形态是 Amazon Cognito 用户池（账号由运维人员
创建，自助注册关闭）；设置 `PLATFORM_OIDC_ISSUER` 后切换为企业 SSO，本部署
采用后者。两种模式下后端的校验逻辑同构，安全性质一致。

这一步的安全要点：

- **浏览器和最终用户从头到尾不持有任何 AWS 凭证。**所有 AWS API 调用都在服务端，
  由四个 IAM 角色执行（[permissions.md §1](permissions.md#1-principals-at-a-glance)）。
- 后端验证 token 的方式是通过 OIDC discovery 拉取 IdP 的公开 JWKS 做签名校验
  （同时校验 issuer 和 audience；Cognito 模式下另校验 `token_use`）。这是纯
  HTTPS 公钥操作，不需要任何 AWS IAM 权限，也不依赖与 IdP 的共享密钥。
- **IdP 签发的身份声明原样保留。**SSO 模式下后端直接消费 access token，token
  里的组/团队 claim（如 `team`）经签名验证后进入请求上下文，供后续的团队级
  授权使用（见第 8 节的 Gateway 链路与 [enterprise-sso.md](enterprise-sso.md)）。
  用户是谁、属于哪个团队，由企业 IdP 说了算，平台不自建第二套身份。
- **登出同时终止 IdP 会话。**门户的 Sign out 不只清本地 token，还会结束 IdP
  侧的 SSO 会话；否则下次登录 IdP 会静默返回同一身份，换人场景下会串号。
- 门户全链路 HTTPS：CloudFront 强制跳转 HTTPS；ALB 只接受来自 CloudFront 托管
  前缀列表的流量；ECS 后端只接受来自 ALB 的流量。VPC 内**没有任何 0.0.0.0/0
  入站规则**。

---

## 2. 第二步：打开 Dev Workbench

用户点击"New Session"后，获得一个运行在云端的完整 Claude Code 开发环境。
这一步背后发生的事：

1. 后端在 DynamoDB 写入 session 记录，**分区键是 `USER#{用户名}`**，记录天然
   归属于创建者。
2. 后端生成一个随机的 `runtimeSessionId`（两段 UUID 拼接，满足 AgentCore 的
   33 字符下限），**由服务端生成，客户端无法指定**。
3. 后端以自己的 IAM 角色调用 `InvokeAgentRuntime` 做预热。AgentCore 看到一个
   新的 session ID，就**为它启动一台专属 microVM**，拉起交互式内核容器。
4. 浏览器终端的连接方式：浏览器无法在 WebSocket 握手上附加自定义鉴权头，所以
   后端用 SigV4QueryAuth 预签名一个 **5 分钟有效**的 WSS URL 交给前端。凭证
   始终只在后端；容器自身的角色没有权限签发这种 URL。

对 AgentCore 来说，"session"就是隔离的单位：同一个 `runtimeSessionId` 的后续
调用复用同一台 microVM（保留上下文、避免冷启动），不同的 session ID 一定落在
不同的 microVM 上。官方对 session 语义的完整定义见
[Use isolated sessions for agents][sessions]。

生命周期参数（官方默认值，均可调）：session 空闲 **15 分钟**后执行环境被回收，
单个执行环境最长存活 **8 小时**；到点后 microVM 终止并清理内存
（[Quotas][quotas]、[Lifecycle settings][lifecycle]）。平台在此之上补充了两项
机制：浏览器每 20 秒发一个保活帧，防止用户仍连接在终端时环境被回收；回收后下次连接
从 S3 恢复文件和对话历史（见第 4 节）。但这些不改变隔离语义：**恢复出来的是一台
全新的 microVM**。

---

## 3. Session 隔离的三层边界

"session 之间怎么隔离"这个问题，实际上是三个不同的问题：一个 session 里的代码
能不能碰到另一个 session 的内存和文件（计算层）；一个用户能不能连上另一个用户
的 session（平台层）；session 里的代码拿容器凭证能做什么（身份层）。三层的
防御主体不同，下面分开回答：

### 3.1 计算层：每 session 一台 microVM（AWS 托管）

官方安全最佳实践原文的要点（[Security best practices for AgentCore Runtime][bp]）：

> Each user session runs in a dedicated microVM with isolated CPU, memory, and
> filesystem. Commands and agent code cannot access other customers' workloads
> or escape the VM boundary. After session completion, the entire microVM is
> terminated and memory is sanitized.

也就是说：session A 里的代码（包括 agent 自行生成、执行的任何命令）**没有路径**
读到 session B 的内存或文件系统：不同 session 之间没有共享文件系统、没有共享
内存、没有共享网络栈。session 结束后整机销毁、内存清理，不存在残留数据被下一个
session 读到的问题。这层隔离由 AWS 提供并保证，不依赖平台代码自身的正确性。

Web 终端给用户的 shell 权限也只存在于**这台 microVM 之内**。用户在终端里是
root，但作用范围仅限于属于该用户的这台一次性虚拟机。

### 3.2 平台层：session 与用户的绑定（平台负责）

官方文档明确划分了责任边界：

> AgentCore does not enforce session-to-user mappings — your client backend
> should maintain the relationship between users and their session IDs.

即：AgentCore 保证"不同 session ID 互相隔离"，但"哪个用户能用哪个 session ID"
由调用方后端负责。本平台的实现：

- session 记录以 `USER#{用户名}` 为 DynamoDB 分区键；所有 session API（连接、
  停止、删除、浏览文件）都先用**当前登录用户**做键查询，查不到即 404。用户 A
  按 ID 猜测用户 B 的 session，在 API 层就被挡住。
- `runtimeSessionId` 由服务端随机生成且不回传给不相关用户，无法枚举。
- 终端 WSS URL 是按次签发、5 分钟过期的一次性凭证，只有通过了上述归属校验的
  请求才会拿到。

### 3.3 身份层：容器内的 IAM 角色是最小权限（平台负责）

官方最佳实践特别提醒一点，值得安全团队注意：

> Any code or actor running inside the microVM can access execution role
> credentials by calling the metadata endpoint. Scope your execution role
> permissions carefully.

microVM 内的任何代码都能拿到容器执行角色的临时凭证，这是所有容器托管服务的
共性，防御方式就是把这个角色的权限收敛到最小。本平台**每个内核一个独立角色**，
各自只持有自己代码真正调用的权限，逐条列在
[permissions.md §2](permissions.md#2-runtime-execution-roles)：交互式内核角色
只有技能包只读（workspace 同步不用这个角色，见第 4 节）；无头内核角色多出
异步产物前缀和 AgentCore Memory（跨会话记忆存储）的数据面读写；MCP 示例内核
角色没有任何 S3 和密钥权限。`InvokeAgentRuntime` 仅限 `mcp_tools_kernel` 这
一个资源，一个内核**不能**调用其他内核或其他 runtime。所有角色都不能创建/修改
IAM、不能读指定名字之外的任何密钥、不能跨账号
（[permissions.md §6](permissions.md#6-what-is-deliberately-not-granted)）。

---

## 4. Workspace 持久化与跨 session 访问

microVM 是一次性的，但开发工作需要持久化，所以交互式内核把 `/workspace` 和
Claude Code 状态每 30 秒同步到
`s3://agent-platform-workspaces-{account}-{region}/workspaces/{sessionId}/`，
冷启动时从**同一个前缀**恢复。每个 session 有自己的前缀，互不混用。

"workspace 会不会跨 session 访问"要分两条路径回答：

**门户 API 路径（用户视角）：不能。**门户的 Workspace 浏览接口先按
`USER#{用户名}` 查 session 归属，再用 session 记录里的前缀去列 S3，用户只能
看到自己 session 的文件，这条路是严格隔离的。

**运行时路径（容器内代码视角）：同样不能。**这里有一个容易被追问的点，值得
展开。AgentCore 的执行角色是 runtime 级的：同一个 runtime 的所有 session 共享
同一个容器角色，而 microVM 内的任何代码都能从元数据端点拿到这个角色的凭证
（第 3.3 节）。所以如果把 workspace 桶的读写权限授给容器角色，纯 IAM 条件无法
区分"哪个 session 在用"，一个 session 里的代码就能读到其他 session 的前缀。

本平台的做法是把这条路径整个关掉：**交互式内核的执行角色没有任何
`workspaces/*` 权限**。workspace 同步用的是另一套凭证。后端（它掌握 session
与用户的映射）在每次 session 连接时向 STS 申请一份临时凭证，附带一条 session
policy 把 S3 权限限定在 `workspaces/{本session}/*` 这一个前缀上，随预热请求
下发给该 session 的容器。容器把它写进一个仅供同步命令使用的独立凭证文件，
默认凭证链保持在执行角色上。凭证一小时过期（角色链上限），容器凭一个 session
专属的刷新令牌（常数时间比较，与 Channel token 同一模式）向后端换新。

由此，容器内的恶意代码即使读元数据端点，拿到的角色也没有 workspace 权限；它
唯一持有的 S3 凭证被 session policy 限定在自己的前缀内。而 session policy 只能
收窄、不能放大所 assume 角色的权限，所以即使后端出 bug，也越不过专用角色本身
`workspaces/*` 的上界。

三个内核的角色也是分开的：无头内核（执行所有已发布 agent 和外部 prompt 的
地方）对 `workspaces/*` 完全无权限，只有技能包只读和异步产物前缀的读写；MCP
示例内核没有任何 S3 权限。逐条清单见
[permissions.md §2](permissions.md#2-runtime-execution-roles)。

S3 桶本身：`BLOCK_ALL` 公共访问、强制 SSL、S3 托管加密、删除栈时保留（防止
误删会话数据），只有运行时角色和后端角色两个主体能访问。

---

## 5. 网络：AgentCore session 在网络上如何被保护

AgentCore Runtime 默认的 PUBLIC 网络模式经由 AWS 托管的 NAT 池出网，IP 不固定。
本平台改用 **VPC 模式**（[Configure AgentCore Runtime for VPC][vpc]），每台
microVM 的网卡（ENI）落在客户 VPC 的**私有子网**里：

```
microVM ENI → 私有子网 → NAT Gateway（固定 EIP）→ 互联网/AWS 服务
```

这一架构带来三项网络层保障：

1. **没有公网 IP。**运行时 ENI 全部在私有子网，从互联网没有任何路由能直接到达
   某台 microVM。
2. **入站不走 VPC。**调用 agent 的流量（`InvokeAgentRuntime`、终端 WebSocket）
   通过 AgentCore 的服务数据面送达容器，每一个入站请求都要过 IAM SigV4 鉴权
   （见第 7 节）；因此运行时安全组是**纯出站**的，一条入站规则都没有。不是
   "只开放了必要端口"，而是**零入站**。
3. **出站汇聚到一个固定 IP。**所有出网流量（LLM 网关、ECR、Secrets Manager、
   S3、日志）都从同一个 NAT Gateway 的弹性 IP 离开。这既是给 LLM 网关做源 IP
   白名单的前提（网关只放行这一个 /32），也让 agent 的全部外联在网络层可观测、
   可管控。

另有一个运维性质的细节：网络配置并非只能在创建时设定，存量 runtime 可以在
PUBLIC 和 VPC 之间切换而不改变 ARN。

更进一步的形态（如敏感数据场景下完全不出公网、通过 VPC endpoint 私有调用）见
官方博客 [Network connectivity patterns for agents on AgentCore Runtime][netblog]，
本平台的 VPC 模式即其中的标准企业出网模式。

---

## 6. 第三步：从 Dev Workbench 发布到线上 URL

用户在 Dev Workbench 里完成 agent 行为的调试之后，在 workspace 根目录写一份
`agent.yaml` 清单：

```yaml
name: support-triage
description: Classifies inbound tickets
system_prompt: |
  You triage support tickets into billing / bug / feature.
max_turns: 8
mcp_servers: [platform-tools]   # 按名字引用注册表条目
skills: [code-review-checklist]
memory_id: ""
```

然后在 Publish 页选择该 session 执行发布。这条链路上每一步的安全含义：

**发布产物是一份配置，不是代码或镜像。**后端从该 session 的 S3 前缀读出
清单，校验后写入 DynamoDB 成为一条**版本化配置记录**（系统提示词、工具引用、
轮次上限、memory 绑定、模型后端选择）。没有镜像构建、没有新容器、没有新 IAM
角色、没有新网络路径。安全审查的范围因此大幅缩小：

- **发布动作不扩大攻击面。**已发布 agent 在调用时由共享的无头内核执行，其计算
  隔离（每次调用的 session 一样是独立 microVM）、IAM 权限（同一个最小权限运行时
  角色）、网络路径（同一个 VPC/NAT 出口）与发布前的调试调用完全一致。对这个
  运行时环境的一次审查结论，适用于所有已发布 agent。
- **workspace 里的其他文件不会被"发布"出去。**发布只读取清单这一个文件；agent
  运行时也不挂载开发时的 workspace。
- **引用在发布时校验。**清单里的 MCP server、skill、模型后端都按名字对注册表
  和治理配置校验，写错名字或引用被禁用的后端，发布直接失败，不会在配置
  有误的情况下上线。
- **每次发布记入审计日志**（谁、什么时间、发布了哪个 agent 的哪个版本），同名
  重发布是版本递增，历史保留。

发布机制不防御的一点也要说明：系统提示词本身的内容是发布者写的，平台不审查
其语义。一个写得糟糕（或恶意）的提示词能让 agent 输出糟糕的回答，但它改变不了
agent 的权限边界，agent 能做的事仍被上述运行时角色、网络出口和治理配额框住。

**发布后，agent 获得一个门户 API 端点（即"线上 URL"）：**

```
POST https://{portal}/api/v1/agents/{id}/invoke      {"prompt": "..."}
```

一次外部调用的完整路径和每一跳的保护：

```
调用方
  │  HTTPS（TLS，CloudFront 强制）
  ▼
CloudFront ──→ ALB（仅接受 CloudFront 前缀列表来源）──→ ECS 后端
  │
  │  ① 鉴权：Cognito ID token 或 Channel token（见第 7 节）
  │  ② 治理管道：配额检查 → 来源开关 → 轮次上限
  │  ③ 解析 agent 配置（含模型后端路由）
  ▼
InvokeAgentRuntime（后端 IAM 角色 SigV4 签名）
  │
  ▼
AgentCore 数据面 → 为本次调用的 session 分配/复用专属 microVM → 无头内核执行
  │
  ▼
④ 结果与元数据（延迟、轮次、成本、实际使用的 backend:model）写入调用台账
```

调用方接触到的只有门户这一层；AgentCore runtime 的 ARN、AWS 凭证、VPC 内部
结构对调用方全部不可见。

---

## 7. 线上 URL 的调用方鉴权与治理

调用已发布 agent 有两种身份路径，面向不同受众：

**路径一：IdP 签发的 Bearer token（平台用户 / 内部系统）。**与门户登录同一套
身份：SSO 模式下是企业 IdP 的 access token，Cognito 模式下是用户池的 ID token
（12 小时过期），适合能走企业身份体系的场景。

**路径二：Channel token（外部系统，推荐给机器调用）。**为外部系统（聊天机器人
桥接、CI、运维 webhook）设计：创建 Channel 时服务端生成一个随机 token，**只显示
一次**，之后不可再取（轮换 = 删除重建）；校验用常数时间比较防时序侧信道。这条
路径的授权范围极窄：一个 token 只能调用**它绑定的那一个 agent**，无法访问任何
其他平台 API。外部系统因此完全不需要 AWS 凭证或用户池账号。

无论哪条路径，进入的都是**同一条治理管道**，没有旁路：

- **配额**：按用户和平台总量的每日调用上限，超限返回 429；
- **来源开关**：debug / api / schedule / channel / eval 五个入口各有独立
  开关，例如停用 `channel` 后所有 webhook 立即返回 429，可作为紧急停用手段；
- **轮次上限**：平台级 max-turns 上限优先于调用方指定的值；
- **调用台账**：每次调用记录来源、目标、调用者、延迟、轮次、成本、错误、实际
  使用的模型后端，Observability 页可查；平台的每个变更动作另有只可追加的
  审计日志。

底层 `InvokeAgentRuntime` 这一跳的鉴权由 AWS 负责：AgentCore Runtime 的默认
入站鉴权就是 **IAM SigV4**，与调用任何 AWS API 相同的签名机制
（[Authenticate with Inbound Auth and Outbound Auth][oauth]）。只有持有
`bedrock-agentcore:InvokeAgentRuntime` 权限的主体（本平台里即后端任务角色和
调度 Lambda 角色）能够调用 runtime。AgentCore 也支持把入站鉴权换成企业 IdP 的
JWT（[Configure inbound JWT authorizer][jwt]），让用户自己的身份令牌直达
agent；本仓库的 enterprise SSO 演示（[enterprise-sso.md](enterprise-sso.md)）
展示了这条形态。

---

## 8. 工具层：MCP、内置工具与 AgentCore Gateway

agent 的能力边界由它能调用的工具决定，因此工具链路的鉴权同样属于审查范围。
每类工具要回答的其实是同一个问题：**凭证在哪一跳注入，谁有权拒绝这次调用。**
四类工具的答案各不相同：

**AgentCore 托管的 MCP server**（如平台自带的 `mcp_tools_kernel`）：内核通过
`mcp-proxy-for-aws`（AWS 提供的本地代理，把 MCP 请求转为 SigV4 签名的
AgentCore 调用）访问，每个请求用容器角色做 SigV4 签名。运行时角色的
`InvokeAgentRuntime` 权限**仅**授予这一个 MCP runtime 资源，agent 无法
借这条通道调用其他 runtime。

**AgentCore Gateway**（如 feed 流水线用的托管 Web Search connector）：同样走
`mcp-proxy-for-aws` 做 SigV4，鉴权用容器角色，没有需要保管的 API key。检索
query 由 AWS 内部的索引服务处理，不出 AWS、不经第三方搜索 API —— 对"用户提问会
流到哪里"这类审查，这条通道本身就没有出网面。

**外部 URL 型 MCP server**（接第三方服务时）：API key 不落注册表：注册表里存的
是 `{{secret:agent-platform/remote-mcp-key}}` 占位符，内核在 session 启动时才从
Secrets Manager 解析，且运行时角色的密钥读取权限精确到这一个密钥名。平台自带的
能力都不走这条路，它是留给适配自有第三方服务的。

**内置工具（Code Interpreter / Browser）**：代码执行和浏览器自动化分别跑在
AWS 托管的独立沙箱里（不在 agent 的 microVM 内），以容器角色鉴权，权限精确到
这两类资源（[Security Reference Architecture for Generative AI][sra] 对这两个
托管沙箱的隔离有专节描述）。

**AgentCore Gateway**：当 agent 需要访问企业已有的 API 时，Gateway 把它们统一
转换成 MCP 工具，鉴权分两段：

- **入站**：校验"谁在调用 Gateway"：IAM SigV4，或配置 JWT authorizer 对接企业
  IdP（校验 issuer、audience、client、scope，[官方文档][jwt]）；
- **出站**：按目标配置"以什么身份调用后端"：IAM 角色、OAuth 2.0（可做 on-behalf-of
  令牌交换，把**最终用户**的身份带到后端）、或 API key。

这两段的组合决定了"授权在哪里裁决"：出站凭证带用户身份（OAuth 交换），后端自己
做授权；出站是静态 API key，则由 Gateway 的 interceptor 裁决，因为后端不知道
调用者是谁。门户的 Gateway 页把每个 target 的这项属性直接列了出来，细节见
[enterprise-sso.md](enterprise-sso.md#where-authorization-happens)。

---

## 9. 模型访问：密钥与路由

### 9.1 前提：容器里没有"对用户保密"的东西

先把边界说清，因为它决定了这一节的所有设计。

Dev Workbench 给用户的是 session 所属 microVM 里的一个真实终端，用户在其中是
root。因此容器里的一切都视为用户可见：环境变量、文件、进程内存，以及执行角色
从 metadata 端点取到的凭证。headless 内核同理，Claude Agent SDK 会 fork 出
CLI 子进程，agent 的工具就在那个子进程里执行。

由此得到一条硬规则：**运行时角色只能持有"这个用户本来就有权使用的东西"，不能
持有代表平台或其他租户的凭证。** 平台在 workspace S3 上一直遵循这条规则
（内核角色对 `workspaces/*` 零权限，由后端按 session 铸凭证，见第 4 节）。
模型凭证现在同样遵循。

### 9.2 两个后端

模型调用有两个后端，由治理页的模型控制面按 agent 路由：

- **Bedrock 直连**：以容器的 IAM 角色调用 `bedrock:InvokeModel*`，不存在长期
  密钥，模型 ID 用 `global.` 前缀的跨区推理配置。
- **LLM 网关（如 LiteLLM）**：容器**不持有网关密钥**。密钥只存在于
  `llm-edge` 这个内部服务的任务角色里（`agent-platform-llm-edge`，全平台唯一
  被授予该密钥读权的主体；运行时角色已不再有此授权）。内核拿到的是一份按
  session 铸出的短期凭证。

### 9.3 网关模式的调用链

```
Claude Code / Agent SDK
  ANTHROPIC_BASE_URL = http://127.0.0.1:8787   ← 容器内 loopback
  ANTHROPIC_AUTH_TOKEN = unused                ← 字面量，不是密钥
    ↓
loopback shim（内核进程内）
  session 凭证只存在于该进程内存，不写环境变量、不落磁盘
    ↓
内网 ALB（仅 VPC 内可解析可达，公网无路由）
    ↓
llm-edge
  凭证 → 反查 grant：是否有效、属于哪个用户/团队、允许哪些模型
  注入真实网关密钥，流式转发，记录用量
    ↓
LiteLLM（VPC 外，经固定 NAT EIP 出网，网关侧做源 IP 白名单）
```

grant 里的上游地址、密钥名、模型白名单，全部由后端在铸凭证时写入，
`llm-edge` 每次调用都重新读取。**容器上报的任何路由信息都不被采信**，所以租户
无法通过改请求把自己路由到别的模型或别的上游。

### 9.4 可验收的三条

1. 容器的环境变量、文件、进程命令行、Claude Code 会话里，**不存在任何真实
   密钥**。原先放平台级网关 key 的位置现在是字面量 `unused`。
2. 把容器内一切可获取的材料（环境变量、文件、内存中的 session 凭证、IAM 角色
   凭证）导出到外部机器，**均不可用**：`llm-edge` 的监听器在 VPC 内网，公网无
   路由；session 凭证在一小时内过期，且只对该 session 被允许的模型有效。
3. 在 session 内滥用只能消耗该 session 自己的额度，且归因到具体用户。

### 9.5 残余边界

用户可以在自己的 session 里直接调用那个 loopback 端口来使用模型。这是设计
预期：这本就是他有权使用的额度，有上限、可归因、随 session 失效。安全团队
评估时应当把它理解为"用户正常使用自己的配额"，而不是越权。

每次调用实际走了哪个后端、哪个模型，记入调用台账（`backend:model` 字段），
路由变更前后可审计；`llm-edge` 另有结构化日志记录每次调用的 session、用户、
模型、状态与 token 用量。

---

## 10. 数据保护与审计

| 数据 | 位置 | 保护 |
|---|---|---|
| Session 文件、对话历史 | Workspace S3 桶 | 公共访问全阻断、强制 SSL、静态加密、仅两个角色可访问 |
| 平台记录（session/agent/schedule/台账/审计） | DynamoDB | 静态加密（默认 AWS 拥有密钥，可换 CMK） |
| LLM 网关 key | Secrets Manager | 加密存储；仅 `llm-edge` 任务角色可读，内核角色无此授权；从不写入镜像，也从不进入任何 session 容器 |
| 可选的第三方 MCP key、管理员口令 | Secrets Manager | 加密存储；按密钥名精确授权；容器/Lambda 启动时读取，从不写入镜像 |
| 内核与平台日志 | CloudWatch Logs | 前缀级授权；平台自有日志组 7 天保留 |

AgentCore 服务侧的数据（session 元数据、Memory 记录等）默认用 AWS KMS 加密静态
存储，部分资源支持客户托管密钥（CMK），见官方
[Data encryption][enc]。microVM 生命周期结束时的内存清理见第 3.1 节的官方引文。

审计记录分三处，排查时按问题类型选入口：想知道"谁改了平台配置"查平台审计
日志（每个变更动作，只可追加）；想知道"某次 agent 调用发生了什么"查调用台账
（来源、调用者、延迟、成本、实际模型路由）；要下钻到单次调用内部的每个工具
调用和模型轮次，查 AWS 侧的 CloudWatch GenAI Observability（span 级 OTel
追踪，`/aws/bedrock-agentcore/runtimes/*`），AWS API 层的操作则在 CloudTrail。

---

## 11. 已知边界与收紧选项

以下每一条都是有取舍的工程判断，不是遗漏。安全团队评估时应当逐条确认取舍是否
符合本组织的要求。

1. **交互式内核仍持有 `bedrock:InvokeModel*`，资源为 `*`**。这带来两个后果，
   需要一并看：
   - 跨区推理配置横跨多个区域，无法收紧到单区。可用 SCP 或权限边界按推理配置
     ARN 约束。
   - 更重要的是，拿到 Dev Workbench 终端的用户可以在 shell 里直接调用账号内
     任何 Bedrock 模型，**绕过治理页按 agent 配置的模型路由**。这不是凭证泄露，
     而是角色本身授予的权限。Bedrock 直连模式的价值在于完全没有可被带走的密钥，
     代价就是模型管控落在 IAM 策略范围而不是应用层白名单上。
     若要求模型白名单对交互式 session 强制生效，做法是让该部署只用网关模式，并
     把 `bedrock:*` 从交互式角色整体删除（第 3 条）；此时全部模型访问都经过
     `llm-edge`，白名单与预算在租户碰不到的位置执行。
2. **`llm-edge` 的内网监听器默认为明文 HTTP**。该链路位于 VPC 内网、公网无
   路由，但其请求体承载 prompt 内容。设置 `llm_edge_certificate_arn` 即启用
   TLS；默认不启用是因为私有监听器需要一张部署方自己拥有的域名证书，这一点无法
   预设。处理受监管数据的部署应当设置它。
3. **不用的能力可以整体移除**：内置工具（Code Interpreter/Browser）、Bedrock
   权限（纯网关模式下运行时角色可以完全没有 `bedrock:*`）都是独立的策略语句，
   删除即生效。
4. **headless 异步任务的网关凭证寿命较长**。异步运行最长到平台的 8 小时上限，
   且没有续期通道，因此其 grant 的有效期与之匹配（9 小时）而不是 1 小时。影响
   有限：headless 内核没有终端，且该 grant 从不进入 agent 子进程的环境（内核
   只给子进程一个容器内本地令牌，见第 9.3 节）。
5. **加密密钥默认为 AWS 托管**：合规要求 CMK 时，S3/DynamoDB/AgentCore 资源
   均可切换，代价是给相关角色增加精确限定到该密钥 ARN 的 KMS 权限。
6. **所有平台角色都可加权限边界**，保证未来任何代码改动都不能越过边界扩权。

另见第 9.5 节：用户可在自己 session 内直接调用 loopback 端口消耗自己的配额，
这是设计预期而非越权。

给安全团队一个可核对的交付物：`terraform/` 下的模块源码即完整的 IAM 声明，
`terraform plan` 与 `terraform show` 可逐条比对本文和 permissions.md 的描述，
无需授予任何推测性权限。其中与本节最相关的两处是
`terraform/modules/runtime/iam.tf`（内核角色，可核对其**没有**网关密钥读权）
和 `terraform/modules/llm_edge/iam.tf`（唯一持有该读权的角色）。

---

## 12. 官方文档引用汇总

**隔离与 session**

- [Security best practices for AgentCore Runtime][bp]：microVM 隔离边界、
  内存清理、session-to-user 映射的责任划分、执行角色凭证暴露面
- [Use isolated sessions for agents][sessions]：session 隔离模型的完整定义
- [Quotas for Amazon Bedrock AgentCore][quotas] / [Lifecycle settings][lifecycle]：
  空闲 15 分钟、最长 8 小时的生命周期参数
- [Securely launch and scale your agents on AgentCore Runtime][scale]：
  session 三态生命周期（Active/Idle/Terminated）

**网络**

- [Configure AgentCore Runtime and tools for VPC][vpc]：VPC 模式、私有子网 +
  NAT 的官方架构
- [Network connectivity patterns for agents on AgentCore Runtime][netblog]：
  四种网络形态（含全私有形态）

**身份与鉴权**

- [Authenticate and authorize with Inbound Auth and Outbound Auth][oauth]：
  SigV4 默认入站鉴权与 JWT 方案
- [Configure inbound JWT authorizer][jwt]：JWT authorizer 的
  issuer/audience/client/scope 校验

**数据保护与参考架构**

- [Data encryption][enc]：AgentCore 静态/传输加密与 CMK 支持
- [AWS Security Reference Architecture for Generative AI — agents][sra]：
  AgentCore 各组件在 AWS 安全参考架构中的定位

[bp]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html
[sessions]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html
[quotas]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html
[lifecycle]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html
[vpc]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html
[oauth]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html
[jwt]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/inbound-jwt-authorizer.html
[enc]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/data-encryption.html
[sra]: https://docs.aws.amazon.com/prescriptive-guidance/latest/security-reference-architecture-generative-ai/gen-auto-agents.html
[netblog]: https://aws.amazon.com/blogs/networking-and-content-delivery/network-connectivity-patterns-for-agents-deployed-on-amazon-bedrock-agentcore-runtime/
[scale]: https://aws.amazon.com/blogs/machine-learning/securely-launch-and-scale-your-agents-and-tools-on-amazon-bedrock-agentcore-runtime/
