CAworker 本地编程智能体
=======================

项目地址：https://github.com/STL250/agent
运行环境：Python 3.10 及以上
开源协议：MIT License


一、项目简介
------------

CAworker 是一个面向本地代码仓库的多轮编程 Agent。项目不依赖 LangChain 等
Agent 框架，而是直接实现模型通信协议、ReAct 工具循环、上下文管理、会话持久化、
权限控制和本地工具执行。

用户可以通过自然语言描述任务，CAworker 会读取当前项目、制定计划、调用工具修改
文件、执行验证命令，并把结果持续反馈到终端界面或本地 Web 界面。

项目的展示名称为 CAworker。为保持已有安装方式兼容，Python 包和命令行入口仍使用
rivet。


二、核心功能
------------

1. 原生 Agent 循环

   直接实现 OpenAI-compatible Chat Completions、SSE 流式输出和 Function Calling。
   模型产生工具调用后，本地程序负责解析参数、执行工具，并携带原始调用 ID 把结果
   重新加入消息历史，形成完整的“思考—行动—观察”循环。

2. 主 Agent 与只读子 Agent

   主 Agent 负责理解任务、整合信息、修改文件和验证结果。Explorer 子 Agent 负责
   搜索与梳理项目，Reviewer 子 Agent 负责代码审查与风险分析。两个子 Agent 使用
   独立上下文且保持只读，不能修改文件、运行命令或继续创建下级 Agent。

3. 多轮会话与持久化

   用户可以在上一轮任务完成后继续补充要求。每轮结束后，会话原子保存到当前项目的
   .rivet/sessions 目录，可恢复消息、计划、上下文归档、子 Agent 报告、文件 Diff、
   修改记录和验证状态。

4. 分层上下文压缩

   长对话超过阈值后，较早消息会被归档并转换为固定结构的事实摘要，近期消息继续
   保留原文。Function Call 与对应的 Tool Result 始终作为不可拆分的整体，避免出现
   孤立调用。

   被归档的原文仍可通过普通关键词相关度检索。检索同时考虑完整短语和各关键词的
   出现次数，不使用 Embedding、向量数据库或 RAG。

5. 修改、验证与完成闭环

   文件修改会使之前的验证证据失效。只有在最新修改之后成功执行测试、编译、静态
   检查等验证命令，任务才会被标记为“已验证完成”。验证来自真实命令结果，而不是
   模型口头声称成功。

6. 操作级撤销与失败恢复

   每轮任务会形成一个可撤销检查点。用户可以按时间倒序撤销该轮产生的文件变化；
   如果文件后来又被其他操作修改，系统会拒绝不安全的覆盖。失败任务可以恢复到本轮
   检查点后重新执行。

7. 三层 Skills

   支持内置 Skill、用户级 Skill 和项目级 Skill。模型启动时只接收名称和描述，
   明确匹配当前任务后才加载 SKILL.md 正文，减少无关上下文占用。同名 Skill 的
   优先级为：项目级 > 用户级 > 内置。

8. TUI 与 Web UI

   TUI 提供命令菜单、流式输出和动态工作状态。Web UI 提供会话管理、聊天、计划、
   文件树、行级 Diff、工具活动、Skills、子 Agent、上下文压缩、权限切换、操作撤销
   和失败恢复等可视化功能。

9. 安全边界

   文件访问被限制在当前工作区；写入采用原子替换；命令具有超时、输出上限和危险
   操作拦截；模型参数会进行本地二次校验；API Key 和敏感 Header 不会写入会话或
   发送到浏览器。


