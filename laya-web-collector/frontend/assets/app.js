const API = "/api";

function currentLocationForRedirect() {
  return `${window.location.pathname}${window.location.search}`;
}

function redirectToLogin() {
  if (window.location.pathname === "/login") return;
  window.location.href = `/login?next=${encodeURIComponent(currentLocationForRedirect())}`;
}

function isAuthPath(path) {
  return path === "/auth/login" || path === "/auth/refresh";
}

function isPatientSessionPath(path) {
  return path.startsWith("/patient/session/");
}

function buildHeaders(options) {
  const headers = {};
  const input = options.headers;
  if (input && typeof input.forEach === "function" && !Array.isArray(input)) {
    input.forEach((value, key) => { headers[key] = value; });
  } else if (input) {
    Object.assign(headers, input);
  }
  Object.keys(headers).forEach((key) => {
    if (/^x-user-(id|role)$/i.test(key)) delete headers[key];
  });
  const method = String(options.method || "GET").toUpperCase();
  const hasBody = options.body !== undefined && options.body !== null;
  const hasContentType = Object.keys(headers).some((key) => key.toLowerCase() === "content-type");
  if (hasBody && !hasContentType) headers["Content-Type"] = "application/json";
  if (method !== "GET" && method !== "HEAD") {
    Object.keys(headers).forEach((key) => {
      if (key.toLowerCase() === "x-requested-with") delete headers[key];
    });
    headers["X-Requested-With"] = "laya-collector";
  }
  return headers;
}

async function parseResponseBody(response) {
  const text = await response.text();
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}

function responseError(body, response) {
  const detail = body && body.detail ? body.detail : body || response.statusText;
  const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  error.status = response.status;
  error.body = body;
  return error;
}

async function tryRefresh() {
  try {
    const response = await fetch(`${API}/auth/refresh`, {
      method: "POST",
      headers: { "X-Requested-With": "laya-collector" },
      credentials: "same-origin",
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function apiRequest(path, options, isRetry) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: buildHeaders(options),
    credentials: "same-origin",
  });
  const body = await parseResponseBody(response);
  if (response.status === 401 && !isAuthPath(path) && !isPatientSessionPath(path)) {
    if (!isRetry) {
      const refreshed = await tryRefresh();
      if (refreshed) {
        try {
          return await apiRequest(path, options, true);
        } catch (err) {
          // A transport failure on the retry cannot be recovered here.
          if (!err || err.status === undefined) redirectToLogin();
          throw err;
        }
      }
    }
    redirectToLogin();
    throw responseError(body, response);
  }
  if (!response.ok) throw responseError(body, response);
  return body;
}

async function api(path, options = {}, _legacyRole = undefined) {
  return apiRequest(path, options, false);
}

function toast(message, kind = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function dateTime(value) {
  if (!value) return "";
  try { return new Date(value).toLocaleString(); } catch { return value; }
}

async function getLatestEncounter() {
  const latest = await api("/encounters/latest");
  if (!latest || !latest.encounter_id) throw new Error("暂无 encounter，请先运行演示初始化。");
  return latest;
}

let currentUser = null;

async function me() {
  const user = await api("/auth/me");
  currentUser = user;
  return user;
}

function getCurrentUser() {
  return currentUser;
}

async function logout() {
  try {
    await api("/auth/logout", { method: "POST" });
  } catch {
    // Even if the session already expired, leave through the login page.
  }
  window.location.href = "/login";
}

function showForbidden() {
  if (document.getElementById("forbidden-screen")) return;
  const overlay = document.createElement("div");
  overlay.id = "forbidden-screen";
  overlay.className = "forbidden-screen";
  overlay.setAttribute("role", "alert");
  overlay.textContent = "403 无权限：当前账号不能访问此页面。";
  document.body.appendChild(overlay);
}

async function requireLogin(allowedRoles) {
  let user;
  try {
    user = await me();
  } catch (err) {
    if (err && err.status === 404) {
      // Local development (AUTH_MODE=disabled) has no login session.
      return true;
    }
    if (err && err.status === 403) {
      showForbidden();
      return false;
    }
    if (!err || err.status !== 401) toast((err && err.message) || "登录状态检查失败", "error");
    return false;
  }
  const roles = Array.isArray(allowedRoles) ? allowedRoles : [];
  if (roles.length && !roles.includes(user.role)) {
    showForbidden();
    return false;
  }
  return true;
}

async function mountUserBadge() {
  const el = document.getElementById("current-user");
  if (!el) return;
  try {
    const user = await me();
    el.textContent = `${user.pilot_user_id} (${user.role})`;
  } catch {
    // api() already redirects to /login on 401; never crash page init here.
  }
}

window.Pilot = {
  API,
  api,
  toast,
  escapeHtml,
  dateTime,
  getLatestEncounter,
  me,
  getCurrentUser,
  logout,
  requireLogin,
  mountUserBadge,
};
