const canvas = document.getElementById("reactor");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");
const userLine = document.getElementById("userLine");
const replyLine = document.getElementById("replyLine");
const connEl = document.getElementById("conn");
const presenceEl = document.getElementById("presence");
const fsBtn = document.getElementById("fsBtn");

const PALETTE = {
  idle: "#1f9fc4",
  listening: "#22e0ff",
  thinking: "#ffb13b",
  speaking: "#46ffa6",
  error: "#ff5470",
};
const STATUS = {
  idle: "대기 중",
  listening: "듣고 있어요",
  thinking: "생각하는 중",
  speaking: "말하는 중",
  error: "오류",
};
const ENERGY = { idle: 0.14, listening: 0.85, thinking: 0.5, speaking: 0.92, error: 0.3 };

let state = "idle";
let color = PALETTE.idle;
let energy = 0.14;
let targetEnergy = 0.14;
let t = 0;
// 목소리 대역별 세기 (저/중/고). 포락선이 올 때만 채워진다.
let bands = [0, 0, 0];

// --- 목소리 포락선 ---
//
// 음성 데몬이 말하기 직전에 클립 전체의 세기를 프레임 목록으로 보내 준다. 오디오
// 자체는 오지 않는다 — 소리는 데스크톱 스피커에서 나고, 화면은 숫자만 받아 따라
// 그린다. 그래서 여기서는 '언제 시작했는지'만 알고 자기 시계로 훑으면 된다.
let envelope = null;
let envelopeFrameMs = 33;
let envelopeStart = 0;

function playEnvelope(frames, frameMs) {
  if (!Array.isArray(frames) || !frames.length) return;
  envelope = frames;
  envelopeFrameMs = frameMs || 33;
  envelopeStart = performance.now();
}

function readEnvelope() {
  if (!envelope) return null;
  const i = Math.floor((performance.now() - envelopeStart) / envelopeFrameMs);
  if (i < 0) return null;
  if (i >= envelope.length) {
    envelope = null; // 다 재생했다. 다음 클립까지는 상태별 기본값으로 돌아간다.
    return null;
  }
  return envelope[i];
}

// --- 캔버스 크기 ---
let dpr = Math.min(window.devicePixelRatio || 1, 2);
function resize() {
  const size = Math.min(window.innerWidth, window.innerHeight);
  canvas.width = size * dpr;
  canvas.height = size * dpr;
}
window.addEventListener("resize", resize);
resize();

// --- 상태 전환 ---
function setState(s, text) {
  state = PALETTE[s] ? s : "idle";
  color = PALETTE[state];
  targetEnergy = ENERGY[state];
  document.documentElement.style.setProperty("--c", color);
  document.body.dataset.state = state;
  statusEl.textContent = STATUS[state] || "";

  if (state === "listening") {
    userLine.classList.remove("show");
    userLine.textContent = "";
    replyLine.textContent = "";
  } else if (state === "thinking" && text) {
    userLine.textContent = text;
    userLine.classList.add("show");
  } else if (state === "speaking" && text) {
    typeReply(text);
  }
}

// --- 답변 타이핑 효과 ---
let typeTimer = null;
function typeReply(text) {
  clearInterval(typeTimer);
  replyLine.textContent = "";
  let i = 0;
  typeTimer = setInterval(() => {
    replyLine.textContent = text.slice(0, ++i);
    if (i >= text.length) clearInterval(typeTimer);
  }, 22);
}

// --- 입자 ---
//
// 상태별로 색과 움직임이 달라지고, 자비스가 말할 때는 목소리 세기에 맞춰 밀려났다
// 돌아온다. 링만 있을 때보다 '살아 있다'는 느낌이 크다. 입자는 한 번만 만들고
// 계속 재사용한다 — 매 프레임 새로 만들면 GC 가 프레임을 잡아먹는다.
const PARTICLES = 420;
const dust = Array.from({ length: PARTICLES }, () => ({
  a: Math.random() * Math.PI * 2,          // 각도
  r: 0.55 + Math.random() * 1.15,          // 반지름 (R 배수)
  size: 0.6 + Math.random() * 1.6,
  drift: (Math.random() - 0.5) * 0.35,     // 각속도 — 제각각이라야 흐르는 것처럼 보인다
  phase: Math.random() * Math.PI * 2,      // 숨쉬기 위상
  push: 0,                                 // 소리에 밀려난 정도. 천천히 되돌아온다
  band: Math.floor(Math.random() * 3),     // 이 입자가 반응할 대역
}));

