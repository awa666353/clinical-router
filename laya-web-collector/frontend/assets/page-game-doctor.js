(function initGameDoctor() {
  const encounterId = new URLSearchParams(location.search).get("encounter");
  if (!encounterId) return;

  const state = { game: null, current: null, busy: false, refreshing: false, lastTurnNo: 0 };
  const els = {
    score: document.getElementById("game-score"),
    scoreStatus: document.getElementById("score-status"),
    notes: document.getElementById("game-round-notes"),
    chat: document.getElementById("game-chat-log"),
    turnCount: document.getElementById("game-turn-count"),
    question: document.getElementById("game-question"),
    send: document.getElementById("game-send-question"),
    hint: document.getElementById("game-waiting-hint"),
    invitePanel: document.getElementById("invite-panel"),
    inviteLink: document.getElementById("invite-link"),
  };

  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const next = await Pilot.api(`/game/encounters/${encodeURIComponent(encounterId)}/state`);
      const previousScore = Number(els.score.textContent || 0);
      state.current = next;
      els.score.textContent = String(next.score);
      if (next.score > previousScore) {
        els.score.classList.remove("score-bump");
        void els.score.offsetWidth;
        els.score.classList.add("score-bump");
      }
      els.scoreStatus.textContent = next.scoring_status === "scoring" ? "病历整理与评分中…"
        : next.scoring_status === "unavailable" ? "AI 评分未配置，问答仍会保存"
        : next.scoring_status === "failed" ? "评分暂时失败，记录已保存"
        : next.waiting_for_patient ? "等待患者回答"
        : next.turns.length ? "回答已计分" : "等待第一轮问答";
      els.hint.textContent = next.waiting_for_patient
        ? state.game.patient_mode === "ai" ? "AI 患者正在回答…" : "等待另一位玩家回答…"
        : "患者回答后，AI 先整理病历，再自动计分。";
      els.send.disabled = state.busy || next.waiting_for_patient;
      els.question.disabled = state.busy || next.waiting_for_patient;
      const turns = next.turns || [];
      els.turnCount.textContent = `${turns.length} 条对话`;
      els.chat.innerHTML = turns.length ? turns.map((turn) => `
        <div class="bubble ${turn.speaker === "interviewer" ? "interviewer" : "patient"}">
          <span class="who">${turn.speaker === "interviewer" ? "医生" : "患者"}</span>
          ${Pilot.escapeHtml(turn.raw_text)}
        </div>
      `).join("") : `<div class="empty">患者已经就位。你想先问什么？</div>`;
      const rounds = next.rounds || [];
      els.notes.innerHTML = rounds.length ? [...rounds].reverse().map((round) => {
        const points = round.points_earned !== null ? `+${round.points_earned} 分`
          : round.score_status === "unavailable" ? "未计分"
          : round.score_status === "failed" ? "评分失败" : "评分中";
        const note = round.note_text ? Pilot.escapeHtml(round.note_text).replace(/\n/g, "<br>")
          : round.note_status === "failed" ? "病历整理暂时失败，问答记录已保存。"
          : round.note_status === "unavailable" ? "配置 AI 后可自动整理本轮病历。"
          : "正在根据对话整理病历…";
        return `<article class="game-round-note">
          <div class="game-round-heading"><b>第 ${round.round_no} 轮</b><span>${points}</span></div>
          <div class="game-note-text">${note}</div>
        </article>`;
      }).join("") : `<div class="empty">完成第一轮问答后显示病历。</div>`;
      const newestTurn = turns.length ? turns[turns.length - 1].turn_no : 0;
      if (newestTurn !== state.lastTurnNo) {
        els.chat.scrollTop = els.chat.scrollHeight;
        state.lastTurnNo = newestTurn;
      }
    } catch (err) {
      Pilot.toast(err.message, "error");
    } finally {
      state.refreshing = false;
    }
  }

  async function sendQuestion() {
    const questionText = els.question.value.trim();
    if (!questionText) return Pilot.toast("先输入想问患者的问题", "error");
    if (state.busy || state.current?.waiting_for_patient) return;
    state.busy = true;
    els.send.disabled = true;
    els.question.disabled = true;
    try {
      await Pilot.api(`/game/encounters/${encodeURIComponent(encounterId)}/ask`, {
        method: "POST",
        body: JSON.stringify({ question_text: questionText }),
      });
      els.question.value = "";
      await refresh();
    } catch (err) {
      Pilot.toast(err.message, "error");
    } finally {
      state.busy = false;
      els.send.disabled = Boolean(state.current?.waiting_for_patient);
      els.question.disabled = Boolean(state.current?.waiting_for_patient);
    }
  }

  async function boot() {
    const allowed = await Pilot.requireLogin(["doctor", "interviewer", "curator", "admin"]);
    if (!allowed) return;
    Pilot.mountUserBadge();
    try {
      state.game = await Pilot.api(`/game/encounters/${encodeURIComponent(encounterId)}`);
      document.body.classList.add("roleplay-active");
      document.getElementById("legacy-workspace").classList.add("hidden");
      document.getElementById("game-doctor").classList.remove("hidden");
      document.getElementById("manual-answer-panel").classList.add("hidden");
      document.getElementById("encounter-pill").textContent = `会话 ${encounterId.slice(0, 10)}`;
      document.getElementById("roleplay-title").textContent = "虚拟病例 · 自由问诊";
      document.getElementById("chief-complaint").textContent = state.game.chief_complaint;
      document.getElementById("patient-mode-pill").textContent = state.game.patient_mode === "ai" ? "✦ AI 患者" : "🎭 真人患者";
      if (state.game.patient_mode === "human") {
        let patientLink = sessionStorage.getItem(`laya-patient-link:${encounterId}`);
        if (!patientLink) {
          const link = await Pilot.api(`/encounters/${encodeURIComponent(encounterId)}/patient-session-link`, { method: "POST" });
          patientLink = `${location.origin}/patient?token=${encodeURIComponent(link.token)}`;
          sessionStorage.setItem(`laya-patient-link:${encounterId}`, patientLink);
        }
        els.inviteLink.value = patientLink;
        els.invitePanel.classList.remove("hidden");
      }
      await refresh();
      setInterval(refresh, 2500);
    } catch (err) {
      document.getElementById("encounter-pill").textContent = "初始化失败";
      Pilot.toast(err.message, "error");
    }
  }

  els.send.addEventListener("click", sendQuestion);
  els.question.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") sendQuestion();
  });
  document.getElementById("copy-invite").addEventListener("click", async () => {
    if (!els.inviteLink.value) return;
    try { await navigator.clipboard.writeText(els.inviteLink.value); }
    catch { els.inviteLink.select(); document.execCommand("copy"); }
    Pilot.toast("患者链接已复制");
  });
  boot();
})();
