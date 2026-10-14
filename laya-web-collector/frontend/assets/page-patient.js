(function initPatientPage() {
  const state = { view: null, token: null, session: null, brief: null, activeQuestionId: null,
    generating: false, requestedAfter: null, generationError: null, finished: false, sending: false };
  const els = {
    status: document.getElementById("status-pill"),
    complaint: document.getElementById("chief-complaint"),
    chat: document.getElementById("chat"),
    answer: document.getElementById("answer"),
    send: document.getElementById("send"),
  };

  function disableComposer() {
    els.answer.disabled = true;
    els.send.disabled = true;
    document.querySelectorAll(".quick button").forEach((button) => { button.disabled = true; });
  }

  function syncComposer() {
    const enabled = Boolean(state.token && state.session?.questions?.length && !state.sending);
    els.answer.disabled = !enabled;
    els.send.disabled = !enabled;
    document.querySelectorAll(".quick button").forEach((button) => { button.disabled = !enabled; });
  }

  async function boot() {
    try {
      state.token = new URLSearchParams(location.search).get("token");
      if (!state.token) {
        els.status.textContent = "缺少患者链接";
        els.status.className = "pill danger";
        els.chat.innerHTML = `<div class="empty">请从医生页获取患者链接，再打开患者页。</div>`;
        disableComposer();
        return;
      }
      els.status.textContent = "已连接";
      els.status.className = "pill ok";
      try {
        state.brief = await Pilot.api(`/patient/session/${encodeURIComponent(state.token)}/brief`);
        if (state.brief.doctor_mode === "ai") {
          document.getElementById("patient-title").textContent = "扮演患者，与 AI 医生对话";
        }
        renderBrief();
      } catch (err) {
        if (![400, 401, 404].includes(err.status)) throw err;
      }
      await refresh();
      setInterval(() => { refresh().catch(() => {}); }, 2500);
    } catch (err) {
      els.status.textContent = "连接失败";
      els.status.className = "pill danger";
      Pilot.toast(err.message, "error");
    }
  }

  function renderBrief() {
    const brief = state.brief;
    if (!brief) return;
    const panel = document.getElementById("patient-brief");
    const doctorLink = `${location.origin}/doctor?encounter=${encodeURIComponent(brief.encounter_id)}`;
    const invite = brief.doctor_mode === "ai"
      ? `<p class="tiny">AI 医生会根据你的回答继续提问。每次只回答病历卡中已有的信息。</p>`
      : `<div class="invite-row"><input id="doctor-invite-link" type="text" readonly value="${Pilot.escapeHtml(doctorLink)}" aria-label="医生邀请链接" /><button class="btn small" id="copy-doctor-link" type="button">复制医生链接</button></div>`;
    const facts = Object.values(brief.answers || {}).map((value) => `<li>${Pilot.escapeHtml(value)}</li>`).join("");
    panel.innerHTML = `
      <div class="game-eyebrow">仅患者可见 · 虚拟病历卡</div>
      <h2>${Pilot.escapeHtml(brief.title)}</h2>
      <p>${Pilot.escapeHtml(brief.persona)}</p>
      <p><b>开场可以说：</b>“${Pilot.escapeHtml(brief.opening)}”</p>
      <details open><summary>你知道的情况 · 回答时参考</summary><ul>${facts}</ul></details>
      <p class="tiny">${Pilot.escapeHtml(brief.acting_rule)}</p>
      ${invite}
    `;
    panel.classList.remove("hidden");
    panel.querySelector("#copy-doctor-link")?.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(doctorLink); }
      catch { const input = panel.querySelector("#doctor-invite-link"); input.select(); document.execCommand("copy"); }
      Pilot.toast("医生链接已复制");
    });
  }

  async function refresh() {
    if (!state.token) return;
    try {
      state.session = await Pilot.api(`/patient/session/${encodeURIComponent(state.token)}`);
    } catch (err) {
      if (err.status !== 401) throw err;
      els.status.textContent = "链接无效或已过期";
      els.status.className = "pill danger";
      els.chat.innerHTML = `<div class="empty">请联系医生重新生成患者链接。</div>`;
      disableComposer();
      state.token = null;
      return;
    }
    els.complaint.textContent = state.session.chief_complaint || "未记录";
    renderQueue(state.session);
    syncComposer();
    if (state.brief?.doctor_mode === "ai" && !state.session.questions?.length) {
      requestAiDoctor();
    }
  }

  async function requestAiDoctor(force = false) {
    if (!state.token || state.generating || state.finished || state.session?.questions?.length) return;
    const after = state.session?.history?.length || 0;
    if (!force && state.requestedAfter === after) return;
    state.generating = true;
    state.generationError = null;
    state.requestedAfter = after;
    renderQueue(state.session);
    try {
      const result = await Pilot.api(`/patient/session/${encodeURIComponent(state.token)}/ai-doctor/next`, { method: "POST" });
      state.finished = result.status === "finished";
      await refresh();
    } catch (err) {
      state.generationError = err.message;
      renderQueue(state.session);
    } finally {
      state.generating = false;
      renderQueue(state.session);
    }
  }

  function renderQueue(session) {
    const questions = session.questions || [];
    const history = (session.history || []).map((item) => `
      <div class="bubble interviewer"><span class="who">${state.brief?.doctor_mode === "ai" ? "AI 医生问" : "医生刚才问"}</span>${Pilot.escapeHtml(item.question_text)}</div>
      <div class="bubble patient"><span class="who">你的回答</span>${Pilot.escapeHtml(item.answer_text)}</div>
    `).join("");
    if (!questions.length) {
      const ai = state.brief?.doctor_mode === "ai";
      const message = state.finished ? "本轮演练完成，你可以返回首页再开一局。"
        : state.generationError ? `AI 医生暂时无法提问：${Pilot.escapeHtml(state.generationError)}`
        : state.generating ? "AI 医生正在思考下一题…"
        : ai ? "即将开始问诊…"
        : history ? "回答已送达，等待医生的下一题。" : "病历已就绪，等待医生发来第一题。";
      els.chat.innerHTML = `${history}<div class="empty" role="status">${message}${state.generationError ? '<br><button class="btn small" id="retry-ai-doctor" type="button">重试提问</button>' : ""}</div>`;
      els.chat.querySelector("#retry-ai-doctor")?.addEventListener("click", () => requestAiDoctor(true));
      return;
    }
    if (!state.activeQuestionId || !questions.some((q) => q.question_event_id === state.activeQuestionId)) {
      state.activeQuestionId = questions[0].question_event_id;
    }
    els.chat.innerHTML = history + questions.map((q) => `
      <div class="bubble interviewer patient-question${q.question_event_id === state.activeQuestionId ? " active" : ""}">
        <span class="who">${state.brief?.doctor_mode === "ai" ? "AI 医生提问" : "医生发来的问题"} · 待回答</span>
        ${Pilot.escapeHtml(q.question_text)}
        <div class="patient-question-actions">
          <button class="chip" data-question="${Pilot.escapeHtml(q.question_event_id)}">回答这一题</button>
        </div>
      </div>
    `).join("");
    els.chat.querySelectorAll("[data-question]").forEach((button) => {
      button.addEventListener("click", () => {
        state.activeQuestionId = button.dataset.question;
        renderQueue(session);
      });
    });
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  }

  async function sendAnswer(text, answerStatus = "answered") {
    if (answerStatus === "answered" && !text.trim()) return Pilot.toast("请先输入回答", "error");
    if (!state.token || !state.activeQuestionId || !state.session?.questions?.length || state.sending) return;
    state.sending = true;
    syncComposer();
    try {
      await Pilot.api(`/patient/session/${encodeURIComponent(state.token)}/questions/${state.activeQuestionId}/view`, {
        method: "POST",
      });
      await Pilot.api(`/patient/session/${encodeURIComponent(state.token)}/questions/${state.activeQuestionId}/answer`, {
        method: "POST",
        body: JSON.stringify({
          answer_text: answerStatus === "answered" ? text.trim() : null,
          answer_status: answerStatus,
          input_mode: "typed",
        }),
      });
      els.answer.value = "";
      state.activeQuestionId = null;
      await refresh();
      Pilot.toast("回答已发送");
    } catch (err) { Pilot.toast(err.message, "error"); }
    finally { state.sending = false; syncComposer(); }
  }

  els.send.addEventListener("click", () => sendAnswer(els.answer.value));
  els.answer.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") sendAnswer(els.answer.value);
  });
  document.querySelectorAll(".quick button").forEach((button) => {
    button.addEventListener("click", () => sendAnswer(button.dataset.text || button.textContent, button.dataset.status || "answered"));
  });
  boot();
})();
