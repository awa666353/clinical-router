(function initAdjudicatorPage() {
  const state = { rows: [] };

  async function load() {
    try {
      state.rows = await Pilot.api("/adjudication/queue");
      document.getElementById("queue-count").textContent = `${state.rows.length} 条待裁定`;
      render();
    } catch (err) { Pilot.toast(err.message, "error"); }
  }

  function options(name, selected = "uncertain") {
    const safeName = Pilot.escapeHtml(name);
    return `<div class="option-row">
      ${[["yes", "是"], ["no", "否"], ["uncertain", "不确定"], ["not_applicable", "不适用"], ["unable_to_assess", "无法评价"]].map(([value, label]) => `
        <label class="option"><input type="radio" name="${safeName}" value="${value}" ${value === selected ? "checked" : ""}>${label}</label>
      `).join("")}
    </div>`;
  }

  function render() {
    const queue = document.getElementById("queue");
    if (!state.rows.length) { queue.innerHTML = `<div class="empty">当前没有待裁定 attempt。完成一次医生采集后回来。</div>`; return; }
    queue.innerHTML = state.rows.map((row) => {
      const attemptId = Pilot.escapeHtml(row.attempt_id);
      return `
        <article class="card queue-card" data-attempt="${attemptId}">
          <div class="section-title">
            <div>
              <h3>${Pilot.escapeHtml(row.target_fact.display_name)}</h3>
              <div class="tiny muted">${Pilot.escapeHtml(row.case_code)} · attempt ${Pilot.escapeHtml(row.attempt_id.slice(0, 10))}</div>
            </div>
            <div class="chips">
              <span class="pill">${Pilot.escapeHtml(row.target_fact.answer_schema.kind)}</span>
              <span class="pill warn">${Number(row.review_count || 0)} / ${Number(row.reviews_required || 2)} 评</span>
            </div>
          </div>
          <div class="queue-grid">
            <div>
              <label class="label">问题</label>
              <div class="summary-card">${Pilot.escapeHtml(row.question ? row.question.question_text : "未记录问题")}</div>
              <label class="label">患者回答</label>
              <div class="summary-card">${Pilot.escapeHtml(row.answer ? row.answer.raw_text : "未记录回答")}</div>
              ${(row.failure_events || []).length ? `<div class="chips mt-8">${row.failure_events.map((f) => `<span class="chip needs_review">${Pilot.escapeHtml(f.failure_code)}</span>`).join("")}</div>` : ""}
              <label class="label">机器候选（仅参考，不作为真值）</label>
              ${(row.candidates || []).map((c) => `
                <div class="candidate-card">
                  <div class="candidate-head">
                    <b>${Pilot.escapeHtml(c.clinical_fact_id)}</b>
                    <span class="chip ${Pilot.escapeHtml(c.validation_status)}">${Pilot.escapeHtml(c.validation_status)}</span>
                  </div>
                  <div class="tiny muted">${Pilot.escapeHtml(c.assertion_status)} · confidence ${Pilot.escapeHtml(c.confidence ?? "–")}</div>
                  <pre>${Pilot.escapeHtml(JSON.stringify({ value: c.value_json, quote: c.evidence_quote, errors: c.validation_errors_json }, null, 2))}</pre>
                </div>
              `).join("") || `<div class="empty">无候选</div>`}
            </div>
            <div>
              <div class="field-grid">
                <div><label class="label">acquired · 患者是否实际回答了</label>${options(`acquired-${row.attempt_id}`)}</div>
                <div><label class="label">recorded · 系统/医生记录是否正确</label>${options(`recorded-${row.attempt_id}`)}</div>
                <div><label class="label">usable · 是否足以解决目标事实</label>${options(`usable-${row.attempt_id}`)}</div>
                <div><label class="label">actionable · 是否足以支持下一步动作</label>${options(`actionable-${row.attempt_id}`)}</div>
              </div>
              <div class="mt-12">
                <label class="label" for="reason-${attemptId}">理由（可选）</label>
                <textarea id="reason-${attemptId}" rows="2" placeholder="例如：患者只回答不知道；或回答明确但 value 无法映射"></textarea>
              </div>
              <div class="composer-row mt-14">
                <span class="tiny muted">独立裁定，不改变原始对话。</span>
                <button class="btn primary submit-outcome" data-attempt="${attemptId}">提交裁定</button>
              </div>
            </div>
          </div>
        </article>
      `;
    }).join("");
    document.querySelectorAll(".submit-outcome").forEach((button) => {
      button.addEventListener("click", () => submitOutcome(button.dataset.attempt, button));
    });
  }

  function getRadio(name) {
    const nodes = document.getElementsByName(name);
    for (const node of nodes) if (node.checked) return node.value;
    return "uncertain";
  }

  async function submitOutcome(attemptId, button) {
    button.disabled = true;
    try {
      const reason = document.getElementById(`reason-${attemptId}`);
      const row = state.rows.find((item) => item.attempt_id === attemptId) || {};
      const user = Pilot.getCurrentUser();
      await Pilot.api(`/attempts/${attemptId}/outcomes`, {
        method: "POST",
        body: JSON.stringify({
          rater_id: user && user.pilot_user_id ? user.pilot_user_id : "adjudicator-demo",
          rater_role: "adjudicator",
          adjudication_round: Number(row.review_count || 0) + 1,
          blinded_to_channel: true,
          blinded_to_ai: true,
          acquired_label: getRadio(`acquired-${attemptId}`),
          recorded_label: getRadio(`recorded-${attemptId}`),
          usable_label: getRadio(`usable-${attemptId}`),
          actionable_label: getRadio(`actionable-${attemptId}`),
          reason_text: reason ? reason.value : "",
        }),
      });
      Pilot.toast("裁定已保存");
      await load();
    } catch (err) {
      button.disabled = false;
      Pilot.toast(err.message, "error");
    }
  }

  async function start() {
    const allowed = await Pilot.requireLogin(["adjudicator", "curator", "admin"]);
    if (!allowed) return;
    Pilot.mountUserBadge();
    load();
  }

  start();
})();