function drawDust(R) {
  ctx.fillStyle = color;
  for (const p of dust) {
    // 대역별로 다르게 밀어야 목소리의 결이 보인다. 전부 같이 움직이면 그냥 깜빡임이다.
    // 실제 말소리는 대역 값이 0.2 를 넘는 일이 드물어서(저역 우세, 고역은 0.01 언저리)
    // 그대로 쓰면 거의 안 움직인다. 눈에 보이는 범위로 늘려 준다.
    const target = Math.min(1.1, bands[p.band] * 2.6 + energy * 0.8);
    p.push += (target - p.push) * 0.25;
    p.a += p.drift * 0.004 * (1 + energy);

    const breathe = 1 + 0.05 * Math.sin(t * 1.4 + p.phase);
    const rr = R * (p.r * breathe + p.push * 1.15);
    const x = Math.cos(p.a) * rr;
    const y = Math.sin(p.a) * rr;

    // 멀리 밀려난 입자일수록 옅게 — 퍼져 나가 사라지는 인상을 준다.
    ctx.globalAlpha = Math.max(0, Math.min(0.95, (0.3 + p.push * 0.8) * (1 - p.push * 0.3)));
    ctx.beginPath();
    ctx.arc(x, y, dpr * p.size * (1.0 + p.push * 0.9), 0, Math.PI * 2);
    ctx.fill();
  }
}

// --- 리액터 그리기 ---
function draw() {
  t += 0.016;

  // 말하는 중이면 실제 목소리 세기를, 아니면 상태별 기본값을 쓴다.
  const frame = readEnvelope();
  if (frame) {
    energy += (frame[0] - energy) * 0.5;   // 소리에는 빠르게 붙는다
    for (let i = 0; i < 3; i++) bands[i] += ((frame[i + 1] || 0) - bands[i]) * 0.5;
  } else {
    energy += (targetEnergy - energy) * 0.07;
    for (let i = 0; i < 3; i++) bands[i] += (0 - bands[i]) * 0.08;
  }

  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const cx = W / 2, cy = H / 2;
  const R = Math.min(W, H) * 0.17;
  const pulse = 1 + 0.06 * Math.sin(t * 2.2);

  ctx.save();
  ctx.translate(cx, cy);

  // 입자를 먼저 깔고 그 위에 링을 얹는다. 순서가 반대면 코어 글로우가 입자를 덮는다.
  drawDust(R);

  // 바깥 회전 호
  ctx.strokeStyle = color;
  ctx.globalAlpha = 0.5;
  ctx.lineWidth = dpr * 2;
  for (let k = 0; k < 3; k++) {
    const rr = R * (1.7 + k * 0.22);
    const a0 = t * (0.3 + k * 0.25) + k * 2;
    ctx.beginPath();
    ctx.arc(0, 0, rr, a0, a0 + Math.PI * (0.5 + 0.2 * k));
    ctx.stroke();
  }

  // 원형 반응 바
  const N = 84;
  ctx.globalAlpha = 0.9;
  for (let i = 0; i < N; i++) {
    const ang = (i / N) * Math.PI * 2;
    const wob = 0.5 + 0.5 * Math.sin(t * 3 + i * 0.7) * Math.sin(t * 1.3 + i * 0.2);
    const len = R * (0.18 + energy * wob * 1.1);
    const r0 = R * 1.32;
    const x0 = Math.cos(ang) * r0, y0 = Math.sin(ang) * r0;
    const x1 = Math.cos(ang) * (r0 + len), y1 = Math.sin(ang) * (r0 + len);
    ctx.strokeStyle = color;
    ctx.lineWidth = dpr * 2.2;
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
  }

  // 안쪽 점선 링 (역회전)
  ctx.globalAlpha = 0.6;
  ctx.setLineDash([dpr * 3, dpr * 9]);
  ctx.lineWidth = dpr * 1.5;
  ctx.beginPath();
  ctx.arc(0, 0, R * 1.18 * pulse, t * -0.6, t * -0.6 + Math.PI * 2);
  ctx.stroke();
  ctx.setLineDash([]);

  // 중심 코어 (글로우)
  const coreR = R * (0.62 + energy * 0.28) * pulse;
  const grad = ctx.createRadialGradient(0, 0, 0, 0, 0, coreR);
  grad.addColorStop(0, color);
  grad.addColorStop(0.35, color + "cc");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.globalAlpha = 0.45 + energy * 0.45;
  ctx.fillStyle = grad;
  ctx.shadowColor = color;
  ctx.shadowBlur = dpr * 40 * (0.5 + energy);
  ctx.beginPath();
  ctx.arc(0, 0, coreR, 0, Math.PI * 2);
  ctx.fill();
  ctx.shadowBlur = 0;

  // 코어 테두리
  ctx.globalAlpha = 0.95;
  ctx.strokeStyle = "#eaffff";
  ctx.lineWidth = dpr * 1.5;
  ctx.beginPath();
  ctx.arc(0, 0, R * 0.5 * pulse, 0, Math.PI * 2);
  ctx.stroke();

  ctx.restore();
  requestAnimationFrame(draw);
}
draw();