三、总体架构
------------

                       +----------------------+
                       |      用户任务        |
                       +----------+-----------+
                                  |
                       +----------v-----------+
                       |    TUI / Web UI       |
                       +----------+-----------+
                                  |
                       +----------v-----------+
                       |       主 Agent        |
                       |  ReAct / 计划 / 决策  |
                       +----+-----------+------+
                            |           |
              +-------------+           +----------------+
              |                                              |
    +---------v----------+                        +----------v---------+
    | 只读子 Agent       |                        |   Tool Registry     |
    | Explorer/Reviewer  |                        +----------+---------+
    +---------+----------+                                   |
              |                                     +--------v---------+
              +--结构化报告--> 主 Agent             |    Workspace     |
                                                    +--------+---------+
                                                             |
                                                    +--------v---------+
                                                    | 本地文件与命令   |
                                                    +------------------+

主 Agent 同时连接以下状态模块：

  · ContextManager：管理近期消息、结构化摘要和归档历史；
  · PlanState：保存可校验的任务步骤和执行状态；
  · SessionStore：保存并恢复项目级会话；
  · SkillRegistry：发现、选择和按需加载 Skills；
  · ModelClient：隔离模型厂商和协议实现。


四、任务执行流程
----------------

1. 用户通过 TUI 或 Web UI 输入自然语言任务。
2. Agent 组合系统约束、当前任务、近期消息、历史摘要和工具定义。
3. 模型决定直接回答、制定计划、激活 Skill、委派只读子任务或调用工具。
4. 工具参数经过 Schema、权限和工作区边界校验。
5. Workspace 执行文件或命令操作，并返回结构化观察结果。
6. 观察结果追加到消息历史，模型基于最新状态继续推理。
7. 产生代码修改后，Agent 通过后续命令收集验证证据。
8. 任务结束后保存会话、计划、Diff、撤销检查点和验证状态。


五、安装与配置
--------------

1. 安装项目

   git clone https://github.com/STL250/agent.git
   cd agent
   python -m pip install -e .

2. 创建本地配置

   Windows PowerShell：

   Copy-Item .env.example .env

   Linux 或 macOS：

   cp .env.example .env

3. 填写模型配置

   RIVET_PROTOCOL=openai_chat
   RIVET_BASE_URL=https://provider.example/v1
   RIVET_ENDPOINT_PATH=/chat/completions
   RIVET_MODEL=your-model-name
   RIVET_API_KEY=your-api-key
   RIVET_AUTH_STYLE=bearer

   .env 已被 Git 忽略。真实 API Key 不应写入源码、Git 提交或展示材料。

   配置优先级：

   命令行参数 > 进程环境变量 > .env > 默认值

   完整 endpoint、自定义 Header、额外请求字段、无鉴权本地服务等配置可查看：

   docs/PROVIDERS.md

4. 检查模型兼容性

   rivet --check-model

   该命令会实际检查流式文本、Function Calling 和工具结果回传。模型仅支持普通聊天
   并不代表它能够稳定驱动 Coding Agent。


六、启动方式
------------

在需要操作的项目根目录打开终端。

启动终端界面：

   rivet

启动本地 Web 界面：

   rivet --web

显式指定工作区：

   rivet --workspace path/to/project

Web 服务只监听 127.0.0.1，不会把当前项目公开为远程服务。


七、TUI 命令
------------

输入“/”会显示可筛选的命令菜单，支持方向键选择和 Tab 补全。

  /help                         查看命令说明
  /status                       查看会话、计划、上下文和验证状态
  /plan                         查看当前任务计划
  /diff [path]                  查看本次会话产生的文件改动
  /skills                       查看可用及最近使用的 Skills
  /permissions [safe|ask|never] 查看或切换权限模式
  /sessions                     列出最近保存的会话
  /resume [id]                  恢复最近或指定会话
  /new                          新建干净会话
  /exit                         保存会话并退出

任务执行期间按 Ctrl+C 会取消当前模型请求、审批等待或命令，但不会退出整个会话。


八、权限模式
------------

safe：
  自动执行常规安全操作。安装依赖、网络访问、本地 Git 写操作等敏感动作需要确认。

ask：
  文件修改和命令执行等操作逐项确认。

never：
  只读分析，不允许修改文件或执行命令。

