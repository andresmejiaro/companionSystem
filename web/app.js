/* Static frontend for the Profile OS assistant server.
 *
 * All backend access goes through the `api` client below. API_BASE
 * defaults to same-origin (empty string) so the page works when served
 * by assistant_server.py; when this folder is packaged into a mobile
 * shell (Capacitor / WebView), set `window.API_BASE` to the server URL
 * before this script loads. No secrets live here — the server owns
 * OPENAI_API_KEY and the bridge bearer.
 */
"use strict";

const API_BASE = window.API_BASE || "";

const api = {
  async request(method, path, body) {
    const res = await fetch(API_BASE + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || `${res.status} ${res.statusText}`);
    }
    return data;
  },
  start: () => api.request("POST", "/api/start"),
  message: (text) => api.request("POST", "/api/message", { text }),
  closeout: (notes) => api.request("POST", "/api/closeout", { notes }),
  status: () => api.request("GET", "/api/status"),
};

const $ = (id) => document.getElementById(id);
const chat = $("chat");
const input = $("input");
const sendBtn = $("send-btn");
const closeoutBtn = $("closeout-btn");
const banner = $("banner");
const debugLog = $("debug-log");

function showBanner(text, ok = false) {
  banner.textContent = text;
  banner.classList.toggle("ok", ok);
  banner.hidden = !text;
}

function addMsg(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function logTools(events) {
  for (const line of events || []) debugLog.textContent += line + "\n";
}

function setBusy(busy) {
  sendBtn.disabled = busy;
  input.disabled = busy;
}

async function boot() {
  try {
    const info = await api.start();
    $("profile-name").textContent =
      `${info.display_name} (${info.profile_id})`;
    $("session-meta").textContent =
      `${info.recent_memories} recent memories`;
    addMsg("system", info.started_now
      ? `Session started. State: ${info.compact_state || "(none)"}`
      : "Reconnected to existing session.");
    showBanner("");
    setBusy(false);
    closeoutBtn.disabled = false;
    input.focus();
  } catch (e) {
    showBanner(`Could not start session: ${e.message}`);
  }
}

async function send() {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addMsg("user", text);
  setBusy(true);
  try {
    const res = await api.message(text);
    logTools(res.tool_events);
    addMsg("assistant", res.reply || "(empty reply)");
    showBanner("");
  } catch (e) {
    showBanner(`Error: ${e.message}`);
  } finally {
    setBusy(false);
    input.focus();
  }
}

async function closeout() {
  const notes = prompt("Closeout note (blank for auto):") ?? null;
  setBusy(true);
  closeoutBtn.disabled = true;
  try {
    await api.closeout(notes || null);
    addMsg("system", "Session closed out. Reload the server for a new one.");
    showBanner("Closed out.", true);
  } catch (e) {
    showBanner(`Closeout failed: ${e.message}`);
    closeoutBtn.disabled = false;
    setBusy(false);
  }
}

sendBtn.addEventListener("click", send);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
closeoutBtn.addEventListener("click", closeout);

boot();
