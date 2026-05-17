const stateText = {
  M: "Modified: local change exists, but it is not committed to shared cache.",
  O: "Owned: an agent or service owns revalidation or re-anchoring responsibility.",
  E: "Exclusive: one agent has a write lease for this pointer.",
  S: "Shared: readable by multiple agents after version checks.",
  I: "Invalid: stale, drifted, or unverifiable; never inject into prompts.",
};

const modeCopy = {
  cache: "Cache mode opens only verified pointer ranges: 149 estimated tokens.",
  baseline: "No-cache mode rereads whole source files for repeated agent tasks: 6,487 estimated tokens.",
};

const agentColors = {
  agent_orders: "#0f766e",
  agent_orders_2: "#588157",
  agent_payments: "#2563eb",
  agent_notifications: "#d97706",
};

const canvas = document.querySelector("#cache-canvas");
const context = canvas.getContext("2d");
let activeAgent = "agent_orders";
let frame = 0;

function resizeCanvas() {
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.floor(rect.width * ratio);
  canvas.height = Math.floor(rect.height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function drawNode(x, y, radius, fill, label, sublabel) {
  context.beginPath();
  context.arc(x, y, radius, 0, Math.PI * 2);
  context.fillStyle = fill;
  context.fill();
  context.lineWidth = 2;
  context.strokeStyle = "rgba(31, 42, 46, 0.24)";
  context.stroke();

  context.fillStyle = "#fff";
  context.textAlign = "center";
  context.font = "700 14px system-ui";
  context.fillText(label, x, y - 2);
  context.font = "600 10px system-ui";
  context.fillText(sublabel, x, y + 14);
}

function drawLine(from, to, color, pulseOffset) {
  const progress = (Math.sin(frame * 0.035 + pulseOffset) + 1) / 2;
  context.strokeStyle = "rgba(31, 42, 46, 0.18)";
  context.lineWidth = 2;
  context.beginPath();
  context.moveTo(from.x, from.y);
  context.lineTo(to.x, to.y);
  context.stroke();

  const pulseX = from.x + (to.x - from.x) * progress;
  const pulseY = from.y + (to.y - from.y) * progress;
  context.beginPath();
  context.arc(pulseX, pulseY, 4, 0, Math.PI * 2);
  context.fillStyle = color;
  context.fill();
}

function drawCanvas() {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  context.clearRect(0, 0, width, height);

  const centerX = width * 0.68;
  const l2 = { x: centerX, y: height * 0.54 };
  const l3 = { x: centerX, y: height * 0.76 };
  const agents = [
    { id: "agent_orders", x: width * 0.48, y: height * 0.25, label: "A1" },
    { id: "agent_payments", x: width * 0.78, y: height * 0.24, label: "A2" },
    { id: "agent_notifications", x: width * 0.88, y: height * 0.48, label: "A3" },
    { id: "agent_orders_2", x: width * 0.55, y: height * 0.83, label: "A4" },
  ];

  drawLine(l2, l3, "#d97706", 1.8);
  agents.forEach((agent, index) => {
    const color = agent.id === activeAgent ? agentColors[agent.id] : "#7a8985";
    drawLine(agent, l2, color, index);
  });

  drawNode(l2.x, l2.y, 54, "#115e59", "L2", "shared");
  drawNode(l3.x, l3.y, 48, "#d97706", "L3", "index");
  agents.forEach((agent) => {
    const color = agent.id === activeAgent ? agentColors[agent.id] : "#53615f";
    drawNode(agent.x, agent.y, agent.id === activeAgent ? 42 : 36, color, agent.label, "L1");
  });

  frame += 1;
  requestAnimationFrame(drawCanvas);
}

document.querySelectorAll(".segment").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".segment").forEach((item) => {
      item.classList.toggle("active", item === button);
      item.setAttribute("aria-selected", item === button ? "true" : "false");
    });
    document.querySelector("#mode-copy").textContent = modeCopy[button.dataset.mode];
  });
});

document.querySelectorAll(".task-card").forEach((card) => {
  card.addEventListener("click", () => {
    activeAgent = card.dataset.agent;
    document.querySelectorAll(".task-card").forEach((item) => item.classList.toggle("active", item === card));
  });
});

document.querySelectorAll(".state-chip").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".state-chip").forEach((item) => item.classList.toggle("active", item === button));
    document.querySelector("#state-description").textContent = stateText[button.dataset.state];
  });
});

document.querySelectorAll(".step").forEach((step) => {
  step.addEventListener("mouseenter", () => {
    document.querySelectorAll(".step").forEach((item) => item.classList.toggle("active", item === step));
  });
});

window.addEventListener("resize", resizeCanvas);
document.querySelector(".task-card")?.classList.add("active");
resizeCanvas();
drawCanvas();
