"use strict";

const token = document.querySelector('meta[name="rivet-token"]').content;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const ui = {
  shell: $("#appShell"),
  sidebar: $("#sidebar"),
  inspector: $("#inspector"),
  backdrop: $("#mobileBackdrop"),
  conversation: $("#conversation"),
  empty: $("#emptyState"),
  messages: $("#messageList"),
  form: $("#composerForm"),
  input: $("#messageInput"),
  send: $("#sendButton"),
  newSession: $("#newSessionButton"),
  sessions: $("#sessionList"),
  sessionCount: $("#sessionCount"),
  workspaceName: $("#workspaceName"),
  workspacePath: $("#workspacePath"),
  modelName: $("#modelName"),
  modelProtocol: $("#modelProtocol"),
  connectionDot: $("#connectionDot"),
  pageTitle: $("#pageTitle"),
  pageMeta: $("#pageMeta"),
  runStatus: $("#runStatus"),
  runStatusText: $("#runStatusText"),
  turnSummary: $("#turnSummary"),
  planEmpty: $("#planEmpty"),
  planList: $("#planList"),
  inspectedCount: $("#inspectedCount"),
  changedCount: $("#changedCount"),
  changedFiles: $("#changedFiles"),
  verificationCard: $("#verificationCard"),
  verificationTitle: $("#verificationTitle"),
  verificationDetail: $("#verificationDetail"),
  activity: $("#activityList"),
  approvalModal: $("#approvalModal"),
  approvalTool: $("#approvalTool"),
  approvalSummary: $("#approvalSummary"),
  approve: $("#approveButton"),
  reject: $("#rejectApprovalButton"),
  diffModal: $("#diffModal"),
  diffContent: $("#diffContent"),
  toasts: $("#toastRegion"),
};

let currentSnapshot = null;
let busy = false;
let activeAssistant = null;
let lastAssistantText = "";
let pendingApproval = null;
let activityStarted = false;
const runningTools = [];

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Rivet-Token", token);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      message = payload.error || message;
    } catch (_) {
      // Keep the stable status message.
    }
    throw new Error(message);
  }
  return response;
}

async function jsonRequest(path, options = {}) {
  const response = await request(path, options);
  return response.json();
}

function setBusy(value, label = "就绪") {
  busy = value;
  ui.send.disabled = value;
  ui.newSession.disabled = value;
  ui.input.disabled = value;
  ui.runStatus.className = `run-status ${value ? "working" : "idle"}`;
  ui.runStatusText.textContent = value ? label : "就绪";
  if (!value) {
    ui.input.disabled = false;
    ui.input.focus();
  }
}

function setRunError(message) {
  busy = false;
  ui.send.disabled = false;
  ui.newSession.disabled = false;
  ui.input.disabled = false;
  ui.runStatus.className = "run-status error";
  ui.runStatusText.textContent = "已停止";
  toast(message, "error");
}

function toast(message, kind = "info") {
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  item.textContent = message;
  ui.toasts.append(item);
  window.setTimeout(() => item.remove(), 4400);
}

function scrollToBottom() {
  ui.conversation.scrollTop = ui.conversation.scrollHeight;
}

function showConversation() {
  ui.empty.classList.add("hidden");
}

function addMessage(role, text, { streaming = false } = {}) {
  showConversation();
  const article = document.createElement("article");
  article.className = `message ${role}${streaming ? " streaming" : ""}`;

  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.textContent = role === "assistant" ? "R" : "你";

  const content = document.createElement("div");
  content.className = "message-content";
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "assistant" ? "Rivet" : "You";
  const body = document.createElement("div");
  body.className = "message-text";
  body.textContent = text;
  content.append(label, body);
  article.append(avatar, content);
  ui.messages.append(article);
  scrollToBottom();
  return { article, body };
}

function addSystemNote(text) {
  showConversation();
  const note = document.createElement("div");
  note.className = "system-note";
  note.textContent = text;
  ui.messages.append(note);
  scrollToBottom();
}

