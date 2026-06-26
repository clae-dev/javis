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
      feedTTS(m.content);     // 문장이 완성되는 대로 미리 읽기 시작
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
      speak(m.content);
      break;
    case "done":
      flushTTS();             // 남은 문장 토막까지 마저 읽기
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
  stopTTS();   // 이전 턴에 밀려 있던 음성은 끊는다
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

// --- 음성 출력 (TTS) — 문장 단위 스트리밍 재생 ---
//
// 토큰이 스트리밍되는 대로 문장 경계에서 끊어 TTS 합성을 먼저 시작하고,
// 합성된 오디오는 큐에 쌓아 순서대로 재생한다. 다음 문장 합성이 현재 문장
// 재생과 겹쳐 돌기 때문에, 응답 전체를 기다렸다가 한 번에 읽던 방식보다
// 첫 소리까지의 지연이 크게 짧아진다.

voiceOutBtn.addEventListener("click", () => {
  voiceOutput = !voiceOutput;
  localStorage.setItem("javis_voice_out", voiceOutput ? "1" : "0");
  setVoiceOutUI();
  if (!voiceOutput) stopTTS();
});

const SENTENCE_END = /[.!?。…\n]/;   // 문장 종결 신호
const CLAUSE_END = /[,，、;:]/;        // 절 경계 — 첫 덩어리를 일찍 끊을 때만
const FIRST_MIN = 4, FIRST_MAX = 20;  // 첫 덩어리 최소/최대 글자

let ttsBuffer = "";                  // 아직 합성에 안 넘긴 누적 텍스트
const ttsTextQueue = [];             // 합성 대기 중인 문장
const ttsAudioQueue = [];            // 재생 대기 중인 오디오 URL
let ttsFetching = false;
let ttsPlaying = false;
let ttsAudioEl = null;
let ttsFirstDone = false;            // 이번 턴 첫 덩어리를 이미 내보냈는지

// 문장 종결 위치(끝 다음 인덱스). 없으면 -1.
function sentenceCut(buf) {
  const i = buf.search(SENTENCE_END);
  return i === -1 ? -1 : i + 1;
}

// 첫 덩어리용: 문장 끝을 안 기다리고 절 경계/충분한 길이에서 일찍 끊는다.
function firstCut(buf) {
  const s = sentenceCut(buf);
  if (s !== -1) return s;
  for (let i = 0; i < buf.length; i++) {
    if (i + 1 >= FIRST_MIN && CLAUSE_END.test(buf[i])) return i + 1;
  }
  if (buf.length >= FIRST_MAX) {
    const sp = buf.lastIndexOf(" ", FIRST_MAX);
    return sp >= FIRST_MIN ? sp + 1 : FIRST_MAX;
  }
  return -1;
}

// 스트리밍 토큰을 받아, 첫 덩어리는 일찍(절 단위) 이후는 문장 단위로 합성 큐에 넣는다.
function feedTTS(token) {
  if (!voiceOutput) return;
  ttsBuffer += token;
  while (true) {
    const i = ttsFirstDone ? sentenceCut(ttsBuffer) : firstCut(ttsBuffer);
    if (i === -1) break;
    const seg = ttsBuffer.slice(0, i).trim();
    ttsBuffer = ttsBuffer.slice(i);
    if (seg) { ttsTextQueue.push(seg); ttsFirstDone = true; }
  }
  pumpFetch();
}

// 응답이 끝나면 종결 부호 없이 남은 토막까지 마저 읽는다.
function flushTTS() {
  if (!voiceOutput) { ttsBuffer = ""; return; }
  const rest = ttsBuffer.trim();
  ttsBuffer = "";
  if (rest) { ttsTextQueue.push(rest); pumpFetch(); }
}

// 완성된 한 덩어리(공지 등)를 통째로 읽는다.
function speak(text) {
  if (!voiceOutput || !text || !text.trim()) return;
  ttsTextQueue.push(text.trim());
  pumpFetch();
}

// 진행 중인 음성/대기열을 전부 비운다 (새 턴 시작·음성 끄기·취소 시).
function stopTTS() {
  ttsBuffer = "";
  ttsFirstDone = false;
  ttsTextQueue.length = 0;
  ttsAudioQueue.forEach((url) => URL.revokeObjectURL(url));
  ttsAudioQueue.length = 0;
  if (ttsAudioEl) { ttsAudioEl.pause(); ttsAudioEl = null; }
  ttsPlaying = false;
}

// 합성 워커: 문장을 순서대로 합성해 오디오 큐로 옮긴다. (재생과 겹쳐 돈다)
async function pumpFetch() {
  if (ttsFetching) return;
  ttsFetching = true;
  try {
    while (ttsTextQueue.length) {
      const text = ttsTextQueue.shift();
      try {
        const res = await fetch("/voice/tts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        if (!res.ok) continue;
        ttsAudioQueue.push(URL.createObjectURL(await res.blob()));
        pumpPlay();
      } catch { /* 한 문장 합성 실패는 건너뛴다 */ }
    }
  } finally {
    ttsFetching = false;
  }
}

// 재생 워커: 오디오 큐를 순서대로 끊김 없이 재생한다.
function pumpPlay() {
  if (ttsPlaying) return;
  const url = ttsAudioQueue.shift();
  if (!url) return;
  ttsPlaying = true;
  ttsAudioEl = new Audio(url);
  const next = () => {
    URL.revokeObjectURL(url);
    ttsPlaying = false;
    ttsAudioEl = null;
    pumpPlay();
  };
  ttsAudioEl.onended = next;
  ttsAudioEl.onerror = next;
  ttsAudioEl.play().catch(next);
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
