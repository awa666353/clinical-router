(function initHome() {
  async function createGame(patientMode, startAsPatient, doctorMode = "human") {
    const buttons = document.querySelectorAll("#game-launch button");
    buttons.forEach((button) => { button.disabled = true; });
    try {
      const session = await Pilot.api("/game/sessions", {
        method: "POST",
        body: JSON.stringify({ patient_mode: patientMode, doctor_mode: doctorMode }),
      });
      if (session.patient_token) {
        sessionStorage.setItem(`laya-patient-link:${session.encounter_id}`,
          `${location.origin}/patient?token=${encodeURIComponent(session.patient_token)}`);
      }
      location.href = startAsPatient
        ? `/patient?token=${encodeURIComponent(session.patient_token)}`
        : `/doctor?encounter=${encodeURIComponent(session.encounter_id)}`;
    } catch (err) {
      Pilot.toast(err.message, "error");
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  document.getElementById("start-doctor-human").addEventListener("click", () => createGame("human", false));
  document.getElementById("start-doctor-ai").addEventListener("click", () => createGame("ai", false));
  document.getElementById("start-patient").addEventListener("click", () => createGame("human", true));
  document.getElementById("start-patient-ai").addEventListener("click", () => createGame("human", true, "ai"));
  document.getElementById("export-btn").addEventListener("click", async () => {
    try {
      const row = await Pilot.api("/exports", { method: "POST", body: JSON.stringify({}) });
      Pilot.toast(`已导出 ${row.row_count} 条记录`, "ok");
    } catch (err) { Pilot.toast(err.message, "error"); }
  });

  async function boot() {
    const allowed = await Pilot.requireLogin(["doctor", "interviewer", "curator", "admin"]);
    if (!allowed) return;
    try {
      const health = await Pilot.api("/health");
      const local = health.app_env === "development" && health.auth_mode === "disabled";
      document.getElementById("health-pill").textContent = health.llm_configured ? "AI 已连接" : "可用剧本回答演练";
      if (!health.llm_configured) {
        const aiDoctor = document.getElementById("start-patient-ai");
        aiDoctor.disabled = true;
        aiDoctor.title = "请先配置 LLM_API_KEY，再使用 AI 医生";
      }
      document.getElementById("game-launch").classList.toggle("hidden", !local);
      document.getElementById("legacy-links").classList.toggle("hidden", local);
      const stats = await Pilot.api("/stats");
      for (const [id, key] of [["stat-cases", "cases"], ["stat-encounters", "encounters"], ["stat-attempts", "attempts"], ["stat-pending", "pending_adjudication"]]) {
        document.getElementById(id).textContent = stats[key];
      }
    } catch (err) { Pilot.toast(err.message, "error"); }
  }
  boot();
})();
