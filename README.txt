Rivet 编程智能体

Git 仓库：<提交前填写公开仓库地址>

运行环境：Python 3.10 及以上，无运行时第三方依赖。

安装与运行：
1. 创建并激活虚拟环境：python -m venv .venv。
2. 安装项目：python -m pip install -e .。
3. 通过环境变量设置 RIVET_API_KEY 和 RIVET_MODEL；使用兼容网关时另设 RIVET_BASE_URL。
4. 执行任务：rivet -w 待修改的项目目录 "用自然语言描述编程任务"。

示例项目位于 examples/demo_project。可运行：
rivet -w examples/demo_project "修复 calculator.py 中的 mean 函数，并运行测试验证"

特色功能：Rivet 不使用 agent 框架或 SDK，直接实现 OpenAI 兼容的 Chat Completions 工具调用协议。核心循环负责上下文管理、模型输出解析、工具调度、结果回传和终止判断。本地工具支持目录遍历、文件读写、文本搜索、精确替换和命令执行。长对话压缩会保留完整的工具调用-结果配对；网络异常会退避重试；空回复、重复调用和最大步数均有终止策略。文件访问限制在指定工作区，写入采用原子替换，命令输出和运行时间均受限制。safe、ask 和 never 三种模式分别适用于自动执行、逐项确认和只读检查。

架构设计与安全边界见 docs/ARCHITECTURE.md。API key 仅通过环境变量或未入库配置文件提供，不得写入仓库或展示材料。

