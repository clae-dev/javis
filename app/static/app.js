const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const statusEl = document.getElementById("status");
const micBtn = document.getElementById("micBtn");
const voiceOutBtn = document.getElementById("voiceOut");
const clearBtn = document.getElementById("clearBtn");
const confirmBox = document.getElementById("confirm");
const confirmMsg = document.getElementById("confirmMsg");
const confirmDetail = document.getElementById("confirmDetail");
const confirmOk = document.getElementById("confirmOk");
const confirmCancel = document.getElementById("confirmCancel");

// thread_id 를 고정해 두면 서버(체크포인터)가 대화 맥락을 이어간다.
const threadId =
  localStorage.getItem("javis_thread") ||
  (() => {
    const id = "web-" + Math.random().toString(36).slice(2, 10);
    localStorage.setItem("javis_thread", id);
    return id;
  })();

let ws;
let currentBot = null;
let currentText = "";
let voiceOutput = localStorage.getItem("javis_voice_out") === "1";

// --- UI helpers ---

function add(cls, text = "") {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}
function note(text) { add("meta", text); }

function setVoiceOutUI() {
  voiceOutBtn.textContent = voiceOutput ? "🔊" : "🔈";
  voiceOutBtn.classList.toggle("active", voiceOutput);
}
setVoiceOutUI();

// --- WebSocket ---

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/chat?thread_id=${threadId}`);
  ws.onopen = () => { statusEl.textContent = "연결됨"; sendBtn.disabled = false; };
  ws.onclose = () => { statusEl.textContent = "연결 끊김 — 재연결 중…"; setTimeout(connect, 2000); };
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
}

function handle(m) {
  switch (m.type) {
    case "token":
      if (!currentBot) { currentBot = add("bot"); currentText = ""; }
      currentBot.textContent += m.content;
      currentText += m.content;
      log.scrollTop = log.scrollHeight;
      break;
    case "tool":
      if (m.status === "running") note(`🔧 ${m.name} 실행 중…`);
      break;
    case "confirm_request":
      askConfirm(m.data);
      break;
    case "proactive":
      add("proactive", m.content);
      notify(m.content);
      if (voiceOutput) speak(m.content);
      break;
    case "done":
      if (voiceOutput && currentText.trim()) speak(currentText);
      currentBot = null; currentText = "";
      sendBtn.disabled = false;
      input.focus();
      break;
    case "error":
      note("오류: " + m.message);
      currentBot = null; currentText = "";
      sendBtn.disabled = false;
      break;
  }
}

function sendText(text) {
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  add("user", text);
  ws.send(JSON.stringify({ content: text }));
  sendBtn.disabled = true;
  currentBot = null; currentText = "";
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  sendText(text);
  input.value = "";
});

// --- 확인 다이얼로그 ---

function askConfirm(data) {
  confirmMsg.textContent = data.message || "이 작업을 실행할까요?";
  confirmDetail.textContent = (data.actions || [])
    .map((a) => `${a.name}\n${JSON.stringify(a.args, null, 2)}`)
    .join("\n\n");
  confirmBox.classList.remove("hidden");

  const decide = (approved) => {
    confirmBox.classList.add("hidden");
    ws.send(JSON.stringify({ approved }));
    note(approved ? "✅ 승인했습니다" : "❌ 취소했습니다");
    currentBot = null; currentText = "";
    confirmOk.onclick = confirmCancel.onclick = null;
  };
  confirmOk.onclick = () => decide(true);
  confirmCancel.onclick = () => decide(false);
}

// --- 음성 입력 (녹음 → STT) ---

let recorder = null;
let chunks = [];

micBtn.addEventListener("click", async () => {
  if (recorder && recorder.state === "recording") {
    recorder.stop();
    return;
  }
  // 브라우저는 localhost 또는 https(보안 컨텍스트)에서만 마이크를 허용한다.
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    note(
      window.isSecureContext
        ? "이 브라우저는 마이크 API를 지원하지 않습니다."
        : "마이크는 localhost 또는 https 에서만 됩니다. 다른 기기에서 IP(예: 192.168.x.x)로 접속 중이라면, PC에서 http://localhost:8000 으로 열거나 HTTPS가 필요합니다."
    );
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recorder = new MediaRecorder(stream);
    chunks = [];
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      micBtn.classList.remove("recording");
      const blob = new Blob(chunks, { type: "audio/webm" });
      await transcribe(blob);
    };
    recorder.start();
    micBtn.classList.add("recording");
  } catch (err) {
    const byName = {
      NotAllowedError:
        "마이크 권한이 거부됐습니다. 주소창 왼쪽 자물쇠/마이크 아이콘에서 '허용'으로 바꿔 주세요. (Windows: 설정 > 개인정보 보호 및 보안 > 마이크 에서 앱 접근도 켜져 있어야 합니다.)",
      NotFoundError: "연결된 마이크를 찾을 수 없습니다.",
      NotReadableError: "다른 앱이 마이크를 점유하고 있어 접근할 수 없습니다.",
      SecurityError: "보안 컨텍스트가 아니라 마이크를 쓸 수 없습니다. localhost 또는 https 로 접속해 주세요.",
    };
    note(byName[err.name] || "마이크를 쓸 수 없습니다: " + err.message);
  }
});

async function transcribe(blob) {
  note("🎧 음성 인식 중…");
  try {
    const fd = new FormData();
    fd.append("file", blob, "audio.webm");
    const res = await fetch("/voice/stt", { method: "POST", body: fd });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const { text } = await res.json();
    if (text && text.trim()) sendText(text.trim());
    else note("인식된 내용이 없습니다.");
  } catch (err) {
    note("음성 인식 실패: " + err.message);
  }
}

// --- 음성 출력 (TTS) ---

voiceOutBtn.addEventListener("click", () => {
  voiceOutput = !voiceOutput;
  localStorage.setItem("javis_voice_out", voiceOutput ? "1" : "0");
  setVoiceOutUI();
});

let audioEl = null;
async function speak(text) {
  try {
    const res = await fetch("/voice/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) return;
    const blob = await res.blob();
    if (audioEl) audioEl.pause();
    audioEl = new Audio(URL.createObjectURL(blob));
    audioEl.play().catch(() => {});
  } catch { /* 음성 출력 실패는 조용히 무시 */ }
}

// --- 브라우저 알림 ---

function notify(text) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") {
    new Notification("자비스", { body: text, icon: "/static/icon.svg" });
  }
}
if ("Notification" in window && Notification.permission === "default") {
  Notification.requestPermission();
}

// --- 기타 ---

clearBtn.addEventListener("click", () => { log.innerHTML = ""; });

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}

connect();