无论使用哪种模式，路径逃逸、明显破坏性命令和越权工具参数都会被本地安全逻辑拒绝。


九、Skills 目录
--------------

内置 Skill：
  Python 包内的 src/rivet/builtin_skills

用户级 Skill：
  ~/.rivet/skills

项目级 Skill：
  <workspace>/.rivet/skills

Skill 只提供工作流程和文本参考资源，不能提升权限、突破工作区边界或自动执行附带内容。


十、主要工具
------------

计划工具：
  update_plan

浏览与检索：
  list_files、read_file、search_text、search_history

文件修改：
  write_file、replace_text

改动检查：
  show_diff

命令执行：
  run_command

子任务委派：
  delegate_task、delegate_readonly_tasks

Skills：
  list_skills、activate_skill、read_skill_resource

所有工具参数都会在本地再次校验。run_command 会在执行前后比较工作区快照，因此脚本、
格式化器和代码生成器间接造成的文件变化也会进入 Diff 与验证状态。


十一、项目结构
--------------

agent/
|-- .github/workflows/ci-cd.yml   CI/CD 工作流
|-- docs/
|   |-- ARCHITECTURE.md            架构、安全边界与实现细节
|   `-- PROVIDERS.md               模型服务配置说明
|-- src/rivet/
|   |-- agent.py                   主 ReAct 循环与任务状态
|   |-- client.py                  OpenAI-compatible 模型客户端
|   |-- provider.py                模型客户端工厂
|   |-- context.py                 上下文压缩与历史检索
|   |-- session.py                 会话持久化
|   |-- subagents.py               Explorer / Reviewer 编排
|   |-- skills.py                  三层 Skills 发现与激活
|   |-- tools.py                   工具定义、Schema 与调度
|   |-- workspace.py               文件、命令、Diff、撤销与安全边界
|   |-- tui.py                     终端交互界面
|   |-- web.py                     本地 Web 服务
|   `-- webui/                     Web UI 展示层
|-- .env.example                   模型配置模板
|-- README.md                      GitHub 展示版说明
|-- README.txt                     最终材料纯文本说明
`-- pyproject.toml                 Python 包配置与 CLI 入口


十二、CI/CD
-----------

向 main 分支推送或提交 Pull Request 时，GitHub Actions 会自动执行：

  · Windows / Linux 跨平台检查；
  · Python 3.10 / 3.13 兼容检查；
  · Python 源码编译与 CLI 入口检查；
  · 上下文压缩、历史检索和恢复回归；
  · Web UI JavaScript 语法检查；
  · wheel 与源码包构建和资源完整性检查。

发布版本时，先更新 pyproject.toml 中的 version，再推送对应的 vX.Y.Z 标签。全部
检查通过后，工作流会自动创建 GitHub Release，并上传 wheel 与源码包。CI 不读取
.env，也不需要模型 API Key。


十三、安全与隐私说明
--------------------

  · 文件工具只能访问指定工作区；
  · 写文件采用临时文件和原子替换；
  · 命令具有超时、输出上限和子进程树终止机制；
  · API Key 和敏感 Header 会从诊断信息中脱敏；
  · 凭据特征明显的环境变量不会传递给子进程；
  · .env、会话状态、缓存和构建产物默认不进入 Git；
  · Web UI 不接收模型 API Key；
  · 操作撤销不能保证安全时会拒绝执行，而不是覆盖较新的文件内容。


十四、当前边界
--------------

  · 当前内置客户端面向支持 Chat Completions、SSE 和 Function Calling 的服务；
  · 原生不同协议需要新增 ModelClient 适配器；
  · Web UI 是本地交互界面，不是面向公网的远程控制平台；
  · 子 Agent 有意保持只读，所有修改、整合与验证统一由主 Agent 负责；
  · 验证门禁可以保证存在修改后的成功命令证据，但验证质量仍取决于项目可用的测试、
    编译和静态检查命令。


十五、许可证
------------

本项目使用 MIT License，许可证全文见 LICENSE。
