<div align="center">

# 🤖 build_a_product_agent

**构建生产级 AI Agent · 中文技术手册**

*系统讲解如何从零设计、实现、测试并运维一套生产级 Agent 系统*

<br/>

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/ADW-19/build_a_product_agent?style=flat&logo=github&color=yellow)](https://github.com/ADW-19/build_a_product_agent/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/ADW-19/build_a_product_agent?logo=git&color=orange)](https://github.com/ADW-19/build_a_product_agent/commits/main)
[![Docs](https://img.shields.io/badge/文档-简体中文-red?logo=markdown&logoColor=white)](./README.md)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/ADW-19/build_a_product_agent/pulls)

<br/>

[快速开始](#快速开始) · [学习路线](#学习路线) · [内容总览](#内容总览) · [技术栈](#技术栈) · [English](./README-en.md)

</div>

---

## 项目简介

这是一个**纯文档项目**，专注于 AI Agent 后端开发的核心知识点。每章都是一篇独立成文的技术文章，覆盖：

| | | | |
|:---:|:---:|:---:|:---:|
| 🏗️<br/>**技术选型与架构设计** | 🧩<br/>**关键模块开发** | 🤖<br/>**Agent 通路与多 Agent 协作** | 🧪<br/>**系统测试与上线验证** |
| | 🖥️<br/>**AI Infra 基座与模型选型** | | |

**这本手册和其他教程的区别：**

| 💡 | 说明 |
|:---:|:---|
| 🏭 **工业界标准** | 每个主题都从生产环境的真实要求出发，而不是玩具 Demo |
| ❌ **先看错误示范** | 先展示"课堂典型写法"为什么在生产上会出事，再讲正确做法——强约定 |
| ✅ **完整可运行代码** | 所有示例均为 Python 3.13+ 异步风格，可直接落地到项目骨架 |
| 🧪 **测试与运维并重** | 不止"怎么写"，还讲"怎么测"和"怎么运维" |

> 🎯 **目标读者**：具备一定编程基础的开发者。全书以 **「工业界标准 → 为什么课堂不教 → 你应该怎么写」** 为叙事线。

> [!IMPORTANT]
> **版本说明**：本文档基于作者开发实战经验创作，将不定期持续更新。作者母语为中文，目前主要面向中国大陆、港澳台、新加坡等地区的技术人士；若国际读者需求较多，后续再考虑推出完整英文版。

---

## 学习路线

```mermaid
flowchart LR
    subgraph DEV ["🛠️ 开发篇"]
        direction LR
        A["🏗️ 第1章<br/>技术选型"] --> B["📐 第2章<br/>开发基本要求"] --> C["🧩 第3章<br/>模块开发"] --> D["🤖 第4章<br/>Agent 通路"]
    end
    subgraph OPS ["🚀 测试与运维篇"]
        direction LR
        E["🧪 第5章<br/>系统测试"] --> F["🖥️ 第6章<br/>AI Infra 基座"]
    end
    D --> E
```

---

## 快速开始

```bash
# 克隆项目
git clone https://github.com/ADW-19/build_a_product_agent.git
cd build_a_product_agent

# 阅读顺序建议（按上图路线）
# 1. 第1章：技术选型（了解整体技术栈）
# 2. 第2章：开发基本要求（编码规范）
# 3. 第3章：模块开发（深入实现细节）
# 4. 第4章：Agent通路（单Agent与多Agent协作）
# 5. 第5章：系统测试（上线前的测试体系）
# 6. 第6章：AI Infra 基座（模型部署与选型）
```

---

## 内容总览

| 章节 | 主题 | 一句话看点 |
|:---:|:---|:---|
| 🏗️ **第1章** | 技术选型 | 语言 / 框架 / 中间件 / 协议 / 运维架构的全套选型逻辑 |
| 📐 **第2章** | 开发基本要求 | `.env`、Redis、async、日志、异常处理、类型注解的生产级习惯 |
| 🧩 **第3章** | 模块开发 | 对话接口 / 记忆 / 工具 / 工作流 / RAG 五大模块逐个击破 |
| 🤖 **第4章** | Agent 通路 | 单 Agent 完整通路 + Multi-Agent（A2A 协议）协作机制 |
| 🧪 **第5章** | 系统测试 | 功能 / 质量 / 安全 / 性能 / 上线五维测试体系 |
| 🖥️ **第6章** | AI Infra 基座 | 模型部署运维 + 三层模型选型架构 |

<details>
<summary>🏗️ <b>第1章：技术选型</b>（4 篇）</summary>

| 文件 | 内容 |
|:---|:---|
| `01-技术选型.md` | 语言、框架、Agent 编排工具选型 |
| `02-中间件选型.md` | 数据库、缓存、消息队列选型 |
| `03-协议与架构模式选型.md` | API 协议、通信模式、架构风格 |
| `04-运维架构选型.md` | 部署、监控、日志、容灾 |

</details>

<details>
<summary>📐 <b>第2章：开发基本要求</b>（1 篇）</summary>

| 文件 | 内容 |
|:---|:---|
| `01-开发习惯.md` | 配置管理、Redis、async、日志规范 |

</details>

<details>
<summary>🧩 <b>第3章：模块开发</b>（5 篇）</summary>

| 文件 | 内容 |
|:---|:---|
| `01-对话接口.md` | 流式/非流式、session 隔离、异步高并发 |
| `02-长期记忆与短期记忆.md` | Milvus + Redis 组合使用 |
| `03-工具开发.md` | LangChain tool 定义、调用准确性 |
| `04-工作流.md` | LangGraph StateGraph、结构化输出 |
| `05-RAG系统.md` | 检索增强生成最佳实践 |

</details>

<details>
<summary>🤖 <b>第4章：Agent 通路</b>（2 篇）</summary>

| 文件 | 内容 |
|:---|:---|
| `01-单Agent系统通路.md` | 单 Agent 的完整实现路径 |
| `02-Multi-Agent协作机制.md` | A2A 协议、多 Agent 协作模式 |

</details>

<details>
<summary>🧪 <b>第5章：系统测试</b>（2 篇）</summary>

| 文件 | 内容 |
|:---|:---|
| `01-Agent上线前常用的系统测试方法.md` | 五维测试体系总述（功能/质量/安全/性能/上线），工业界方法全景 |
| `02-RAG系统测试.md` | 以电商客服为例，意图识别、检索召回/精准、生成质量、安全、并发全链路测试 |

</details>

<details>
<summary>🖥️ <b>第6章：AI Infra 基座</b>（2 篇）</summary>

| 文件 | 内容 |
|:---|:---|
| `01-模型底座运维基础知识与架构选型.md` | supervisorctl 进程管控、主从降级、Docker+K8s、VLLM/SGlang 选型 |
| `02-模型选型-不同智能体如何搭配底层模型.md` | 参数×生态×性能三维选型框架，以编码智能体为例的全流程模型搭配实战 |

</details>

---

## 技术栈

> 全书示例代码围绕以下技术栈展开，版本为手册的最低基准：

| 领域 | 选型 | 版本要求 |
|:---:|:---:|:---:|
| 语言 | ![Python](https://img.shields.io/badge/Python-3.13+-3776AB?style=flat&logo=python&logoColor=white) | 3.13+ |
| Web 框架 | ![FastAPI](https://img.shields.io/badge/FastAPI-%E2%89%A50.136-009688?style=flat&logo=fastapi&logoColor=white) | ≥ 0.136 |
| Agent 编排 | ![LangGraph](https://img.shields.io/badge/LangGraph-%E2%89%A51.2-1C3C3C?style=flat&logo=langchain&logoColor=white) | ≥ 1.2 |
| LLM 工具层 | ![LangChain](https://img.shields.io/badge/LangChain-%E2%89%A51.3-1C3C3C?style=flat&logo=langchain&logoColor=white) | ≥ 1.3 |
| 数据校验 | ![Pydantic](https://img.shields.io/badge/Pydantic-%E2%89%A52.13-E92063?style=flat&logo=pydantic&logoColor=white) | ≥ 2.13 |
| 业务数据库 | ![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=flat&logo=postgresql&logoColor=white) | — |
| 缓存 / 会话 / 限流 | ![Redis](https://img.shields.io/badge/Redis-DC382D?style=flat&logo=redis&logoColor=white) | — |
| 向量库（长期记忆） | ![Milvus](https://img.shields.io/badge/Milvus-00A1F0?style=flat&logo=milvus&logoColor=white) | — |
| 消息队列 | ![RabbitMQ](https://img.shields.io/badge/RabbitMQ-FF6600?style=flat&logo=rabbitmq&logoColor=white) | — |

---

## 目录结构

<details>
<summary>📁 点击展开完整目录树</summary>

```text
docs/
├── 01-首页/
│   ├── README.md
│   └── README-en.md
│
├── 02-生产级开发-通用知识/
│   ├── 第1章：技术选型/
│   │   ├── 01-技术选型.md
│   │   ├── 02-中间件选型.md
│   │   ├── 03-协议与架构模式选型.md
│   │   └── 04-运维架构选型.md
│   │
│   ├── 第2章：开发基本要求/
│   │   └── 01-开发习惯.md
│   │
│   ├── 第3章：模块开发/
│   │   ├── 01-对话接口（流式+非流式参数切换，session_id隔离，接口异步高性能处理）.md
│   │   ├── 02-长期记忆与短期记忆.md
│   │   ├── 03-工具开发.md
│   │   ├── 04-工作流.md
│   │   └── 05-RAG系统.md
│   │
│   └── 第4章：Agent通路/
│       ├── 01-单Agent系统通路.md
│       └── 02-Multi-Agent协作机制（A2A协议）.md
│
├── 03-生产级测试-系统测试/
│   └── 第5章：系统测试/
│       ├── 01-Agent上线前常用的系统测试方法.md
│       └── 02-RAG系统测试.md
│
└── 04-生产级AI Infra-基座与运维/
    └── 第6章：AI Infra基础知识/
        ├── 01-模型底座运维基础知识与架构选型.md
        └── 02-模型选型-不同智能体如何搭配底层模型.md
```

</details>

---

## 👤 作者与社区

| | |
|:---:|:---|
| ✍️ **作者** | Andy Yanqi Wang (ADW-19) · 中国上海 |
| 📕 **小红书** | ID：`ADW_AI` |

欢迎通过 Issue 提出建议、勘误，或直接提交 PR。

---

## 许可证

<div align="center">

本项目采用 [MIT License](./LICENSE) 开源。

如果这份手册对你有帮助，欢迎点一个 **Star ⭐** 让更多人看到！

[![GitHub Stars](https://img.shields.io/github/stars/ADW-19/build_a_product_agent?style=flat&logo=github&color=yellow)](https://github.com/ADW-19/build_a_product_agent/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

</div>
