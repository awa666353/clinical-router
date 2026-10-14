(function initLoginPage() {
  const form = document.getElementById("login-form");
  const email = document.getElementById("login-email");
  const password = document.getElementById("login-password");
  const errorBox = document.getElementById("login-error");
  const submit = document.getElementById("login-submit");

  function safeNext() {
    const next = new URLSearchParams(window.location.search).get("next");
    if (!next) return "/";
    if (!next.startsWith("/") || next.startsWith("//") || next.includes("\\")) return "/";
    if (/^[a-z][a-z0-9+.-]*:/i.test(next)) return "/";
    try {
      const resolved = new URL(next, window.location.origin);
      if (resolved.origin !== window.location.origin) return "/";
    } catch {
      return "/";
    }
    return next;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.textContent = "";
    const emailValue = email.value.trim();
    const passwordValue = password.value;
    if (!emailValue || !passwordValue) {
      errorBox.textContent = "请输入邮箱和密码";
      return;
    }
    submit.disabled = true;
    try {
      await Pilot.api("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: emailValue, password: passwordValue }),
      });
      window.location.href = safeNext();
    } catch (err) {
      errorBox.textContent = err && err.status === 401
        ? "邮箱或密码错误"
        : (err && err.message) || "登录失败，请稍后重试";
    } finally {
      submit.disabled = false;
    }
  });
})();
