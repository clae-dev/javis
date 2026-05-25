const canvas = document.getElementById("reactor");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");
const userLine = document.getElementById("userLine");
const replyLine = document.getElementById("replyLine");
const connEl = document.getElementById("conn");
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

// --- 리액터 그리기 ---
function draw() {
  t += 0.016;
  energy += (targetEnergy - energy) * 0.07;

  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const cx = W / 2, cy = H / 2;
  const R = Math.min(W, H) * 0.17;
  const pulse = 1 + 0.06 * Math.sin(t * 2.2);

  ctx.save();
  ctx.translate(cx, cy);

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

// --- WebSocket ---
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/hud`);
  let ping;
  ws.onopen = () => {
    connEl.textContent = "● ONLINE";
    ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
  };
  ws.onmessage = (ev) => {
    try {
      const m = JSON.parse(ev.data);
      if (m.state) setState(m.state, m.text);
    } catch {}
  };
  ws.onclose = () => {
    connEl.textContent = "○ 재연결 중…";
    clearInterval(ping);
    setState("idle");
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