function summarizeArguments(raw) {
  try {
    const data = JSON.parse(raw);
    const candidates = [data.path, data.command, data.query, data.explanation];
    const primary = candidates.find((value) => typeof value === "string" && value.trim());
    return primary ? primary.replace(/\s+/g, " ").slice(0, 130) : raw.slice(0, 130);
  } catch (_) {
    return String(raw || "").replace(/\s+/g, " ").slice(0, 130);
  }
}

function toolLabel(name) {
  const labels = {
    update_plan: "更新任务计划",
    list_files: "浏览文件",
    read_file: "读取文件",
    search_text: "搜索代码",
    write_file: "写入文件",
    replace_text: "修改文件",
    show_diff: "检查改动",
    run_command: "运行命令",
  };
  return labels[name] || name;
}

function addToolCard(data) {
  showConversation();
  const card = document.createElement("div");
  card.className = "tool-card running";
  card.dataset.name = data.name;
  const header = document.createElement("div");
  header.className = "tool-card-header";
  const title = document.createElement("strong");
  title.textContent = toolLabel(data.name);
  const status = document.createElement("span");
  status.textContent = "执行中";
  header.append(title, status);
  const detail = document.createElement("div");
  detail.className = "tool-card-detail";
  detail.textContent = summarizeArguments(data.arguments);
  card.append(header, detail);
  ui.messages.append(card);
  runningTools.push({ name: data.name, card, status, detail });
  addActivity(toolLabel(data.name), summarizeArguments(data.arguments), "tool");
  scrollToBottom();
}

function finishToolCard(data) {
  const index = runningTools.findIndex((item) => item.name === data.name);
  if (index < 0) return;
  const item = runningTools.splice(index, 1)[0];
  let payload = null;
  try {
    payload = JSON.parse(data.result);
  } catch (_) {
    payload = { ok: false, error: "无法解析工具结果" };
  }
  const ok = payload && payload.ok !== false;
  item.card.classList.remove("running");
  item.card.classList.add(ok ? "success" : "failed");
  item.status.textContent = ok ? "完成" : "失败";
  const resultDetail = payload.error || payload.path || payload.command || "操作已返回结果";
  if (resultDetail) item.detail.textContent = String(resultDetail).replace(/\s+/g, " ").slice(0, 180);
  addActivity(toolLabel(data.name), ok ? "操作完成" : String(payload.error || "操作失败"), ok ? "success" : "error");
}

function addActivity(title, detail, kind = "") {
  if (!activityStarted) {
    ui.activity.replaceChildren();
    activityStarted = true;
  }
  const item = document.createElement("div");
  item.className = `activity-item ${kind}`;
  const strong = document.createElement("strong");
  strong.textContent = title;
  const span = document.createElement("span");
  span.textContent = detail;
  item.append(strong, span);
  ui.activity.append(item);
}

function renderSnapshot(snapshot, { renderMessages = false } = {}) {
  currentSnapshot = snapshot;
  const config = snapshot.config || {};
  const status = snapshot.status || {};
  ui.workspaceName.textContent = config.workspace_name || "Workspace";
  ui.workspacePath.textContent = config.workspace || "";
  ui.workspaceName.title = config.workspace || config.workspace_name || "本地工作目录";
  ui.workspacePath.title = config.workspace || "";
  ui.modelName.textContent = config.model || "model";
  ui.modelProtocol.textContent = protocolLabel(config.protocol);
  ui.connectionDot.classList.add("online");
  const activeSession = (snapshot.sessions || []).find((session) => session.session_id === snapshot.session_id);
  ui.pageTitle.textContent = activeSession ? activeSession.task_preview || "未命名会话" : "新会话";
  ui.pageTitle.title = activeSession ? activeSession.task_preview || "未命名会话" : "新会话";
  ui.pageMeta.textContent = `${config.workspace_name || "Workspace"} · ${config.approval || "safe"} 模式`;
  ui.turnSummary.textContent = `${status.turns || 0} 轮 · ${status.total_steps || 0} 个步骤`;
  renderPlan(snapshot.plan || status.plan || {});
  renderChanges(status, snapshot.diff || {});
  renderSessions(snapshot.sessions || [], snapshot.session_id);
  if (renderMessages) renderConversation(snapshot.conversation || []);
}

