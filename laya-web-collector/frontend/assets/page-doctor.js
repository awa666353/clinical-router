(function initDoctorPage() {
  if (new URLSearchParams(location.search).has("encounter")) return;
  document.getElementById("legacy-workspace").classList.remove("hidden");
  const state = { encounter: null, game: null, view: null, attempt: null, facts: [], pollTimer: null, starting: false, sending: false, patientLink: null };
  const els = {
    encounterPill: document.getElementById("encounter-pill"),
    chief: document.getElementById("chief-complaint"),
    factList: document.getElementById("fact-list"),
    factCount: document.getElementById("fact-count"),
    chat: document.getElementById("chat-log"),
    turnCount: document.getElementById("turn-count"),
    panel: document.getElementById("attempt-panel"),
    question: document.getElementById("current-question"),
    questionEditor: document.getElementById("question-editor"),
    channelHint: document.getElementById("channel-hint"),
    answer: document.getElementById("answer-text"),
    sendQuestion: document.getElementById("send-question-btn"),
    submitAnswer: document.getElementById("submit-answer-btn"),
    ledger: document.getElementById("ledger"),
    ledgerPanel: document.getElementById("ledger-panel"),
    progress: document.getElementById("fact-progress"),
    progressLabel: document.getElementById("progress-label"),
    invitePanel: document.getElementById("invite-panel"),
    inviteLink: document.getElementById("invite-link"),
  };

  async function boot() {
    try {
      const encounterId = new URLSearchParams(location.search).get("encounter");
      state.encounter = encounterId
        ? await Pilot.api(`/encounters/${encodeURIComponent(encounterId)}`)
        : await Pilot.getLatestEncounter();
      if (encounterId) {
        state.game = await Pilot.api(`/game/encounters/${encodeURIComponent(encounterId)}`);
        document.getElementById("manual-answer-panel").classList.add("hidden");
        document.getElementById("roleplay-title").textContent = `虚拟病例 · ${state.game.title}`;
        document.getElementById("patient-mode-pill").textContent = state.game.patient_mode === "ai" ? "✦ AI 患者" : "🎭 真人患者";
        if (state.game.patient_mode === "human") {
          const saved = sessionStorage.getItem(`laya-patient-link:${encounterId}`);
          if (saved) {
            state.patientLink = saved;
          } else {
            const link = await Pilot.api(`/encounters/${encounterId}/patient-session-link`, { method: "POST" });
            state.patientLink = `${location.origin}/patient?token=${encodeURIComponent(link.token)}`;
            sessionStorage.setItem(`laya-patient-link:${encounterId}`, state.patientLink);
          }
          els.inviteLink.value = state.patientLink;
          els.invitePanel.classList.remove("hidden");
        }
      } else {
        document.getElementById("patient-mode-pill").textContent = "常规采集";
      }
      els.encounterPill.textContent = `会话 ${state.encounter.encounter_id.slice(0, 10)}`;
      els.encounterPill.className = "pill ok";
      await refresh();
    } catch (err) {
      els.encounterPill.textContent = "初始化失败";
      els.encounterPill.className = "pill danger";
      Pilot.toast(err.message, "error");
    }
  }

  async function refresh() {
    const view = await Pilot.api(`/encounters/${state.encounter.encounter_id}/doctor-view`);
    state.view = view;
    state.facts = view.facts || [];
    els.chief.textContent = view.chief_complaint || "未记录";
    els.turnCount.textContent = state.game ? `已交流 ${(view.turns || []).length} 条` : `${(view.turns || []).length} turns`;
    renderFacts();
    renderChat(view.turns || []);
    if (!state.attempt && (view.recent_attempts || []).length) {
      state.attempt = { attempt_id: view.recent_attempts[0].attempt_id };
    }
    if (state.attempt) {
      try {
        state.attempt = await Pilot.api(`/attempts/${state.attempt.attempt_id}`);
        renderAttempt();
      } catch (err) { state.attempt = null; renderAttempt(); }
    }
  }

  function renderFacts() {
    els.factCount.textContent = state.game ? `${state.facts.length} 条线索` : `${state.facts.length} 个事实`;
    const finished = state.facts.filter((fact) => ["obtained", "denied", "not_applicable"].includes(fact.status)).length;
    els.progress.style.width = `${state.facts.length ? 100 * finished / state.facts.length : 0}%`;
    els.progressLabel.textContent = `已了解 ${finished} / ${state.facts.length} 条线索`;
    if (!state.facts.length) { els.factList.innerHTML = `<div class="empty">暂无事实目录。</div>`; return; }
    els.factList.innerHTML = state.facts.map((fact) => `
      <article class="fact-item ${state.attempt && state.attempt.target_fact_id === fact.clinical_fact_id ? "active" : ""}"
               data-fact="${Pilot.escapeHtml(fact.clinical_fact_id)}">
        <div class="fact-title">${Pilot.escapeHtml(fact.display_name)}</div>
        <div class="fact-meta">
          <span class="status ${Pilot.escapeHtml(fact.status || "unknown")}">${Pilot.escapeHtml(statusLabel(fact.status))}</span>
          ${state.game ? "" : `<span class="tiny muted">权重 ${Number(fact.criticality || 3)}</span>`}
        </div>
        ${state.game ? "" : `<div class="tiny muted">${Pilot.escapeHtml(fact.notes || "")}</div>`}
      </article>
    `).join("");
    els.factList.querySelectorAll(".fact-item").forEach((node) => {
      node.addEventListener("click", () => {
        if (!state.starting) startAttempt(node.dataset.fact);
      });
    });
  }

  function setFactListBusy(busy) {
    els.factList.style.pointerEvents = busy ? "none" : "";
    els.factList.style.opacity = busy ? "0.65" : "";
    els.factList.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function statusLabel(status) {
    return ({ unknown: "未采集", obtained: "已获得", denied: "已否认", asked_no_answer: "未答出", ambiguous: "有歧义", conflicting: "冲突", not_applicable: "不适用" })[status] || status || "未知";
  }

  function renderChat(turns) {
    if (!turns.length) { els.chat.innerHTML = `<div class="empty">${state.game ? "选择左侧一条线索，开始问诊。" : "还没有对话。选择目标事实后，先生成问题。"}</div>`; return; }
    els.chat.innerHTML = turns.map((turn) => `
      <div class="bubble ${Pilot.escapeHtml(turn.speaker)}">
        <span class="who">${turn.speaker === "interviewer" ? "医生" : turn.speaker === "patient" ? "患者" : "系统"} · ${Pilot.escapeHtml(String(turn.turn_no))}</span>
        ${Pilot.escapeHtml(turn.raw_text)}
      </div>
    `).join("");
    els.chat.scrollTop = els.chat.scrollHeight;
  }

  async function startAttempt(factId) {
    if (state.starting) return;
    state.starting = true;
    setFactListBusy(true);
    Pilot.toast("正在开始采集，请稍候…", "ok");
    try {
      clearInterval(state.pollTimer);
      if (!state.game) state.patientLink = null;
      // Declare the target before the allocation is revealed, and hand the
      // declaration time to the server as concealment evidence.
      const targetSelectedAt = new Date().toISOString();
      state.attempt = await Pilot.api(`/encounters/${state.encounter.encounter_id}/attempts`, {
        method: "POST",
        body: JSON.stringify({ target_fact_id: factId, target_selected_at: targetSelectedAt }),
      });
      await createQuestion();
      renderAttempt();
      Pilot.toast(state.game ? `已选择线索：${state.attempt.target_fact.display_name}` : `已开始 ${state.attempt.target_fact.display_name}，channel=${state.attempt.executed_channel}`);
    } catch (err) {
      Pilot.toast(err.message, "error");
    } finally {
      state.starting = false;
      setFactListBusy(false);
      renderFacts();
    }
  }

  async function createQuestion() {
    if (!state.attempt) return;
    const asyncMode = state.attempt.interaction_mode !== "sync";
    const payload = state.attempt.executed_channel === "Human"
      ? { question_text: `请描述${state.attempt.target_fact.display_name}的情况。`, approved_by: "doctor" }
      : {};
    const endpoint = asyncMode ? "question/plan" : "question";
    let question = await Pilot.api(`/attempts/${state.attempt.attempt_id}/${endpoint}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (asyncMode) {
      for (let i = 0; i < 15 && question.status === "planned"; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 800));
        const detail = await Pilot.api(`/attempts/${state.attempt.attempt_id}`);
        question = detail.question;
      }
    }
    state.attempt.question = question;
    renderAttempt();
    return question;
  }

  function renderAttempt() {
    if (!state.attempt) { els.panel.classList.add("hidden"); els.ledger.innerHTML = `<div class="empty">尚未开始当前 attempt。</div>`; return; }
    els.panel.classList.remove("hidden");
    const channel = state.attempt.executed_channel;
    const question = state.attempt.question;
    els.currentQuestionRender(question, channel);
    els.channelHint.textContent = state.game
      ? `本题形式：${channel === "LLM" ? "AI 协助拟题" : channel === "Structured" ? "标准问题卡" : "由你自由提问"}`
      : `本次 channel：${channel} · ${state.attempt.interaction_mode} · ${channel === "LLM" ? "LLM 生成问题" : channel === "Structured" ? "模板问题" : "医生自由询问"}`;
    els.sendQuestion.textContent = state.game ? "向患者提问" : state.attempt.interaction_mode === "sync" ? "发送问题给患者" : "加入患者问题队列";
    const doctorCanEdit = !state.game || channel === "Human";
    const draftInput = document.activeElement?.id === "doctor-question" ? document.activeElement : null;
    const draft = draftInput ? draftInput.value : null;
    const draftCursor = draftInput ? draftInput.selectionStart : null;
    if (!question) {
      els.questionEditor.innerHTML = `
        <label class="label">医生问题</label>
        <input id="doctor-question" type="text" value="请描述${Pilot.escapeHtml(state.attempt.target_fact.display_name)}的情况。" />
      `;
    } else {
      els.questionEditor.innerHTML = `
        <label class="label">${doctorCanEdit ? "当前问题（可修改后发送）" : "系统生成的问题"}</label>
        <input id="doctor-question" type="text" value="${Pilot.escapeHtml(question.question_text)}" ${doctorCanEdit ? "" : "readonly"} />
      `;
    }
    if (draft !== null && doctorCanEdit) {
      const input = document.getElementById("doctor-question");
      input.value = draft;
      input.focus();
      input.setSelectionRange(draftCursor, draftCursor);
    }
    const pending = state.attempt.status === "extracting";
    const questionReady = question && ["generated", "approved", "queued"].includes(question.status);
    els.submitAnswer.disabled = pending || !questionReady || state.attempt.interaction_mode !== "sync";
    els.sendQuestion.disabled = !questionReady || state.sending;
    if (channel === "Human" && state.attempt.status === "answered") els.ledgerPanel.open = true;
    renderLedger();
  }

  els.currentQuestionRender = function(question, channel) {
    const link = state.patientLink && !state.game
      ? `<div class="tiny patient-link-row">患者链接：<a href="${Pilot.escapeHtml(state.patientLink)}" target="_blank" rel="noopener noreferrer">打开患者页</a> · <code>${Pilot.escapeHtml(state.patientLink)}</code></div>`
      : "";
    els.question.innerHTML = question
      ? `<strong>${state.game ? "正在了解" : "当前目标"}：${Pilot.escapeHtml(state.attempt.target_fact.display_name)}</strong>${Pilot.escapeHtml(question.question_text)}${link}`
      : `<strong>${state.game ? "正在了解" : "当前目标"}：${Pilot.escapeHtml(state.attempt.target_fact.display_name)}</strong><span class="muted">正在准备问题…</span>`;
  };

  async function sendQuestion() {
    if (!state.attempt || !state.attempt.question) return;
    const input = document.getElementById("doctor-question");
    const text = (input && input.value || state.attempt.question.question_text || "").trim();
    if (!text) return Pilot.toast("问题不能为空", "error");
    if (state.sending) return;
    state.sending = true;
    els.sendQuestion.disabled = true;
    try {
      let question = state.attempt.question;
      if (state.attempt.interaction_mode !== "sync") {
        if (state.attempt.executed_channel === "Human" && text !== question.question_text) {
          question = await Pilot.api(`/attempts/${state.attempt.attempt_id}/question/revise`, {
            method: "POST", body: JSON.stringify({ question_text: text }),
          });
          state.attempt.question = question;
        }
        if (question.status === "generated") {
          await Pilot.api(`/questions/${question.question_event_id}/approve`, { method: "POST" });
        }
        await Pilot.api(`/questions/${question.question_event_id}/queue`, { method: "POST" });
        if (state.game?.patient_mode === "ai") {
          Pilot.toast("AI 患者正在阅读问题…");
          const reply = await Pilot.api(`/game/encounters/${state.encounter.encounter_id}/ai-reply`, {
            method: "POST", body: JSON.stringify({ attempt_id: state.attempt.attempt_id }),
          });
          Pilot.toast(reply.used_fallback ? "剧本患者已回答" : "AI 患者已回答");
        } else {
          if (!state.patientLink) {
            const link = await Pilot.api(`/encounters/${state.encounter.encounter_id}/patient-session-link`, {
              method: "POST",
            });
            state.patientLink = `${location.origin}/patient?token=${encodeURIComponent(link.token)}`;
          }
          Pilot.toast("问题已发送，等待患者回答");
        }
      } else {
        await Pilot.api(`/encounters/${state.encounter.encounter_id}/turns`, {
          method: "POST",
          body: JSON.stringify({ speaker: "interviewer", raw_text: text }),
        });
        Pilot.toast("问题已发送给患者页");
      }
      await refresh();
    } catch (err) { Pilot.toast(err.message, "error"); }
    finally { state.sending = false; renderAttempt(); }
  }

  async function submitAnswer() {
    if (!state.attempt) return;
    const text = els.answer.value.trim();
    if (!text) return Pilot.toast("请先填写患者回答", "error");
    try {
      await Pilot.api(`/attempts/${state.attempt.attempt_id}/answer`, {
        method: "POST",
        body: JSON.stringify({ answer_text: text, input_mode: "typed" }),
      });
      els.answer.value = "";
      state.attempt.status = state.attempt.executed_channel === "Human" ? "answered" : "extracting";
      renderAttempt();
      if (state.attempt.status === "extracting") await pollAttempt();
      await refresh();
    } catch (err) { Pilot.toast(err.message, "error"); }
  }

  async function pollAttempt() {
    clearInterval(state.pollTimer);
    for (let i = 0; i < 20; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 900));
      state.attempt = await Pilot.api(`/attempts/${state.attempt.attempt_id}`);
      renderAttempt();
      if (!["extracting", "created", "questioned"].includes(state.attempt.status)) break;
    }
  }

  function renderLedger() {
    if (!state.attempt) return;
    const candidates = state.attempt.candidates || [];
    const failures = state.attempt.failure_events || [];
    const question = state.attempt.question;
    const status = state.attempt.status;
    const candidateHtml = candidates.length ? candidates.map((c) => `
      <div class="candidate-card">
        <div class="candidate-head">
          <b>${Pilot.escapeHtml(c.clinical_fact_id)}</b>
          <span class="chip ${Pilot.escapeHtml(c.validation_status)}">${Pilot.escapeHtml(c.validation_status)}</span>
        </div>
        <div class="tiny muted">${Pilot.escapeHtml(c.assertion_status)} · confidence ${Pilot.escapeHtml(c.confidence ?? "–")}</div>
        <pre>${Pilot.escapeHtml(JSON.stringify({ value: c.value_json, quote: c.evidence_quote, errors: c.validation_errors_json }, null, 2))}</pre>
      </div>
    `).join("") : `<div class="empty">等待 LLM 候选或人工记录。</div>`;
    const humanForm = renderHumanForm(status);
    els.ledger.innerHTML = `
      <div class="summary-card">
        <div class="tiny muted">attempt 状态</div>
        <div class="attempt-status">${Pilot.escapeHtml(status)}</div>
        <div class="tiny muted">问题来源：${Pilot.escapeHtml(question ? question.question_source : "pending")}</div>
        <div class="tiny muted">分配：${Pilot.escapeHtml(state.attempt.assigned_channel)} → 执行 ${Pilot.escapeHtml(state.attempt.executed_channel)} · ${Pilot.escapeHtml(state.attempt.target_declared_source || "n/a")}</div>
      </div>
      ${failures.length ? `<div class="summary-card"><div class="candidate-head"><b>失败事件</b></div>${failures.map((f) => `<span class="chip needs_review">${Pilot.escapeHtml(f.failure_code)}</span>`).join(" ")}</div>` : ""}
      ${humanForm}
      ${candidateHtml}
      <div class="divider"></div>
      <p class="tiny muted">这里的候选只是机器建议。真正可用的 outcome 由独立裁定者在另一页面完成。</p>
    `;
    const humanButton = document.getElementById("save-human-record");
    if (humanButton) humanButton.addEventListener("click", submitHumanRecord);
  }

  function renderHumanForm(status) {
    if (state.attempt.executed_channel !== "Human" || !["answered", "extracted", "needs_review"].includes(status)) return "";
    const fact = state.attempt.target_fact || {};
    const schema = fact.answer_schema || {};
    const allowed = schema.allowed_values || [];
    const labels = schema.value_labels || {};
    const valueControl = allowed.length
      ? `<select id="human-value"><option value="">请选择</option>${allowed.map((v) => `<option value="${Pilot.escapeHtml(v)}">${Pilot.escapeHtml(labels[v] || v)}</option>`).join("")}</select>`
      : `<input id="human-value" type="text" placeholder="输入规范化 value" />`;
    return `<div class="summary-card human-record-card">
      <div class="candidate-head"><b>医生人工记录</b><span class="chip">Human</span></div>
      <p class="tiny muted">Human channel 不使用 LLM 自动抽取。请根据患者回答人工写入规范化事实。</p>
      <label class="label">记录状态</label>
      <select id="human-status">
        <option value="positive">阳性</option>
        <option value="negative">阴性</option>
        <option value="unknown">不知道/未答出</option>
        <option value="ambiguous" selected>有歧义</option>
        <option value="not_applicable">不适用</option>
      </select>
      <label class="label mt-10">值</label>
      ${valueControl}
      <label class="label mt-10">备注</label>
      <textarea id="human-note" rows="2" placeholder="可写患者原话或医生记录说明"></textarea>
      <div class="composer-row mt-10">
        <span class="tiny muted">保存后进入独立裁定队列。</span>
        <button class="btn primary small" id="save-human-record">保存人工记录</button>
      </div>
    </div>`;
  }

  async function submitHumanRecord() {
    if (!state.attempt) return;
    const status = document.getElementById("human-status")?.value || "ambiguous";
    const value = document.getElementById("human-value")?.value || null;
    const note = document.getElementById("human-note")?.value || "";
    try {
      await Pilot.api(`/attempts/${state.attempt.attempt_id}/doctor-record`, {
        method: "POST",
        body: JSON.stringify({
          assertion_status: status,
          value: value || null,
          value_text: note || state.attempt.target_fact.display_name,
          evidence_quote: note || null,
          confidence: 1.0,
        }),
      });
      state.attempt = await Pilot.api(`/attempts/${state.attempt.attempt_id}`);
      renderAttempt();
      Pilot.toast("人工记录已保存");
    } catch (err) { Pilot.toast(err.message, "error"); }
  }

  async function start() {
    const allowed = await Pilot.requireLogin(["doctor", "interviewer", "curator", "admin"]);
    if (!allowed) return;
    Pilot.mountUserBadge();
    els.sendQuestion.addEventListener("click", sendQuestion);
    els.submitAnswer.addEventListener("click", submitAnswer);
    document.getElementById("copy-invite").addEventListener("click", async () => {
      if (!els.inviteLink.value) return;
      try { await navigator.clipboard.writeText(els.inviteLink.value); }
      catch { els.inviteLink.select(); document.execCommand("copy"); }
      Pilot.toast("患者链接已复制");
    });
    setInterval(() => { if (state.encounter) refresh().catch(() => {}); }, 5000);
    boot();
  }

  start();
})();
