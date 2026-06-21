const state = {
  sessionId: null,
  sessions: [],
  messages: [],
  pendingApproval: null,
};

const els = {
  status: document.querySelector("#status"),
  sessions: document.querySelector("#sessions"),
  newSession: document.querySelector("#new-session"),
  sessionTitle: document.querySelector("#session-title"),
  sessionId: document.querySelector("#session-id"),
  messages: document.querySelector("#messages"),
  compact: document.querySelector("#compact-session"),
  clear: document.querySelector("#clear-session"),
  form: document.querySelector("#chat-form"),
  input: document.querySelector("#message-input"),
  approval: document.querySelector("#approval"),
  approvalDetail: document.querySelector("#approval-detail"),
  approveTool: document.querySelector("#approve-tool"),
  rejectTool: document.querySelector("#reject-tool"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.message || payload.error || "Request failed");
  }
  return payload;
}

function setStatus(text) {
  els.status.textContent = text;
}

function renderSessions() {
  els.sessions.innerHTML = "";
  if (state.sessions.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No sessions yet";
    els.sessions.append(empty);
    return;
  }

  for (const session of state.sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-button";
    if (session.session_id === state.sessionId) {
      button.classList.add("active");
    }
    button.innerHTML = `<span></span><small></small>`;
    button.querySelector("span").textContent = session.title || "New chat";
    button.querySelector("small").textContent = session.session_id;
    button.addEventListener("click", () => selectSession(session.session_id));
    els.sessions.append(button);
  }
}

function renderMessages() {
  els.messages.innerHTML = "";
  if (state.messages.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Start a conversation with the agent.";
    els.messages.append(empty);
    return;
  }

  for (const message of state.messages) {
    const node = document.createElement("div");
    node.className = `message ${message.role}`;
    node.textContent = message.text;
    els.messages.append(node);
  }
  els.messages.scrollTop = els.messages.scrollHeight;
}

function renderHeader() {
  const current = state.sessions.find((item) => item.session_id === state.sessionId);
  els.sessionTitle.textContent = current?.title || "New chat";
  els.sessionId.textContent = state.sessionId || "No session selected";
}

function renderApproval() {
  if (!state.pendingApproval) {
    els.approval.classList.add("hidden");
    els.approvalDetail.textContent = "";
    return;
  }
  const items = state.pendingApproval.interruptions || [];
  const detail = items
    .map((item) => `${item.qualified_name || item.tool_name || "tool"} ${item.arguments || ""}`)
    .join("\n");
  els.approvalDetail.textContent = detail || "A tool call is waiting for approval.";
  els.approval.classList.remove("hidden");
}

function renderAll() {
  renderSessions();
  renderHeader();
  renderMessages();
  renderApproval();
}

async function loadSessions() {
  const payload = await api("/api/sessions");
  state.sessions = payload.sessions || [];
  if (!state.sessionId && state.sessions.length > 0) {
    state.sessionId = state.sessions[0].session_id;
    await loadSessionItems(state.sessionId);
  }
  renderAll();
}

async function createSession() {
  setStatus("Creating session");
  const payload = await api("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ title: "New chat" }),
  });
  state.sessionId = payload.session.session_id;
  state.messages = [];
  state.pendingApproval = null;
  await loadSessions();
  setStatus("Ready");
}

async function selectSession(sessionId) {
  state.sessionId = sessionId;
  state.pendingApproval = null;
  await loadSessionItems(sessionId);
  renderAll();
}

async function loadSessionItems(sessionId) {
  const payload = await api(`/api/sessions/${encodeURIComponent(sessionId)}/items`);
  state.messages = (payload.items || []).flatMap(itemToMessages);
}

function itemToMessages(item) {
  if (!item || typeof item !== "object") {
    return [];
  }
  if (item.role === "user") {
    return [{ role: "user", text: contentText(item.content) }];
  }
  if (item.role === "assistant" || (item.type === "message" && item.role === "assistant")) {
    return [{ role: "agent", text: contentText(item.content) }];
  }
  if (item.type === "compaction") {
    return [{ role: "system", text: "Earlier context was compacted." }];
  }
  return [];
}

function contentText(content) {
  if (typeof content === "string") {
    return content;
  }
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (!part || typeof part !== "object") {
          return "";
        }
        return part.text || part.refusal || "";
      })
      .filter(Boolean)
      .join("");
  }
  return "";
}

async function sendMessage(event) {
  event.preventDefault();
  const text = els.input.value.trim();
  if (!text) {
    return;
  }
  els.input.value = "";
  state.messages.push({ role: "user", text });
  renderMessages();
  setStatus("Running");

  try {
    const payload = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId, message: text }),
    });
    state.sessionId = payload.session.session_id;
    if (payload.status === "needs_approval") {
      state.pendingApproval = payload;
      state.messages.push({
        role: "system",
        text: "The agent needs approval before running a tool.",
      });
    } else {
      state.pendingApproval = null;
      state.messages.push({ role: "agent", text: payload.output || "" });
    }
    await loadSessions();
    setStatus("Ready");
  } catch (error) {
    state.messages.push({ role: "system", text: error.message });
    setStatus("Error");
  } finally {
    renderAll();
  }
}

async function decideApproval(approve) {
  if (!state.sessionId || !state.pendingApproval) {
    return;
  }
  setStatus(approve ? "Approving" : "Rejecting");
  try {
    const payload = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/approve`, {
      method: "POST",
      body: JSON.stringify({ approve }),
    });
    if (payload.status === "needs_approval") {
      state.pendingApproval = payload;
      state.messages.push({ role: "system", text: "Another approval is required." });
    } else {
      state.pendingApproval = null;
      state.messages.push({ role: "agent", text: payload.output || "" });
    }
    await loadSessions();
    setStatus("Ready");
  } catch (error) {
    state.messages.push({ role: "system", text: error.message });
    setStatus("Error");
  } finally {
    renderAll();
  }
}

async function compactSession() {
  if (!state.sessionId) {
    return;
  }
  setStatus("Compacting");
  try {
    const payload = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/compact`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    state.messages.push({
      role: "system",
      text: payload.message || "Session compaction completed.",
    });
    setStatus("Ready");
  } catch (error) {
    state.messages.push({ role: "system", text: error.message });
    setStatus("Error");
  } finally {
    renderAll();
  }
}

async function clearSession() {
  if (!state.sessionId) {
    return;
  }
  setStatus("Clearing");
  try {
    await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`, { method: "DELETE" });
    state.sessionId = null;
    state.messages = [];
    state.pendingApproval = null;
    await loadSessions();
    setStatus("Ready");
  } catch (error) {
    state.messages.push({ role: "system", text: error.message });
    setStatus("Error");
    renderAll();
  }
}

els.newSession.addEventListener("click", createSession);
els.form.addEventListener("submit", sendMessage);
els.compact.addEventListener("click", compactSession);
els.clear.addEventListener("click", clearSession);
els.approveTool.addEventListener("click", () => decideApproval(true));
els.rejectTool.addEventListener("click", () => decideApproval(false));

loadSessions().catch((error) => {
  state.messages.push({ role: "system", text: error.message });
  setStatus("Error");
  renderAll();
});