function protocolLabel(protocol) {
  const labels = {
    openai_chat: "OpenAI Chat 兼容接口",
    openai_responses: "OpenAI Responses 兼容接口",
    anthropic: "Anthropic 兼容接口",
  };
  return labels[protocol] || String(protocol || "本地模型接口").replaceAll("_", " ");
}

function renderConversation(messages) {
  ui.messages.replaceChildren();
  activeAssistant = null;
  lastAssistantText = "";
  if (!messages.length) {
    ui.empty.classList.remove("hidden");
    return;
  }
  ui.empty.classList.add("hidden");
  for (const message of messages) {
    addMessage(message.role === "assistant" ? "assistant" : "user", message.content || "");
    if (message.role === "assistant") lastAssistantText = message.content || "";
  }
  scrollToBottom();
}

function renderPlan(plan) {
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  ui.planList.replaceChildren();
  ui.planEmpty.hidden = steps.length > 0;
  ui.planList.hidden = steps.length === 0;
  const statusNames = {
    pending: "等待中",
    in_progress: "进行中",
    completed: "已完成",
    blocked: "已阻塞",
  };
  steps.forEach((step, index) => {
    const item = document.createElement("li");
    item.className = `plan-item ${step.status || "pending"}`;
    const marker = document.createElement("div");
    marker.className = "plan-marker";
    marker.textContent = step.status === "completed" ? "✓" : String(index + 1);
    const copy = document.createElement("div");
    copy.className = "plan-copy";
    const title = document.createElement("strong");
    title.textContent = step.step || "未命名步骤";
    const state = document.createElement("span");
    state.textContent = statusNames[step.status] || step.status || "等待中";
    copy.append(title, state);
    item.append(marker, copy);
    ui.planList.append(item);
  });
}

function renderChanges(status, diff) {
  const inspected = Array.isArray(status.inspected_files) ? status.inspected_files : [];
  const changed = Array.isArray(status.changed_files) ? status.changed_files : [];
  ui.inspectedCount.textContent = String(inspected.length);
  ui.changedCount.textContent = String(changed.length);
  ui.changedFiles.replaceChildren();
  for (const path of changed) {
    const row = document.createElement("div");
    row.className = "file-row";
    const name = document.createElement("span");
    name.textContent = path;
    name.title = path;
    row.append(name);
    ui.changedFiles.append(row);
  }
  ui.verificationCard.className = "verification-card neutral";
  if (!status.verification_required) {
    ui.verificationTitle.textContent = "无需验证";
    ui.verificationDetail.textContent = changed.length ? "当前状态不需要额外检查" : "还没有文件修改";
    ui.verificationCard.querySelector(".verification-icon").textContent = "·";
  } else if (status.verification_passed) {
    ui.verificationCard.classList.add("passed");
    ui.verificationTitle.textContent = "验证通过";
    ui.verificationDetail.textContent = "最新修改之后的检查已成功";
    ui.verificationCard.querySelector(".verification-icon").textContent = "✓";
  } else {
    ui.verificationCard.classList.add("pending");
    ui.verificationTitle.textContent = "等待验证";
    ui.verificationDetail.textContent = "修改完成后需要运行检查";
    ui.verificationCard.querySelector(".verification-icon").textContent = "!";
  }
  if (diff && diff.truncated) toast("Diff 内容较长，界面仅显示截断结果", "warning");
}

function renderSessions(sessions, activeId) {
  ui.sessions.replaceChildren();
  ui.sessionCount.textContent = String(sessions.length);
  if (!sessions.length) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = "完成第一个任务后，会话会自动保存在这里。";
    ui.sessions.append(empty);
    return;
  }
  sessions.forEach((session) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `session-item${session.session_id === activeId ? " active" : ""}`;
    button.dataset.sessionId = session.session_id;
    button.title = `${session.task_preview || "未命名会话"} · ${session.turns} 轮 · ${session.total_steps} 步`;
    const title = document.createElement("strong");
    title.textContent = session.task_preview || "未命名会话";
    const meta = document.createElement("span");
    meta.textContent = `${session.turns} 轮 · ${session.total_steps} 步`;
    button.append(title, meta);
    button.addEventListener("click", () => resumeSession(session.session_id));
    ui.sessions.append(button);
  });
}