// --- 카메라에 누가 보이는지 ---
//
// 얼굴 상자를 그리려면 영상이 있어야 하는데 HUD 에는 영상이 없다. 대신 알아본
// 이름만 상단에 띄운다. 카메라 화면을 이 페이지로 끌어오는 건 대역폭도, 사생활도
// 값이 비싸서 하지 않는다.

let presenceTimer = null;

function showPresence(names) {
  clearTimeout(presenceTimer);
  presenceEl.textContent = names.length ? `👁 ${names.join(", ")}` : "";
}

function flashPresence(text) {
  const previous = presenceEl.textContent;
  presenceEl.textContent = text;
  clearTimeout(presenceTimer);
  presenceTimer = setTimeout(() => (presenceEl.textContent = previous), 1500);
}

// --- 브라우저 화면 ---
//
// 자비스가 웹을 뒤지는 동안 어느 페이지를 열었는지 구석에 띄운다. 서버가 2fps 로
// jpeg 를 보내 준다 — 영상이 아니라서 이 정도면 충분하다.

let browserBox = null;

function showBrowser(frame, url) {
  if (!frame) return;
  if (!browserBox) {
    browserBox = document.createElement("div");
    browserBox.className = "browser-feed";
    browserBox.innerHTML = '<img alt="" /><span></span>';
    document.body.appendChild(browserBox);
  }
  browserBox.querySelector("img").src = "data:image/jpeg;base64," + frame;
  browserBox.querySelector("span").textContent = url || "";
}

function hideBrowser() {
  browserBox?.remove();
  browserBox = null;
}

// --- WebSocket ---
// 채팅 화면과 같은 토큰을 쓴다. 서버에 JAVIS_TOKEN 이 없으면 빈 값이어도 붙는다.
let authToken = localStorage.getItem("javis_token") || "";

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/hud`);
  let ping;
  ws.onopen = () => {
    if (authToken) ws.send(JSON.stringify({ token: authToken }));
    connEl.textContent = "● ONLINE";
    ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
  };
  ws.onmessage = (ev) => {
    try {
      const m = JSON.parse(ev.data);
      // 카메라 소식은 아크리액터 상태를 건드리지 않는다. 대화 중에 말하다 말고
      // idle 로 튀면 안 되니, 상단에 따로 표시만 한다.
      if (m.state === "vision") return showPresence(m.faces || []);
      if (m.state === "gesture") return flashPresence(`✋ ${m.text || ""}`);
      // 브라우저 화면은 리액터 상태와 무관하다. 여기서 끊지 않으면 setState 가
      // 모르는 상태로 보고 idle 로 떨어뜨려, 말하다 말고 색이 튄다.
      if (m.state === "browser") return showBrowser(m.frame, m.url);
      if (m.state === "browser_off") return hideBrowser();
      if (m.state) setState(m.state, m.text);
      // 목소리 세기가 같이 왔으면 입자를 거기에 맞춘다. 없으면 상태별 기본 움직임.
      if (m.envelope) playEnvelope(m.envelope, m.frame_ms);
    } catch {}
  };
  ws.onclose = (ev) => {
    clearInterval(ping);
    setState("idle");
    if (ev.code === 1008) {
      connEl.textContent = "○ 인증 실패";
      const t = window.prompt("자비스 접속 토큰");
      authToken = (t || "").trim();
      if (!authToken) return;
      localStorage.setItem("javis_token", authToken);
      connect();
      return;
    }
    connEl.textContent = "○ 재연결 중…";
    setTimeout(connect, 1500);
  };
}
connect();

// --- 전체화면 ---
function toggleFs() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}
fsBtn.addEventListener("click", toggleFs);
document.addEventListener("dblclick", toggleFs);