function switchTab(name) {
  $$(".tab").forEach((tab) => {
    const selected = tab.dataset.tab === name;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
  });
  $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
}

function openApproval(data) {
  pendingApproval = data;
  ui.approvalTool.textContent = toolLabel(data.tool);
  ui.approvalSummary.textContent = data.summary || "该操作会修改本地状态。";
  ui.approvalModal.hidden = false;
  ui.reject.focus();
}

async function answerApproval(approved) {
  if (!pendingApproval) return;
  const data = pendingApproval;
  pendingApproval = null;
  ui.approvalModal.hidden = true;
  try {
    await jsonRequest("/api/approval", {
      method: "POST",
      body: JSON.stringify({ id: data.id, approved }),
    });
    addActivity(approved ? "已允许操作" : "已拒绝操作", toolLabel(data.tool), approved ? "success" : "warning");
  } catch (error) {
    toast(error.message, "error");
  }
}

function handleEvent(event, data) {
  switch (event) {
    case "turn_started":
      addActivity("任务已提交", "开始分析当前工作区");
      break;
    case "model_start":
      setBusy(true, `第 ${data.step} 步`);
      addActivity("模型思考", `第 ${data.step} 步 · 第 ${data.turn} 轮`);
      break;
    case "assistant_stream_start":
      activeAssistant = addMessage("assistant", "", { streaming: true });
      break;
    case "assistant_text_delta":
      if (!activeAssistant) activeAssistant = addMessage("assistant", "", { streaming: true });
      activeAssistant.body.textContent += data.text || "";
      lastAssistantText = activeAssistant.body.textContent;
      scrollToBottom();
      break;
    case "assistant_stream_end":
      if (activeAssistant) activeAssistant.article.classList.remove("streaming");
      activeAssistant = null;
      break;
    case "assistant_text":
      if (!data.streamed && data.text) {
        addMessage("assistant", data.text);
        lastAssistantText = data.text;
      }
      break;
    case "tool_start":
      addToolCard(data);
      break;
    case "tool_end":
      finishToolCard(data);
      break;
    case "plan_updated":
      renderPlan(data);
      addActivity("计划已更新", `${(data.steps || []).length} 个步骤`, "success");
      break;
    case "context_compacted":
      addSystemNote(`上下文已压缩为 ${data.messages} 条消息`);
      addActivity("上下文压缩", `保留 ${data.messages} 条消息`);
      break;
    case "verification_required":
      addSystemNote("文件已修改，Rivet 正在执行必要的验证");
      addActivity("等待验证", `${(data.files || []).length} 个文件`, "warning");
      break;
    case "plan_completion_required":
      addActivity("计划尚未完成", "继续执行剩余步骤", "warning");
      break;
    case "approval_required":
      openApproval(data);
      addActivity("等待你的确认", toolLabel(data.tool), "warning");
      break;
    case "session_saved":
      toast("会话已自动保存");
      break;
    case "session_error":
      toast(data.message || "会话保存失败", "error");
      break;
    case "cancelled":
      addSystemNote(`操作已取消：${data.phase || "当前步骤"}`);
      break;
    case "completed":
      addActivity("任务完成", "结果已生成", "success");
      break;
    case "stopped":
      addActivity("任务停止", data.reason || "未完成", "error");
      break;
    default:
      break;
  }
}

function handleRecord(record) {
  if (record.type === "event") {
    handleEvent(record.event, record.data || {});
    return;
  }
  if (record.type === "fatal_error") {
    setRunError(record.message || "任务执行失败");
    return;
  }
  if (record.type === "turn_complete") {
    const result = record.result || {};
    if (result.final && result.final.trim() !== lastAssistantText.trim()) {
      addMessage("assistant", result.final);
      lastAssistantText = result.final;
    }
    if (record.snapshot) renderSnapshot(record.snapshot);
    setBusy(false);
    ui.runStatusText.textContent = result.success ? "已完成" : "已停止";
    window.setTimeout(() => {
      if (!busy) ui.runStatusText.textContent = "就绪";
    }, 1800);
  }
}

async function sendTurn(message) {
  const task = message.trim();
  if (!task || busy) return;
  addMessage("user", task);
  lastAssistantText = "";
  activeAssistant = null;
  ui.input.value = "";
  resizeComposer();
  setBusy(true, "正在开始");
  try {
    const response = await request("/api/turn", {
      method: "POST",
      body: JSON.stringify({ message: task }),
    });
    if (!response.body) throw new Error("浏览器不支持流式响应");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        handleRecord(JSON.parse(line));
      }
      if (done) break;
    }
    if (buffer.trim()) handleRecord(JSON.parse(buffer));
  } catch (error) {
    setRunError(error.message || "无法完成任务");
  } finally {
    if (busy) setBusy(false);
  }
}

async function newSession() {
  if (busy) return;
  try {
    const snapshot = await jsonRequest("/api/session/new", {
      method: "POST",
      body: "{}",
    });
    activityStarted = false;
    ui.activity.replaceChildren();
    renderSnapshot(snapshot, { renderMessages: true });
    closeMobilePanels();
    toast("已创建新的空白会话");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function resumeSession(id) {
  if (busy) return;
  try {
    const snapshot = await jsonRequest("/api/session/resume", {
      method: "POST",
      body: JSON.stringify({ id }),
    });
    activityStarted = false;
    ui.activity.replaceChildren();
    renderSnapshot(snapshot, { renderMessages: true });
    closeMobilePanels();
    const drifted = snapshot.resume && snapshot.resume.drifted ? snapshot.resume.drifted.length : 0;
    toast(drifted ? `会话已恢复；${drifted} 个文件在关闭期间发生变化` : "会话已恢复", drifted ? "warning" : "info");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function showDiff() {
  ui.diffModal.hidden = false;
  ui.diffContent.textContent = "正在读取…";
  try {
    const diff = await jsonRequest("/api/diff");
    ui.diffContent.textContent = diff.diff || "本次会话尚无文件改动。";
  } catch (error) {
    ui.diffContent.textContent = error.message;
  }
}

function resizeComposer() {
  ui.input.style.height = "auto";
  ui.input.style.height = `${Math.min(ui.input.scrollHeight, 170)}px`;
}

function closeMobilePanels() {
  ui.sidebar.classList.remove("open");
  ui.inspector.classList.remove("open");
  ui.backdrop.classList.remove("visible");
}

function openMobilePanel(panel) {
  closeMobilePanels();
  panel.classList.add("open");
  ui.backdrop.classList.add("visible");
}

async function bootstrap() {
  try {
    const snapshot = await jsonRequest("/api/bootstrap");
    renderSnapshot(snapshot, { renderMessages: true });
    setBusy(Boolean(snapshot.busy), snapshot.busy ? "正在运行" : "就绪");
  } catch (error) {
    setRunError(`无法连接本地 Rivet：${error.message}`);
  }
}

ui.form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendTurn(ui.input.value);
});

ui.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    ui.form.requestSubmit();
  }
});
ui.input.addEventListener("input", resizeComposer);
ui.newSession.addEventListener("click", newSession);
$("#refreshSessionsButton").addEventListener("click", bootstrap);
$("#diffButton").addEventListener("click", showDiff);
$("#closeDiffButton").addEventListener("click", () => { ui.diffModal.hidden = true; });
ui.diffModal.addEventListener("click", (event) => {
  if (event.target === ui.diffModal) ui.diffModal.hidden = true;
});
ui.approve.addEventListener("click", () => answerApproval(true));
ui.reject.addEventListener("click", () => answerApproval(false));
$("#sidebarToggle").addEventListener("click", () => openMobilePanel(ui.sidebar));
$("#inspectorToggle").addEventListener("click", () => openMobilePanel(ui.inspector));
$("#inspectorClose").addEventListener("click", closeMobilePanels);
ui.backdrop.addEventListener("click", closeMobilePanels);

$$('.tab').forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!ui.approvalModal.hidden) {
    answerApproval(false);
  } else if (!ui.diffModal.hidden) {
    ui.diffModal.hidden = true;
  } else {
    closeMobilePanels();
  }
});

window.addEventListener("resize", () => {
  if (window.innerWidth > 1180) closeMobilePanels();
});

bootstrap();
