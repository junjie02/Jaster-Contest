const SVG_NS = "http://www.w3.org/2000/svg";

const STATUS_COLORS = {
  in_progress: "#f59e0b",
  completed: "#22c55e",
  failed: "#ef4444",
};

const state = {
  treeData: null,
  eventSource: null,
  currentTransform: { x: 0, y: 0, k: 1 },
  userAdjustedViewport: false,
  viewportEl: null,
  isPanning: false,
  panStart: null,
};

const container = document.getElementById("graph-container");
const svg = document.getElementById("graph");
const tooltip = document.getElementById("tooltip");
const graphState = document.getElementById("graph-state");
const runIdInput = document.getElementById("run-id-input");
const runIdDisplay = document.getElementById("run-id-display");
const nodeCount = document.getElementById("node-count");
const loadBtn = document.getElementById("load-btn");
const liveBtn = document.getElementById("live-btn");

function setGraphState(message, tone = "info") {
  graphState.textContent = message;
  graphState.dataset.tone = tone;
  graphState.classList.remove("hidden");
}

function hideGraphState() {
  graphState.classList.add("hidden");
}

function closeLive() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function updateStats(nodes) {
  const counts = { in_progress: 0, completed: 0, failed: 0 };
  nodes.forEach((node) => {
    if (counts[node.status] !== undefined) {
      counts[node.status] += 1;
    }
  });
  nodeCount.textContent = `Tasks: ${nodes.length} | In Progress: ${counts.in_progress} | Completed: ${counts.completed} | Failed: ${counts.failed}`;
}

function createSvgElement(name, attrs = {}) {
  const el = document.createElementNS(SVG_NS, name);
  Object.entries(attrs).forEach(([key, value]) => {
    el.setAttribute(key, String(value));
  });
  return el;
}

function getNodeWidth(node) {
  const base = 110;
  const extra = Math.min(90, String(node.title || "").length * 3);
  return base + extra;
}

function getNodeHeight(node) {
  return node.parent_key ? 56 : 64;
}

function buildTreeLayout(data) {
  const nodes = Array.isArray(data.nodes) ? data.nodes.map((node) => ({ ...node })) : [];
  const children = new Map();
  const roots = [];

  nodes.forEach((node) => {
    children.set(node.key, []);
  });

  nodes.forEach((node) => {
    if (node.parent_key && children.has(node.parent_key)) {
      children.get(node.parent_key).push(node);
    } else {
      roots.push(node);
    }
  });

  children.forEach((items) => {
    items.sort((left, right) => String(left.title || "").localeCompare(String(right.title || "")));
  });

  const root = roots[0] || nodes[0] || null;
  if (!root) {
    return { nodes: [], edges: [] };
  }

  const positioned = [];
  const layerMap = new Map();
  const visited = new Set();
  const queue = [{ node: root, depth: 0 }];

  while (queue.length) {
    const current = queue.shift();
    if (!current || visited.has(current.node.key)) {
      continue;
    }
    visited.add(current.node.key);
    const layer = layerMap.get(current.depth) || [];
    layer.push(current.node);
    layerMap.set(current.depth, layer);
    const nextChildren = children.get(current.node.key) || [];
    nextChildren.forEach((child) => queue.push({ node: child, depth: current.depth + 1 }));
  }

  nodes.forEach((node) => {
    if (!visited.has(node.key)) {
      const fallbackLayer = layerMap.get(1) || [];
      fallbackLayer.push(node);
      layerMap.set(1, fallbackLayer);
    }
  });

  const layerGap = 280;
  const rowGap = 110;
  const orderedDepths = [...layerMap.keys()].sort((left, right) => left - right);
  orderedDepths.forEach((depth) => {
    const layer = layerMap.get(depth) || [];
    const totalHeight = Math.max(0, (layer.length - 1) * rowGap);
    layer.forEach((node, index) => {
      positioned.push({
        ...node,
        depth,
        x: depth * layerGap,
        y: index * rowGap - totalHeight / 2,
      });
    });
  });

  const nodeMap = new Map(positioned.map((node) => [node.key, node]));
  const edges = [];
  positioned.forEach((node) => {
    if (node.parent_key && nodeMap.has(node.parent_key)) {
      edges.push({ from: node.parent_key, to: node.key });
    }
  });
  return { nodes: positioned, edges };
}

function fitTransform(positionedNodes) {
  if (!positionedNodes.length) {
    return { x: 80, y: 80, k: 1 };
  }
  const padding = 120;
  const minX = Math.min(...positionedNodes.map((node) => node.x - getNodeWidth(node) / 2));
  const maxX = Math.max(...positionedNodes.map((node) => node.x + getNodeWidth(node) / 2));
  const minY = Math.min(...positionedNodes.map((node) => node.y - getNodeHeight(node) / 2));
  const maxY = Math.max(...positionedNodes.map((node) => node.y + getNodeHeight(node) / 2 + 32));
  const graphWidth = Math.max(1, maxX - minX);
  const graphHeight = Math.max(1, maxY - minY);
  const width = Math.max(320, container.clientWidth || 0);
  const height = Math.max(320, container.clientHeight || 0);
  const scale = Math.max(0.35, Math.min(1.2, Math.min((width - padding) / graphWidth, (height - padding) / graphHeight)));
  const x = (width - graphWidth * scale) / 2 - minX * scale;
  const y = (height - graphHeight * scale) / 2 - minY * scale;
  return { x, y, k: scale };
}

function applyViewportTransform() {
  if (!state.viewportEl) {
    return;
  }
  const { x, y, k } = state.currentTransform;
  state.viewportEl.setAttribute("transform", `translate(${x} ${y}) scale(${k})`);
}

function renderNodeShape(parent, node) {
  const width = getNodeWidth(node);
  const height = getNodeHeight(node);
  const fill = STATUS_COLORS[node.status] || "#9ca3af";
  const stroke = node.parent_key ? "#f8fafc" : "#fde68a";
  const body = createSvgElement("rect", {
    x: -width / 2,
    y: -height / 2,
    width,
    height,
    rx: node.parent_key ? 12 : 18,
    fill,
    stroke,
    "stroke-width": node.parent_key ? 2 : 3,
  });
  parent.appendChild(body);

  const title = createSvgElement("text", {
    x: 0,
    y: -4,
    "text-anchor": "middle",
    "font-size": node.parent_key ? "12" : "13",
    "font-weight": node.parent_key ? "600" : "700",
    fill: "#111827",
  });
  const text = String(node.title || "");
  title.textContent = text.length > 26 ? `${text.slice(0, 24)}...` : text;
  parent.appendChild(title);

  const status = createSvgElement("text", {
    x: 0,
    y: 15,
    "text-anchor": "middle",
    "font-size": "10",
    fill: "#1f2937",
  });
  const attempts = Number(node.attempt_count || 0);
  status.textContent = `${String(node.status || "").replace("_", " ")} | tries ${attempts}`;
  parent.appendChild(status);
}

function showTooltip(event, node) {
  const findings = Array.isArray(node.latest_findings) ? node.latest_findings.join("\n") : "";
  const fields = [
    ["key", node.key],
    ["parent_key", node.parent_key],
    ["title", node.title],
    ["status", node.status],
    ["reason", node.reason],
    ["completion_criteria", node.completion_criteria],
    ["attempt_count", node.attempt_count],
    ["latest_summary", node.latest_summary],
    ["latest_findings", findings],
  ].filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "");

  tooltip.innerHTML = fields
    .map(([key, value]) => `<div class="tip-row"><span class="tip-key">${escapeHtml(key)}:</span> <span class="tip-val">${escapeHtml(value)}</span></div>`)
    .join("");
  tooltip.classList.remove("hidden");
  moveTooltip(event);
}

function moveTooltip(event) {
  if (tooltip.classList.contains("hidden")) {
    return;
  }
  const rect = container.getBoundingClientRect();
  let x = event.clientX - rect.left + 15;
  let y = event.clientY - rect.top + 15;
  if (x + 360 > rect.width) {
    x -= 375;
  }
  if (y + 260 > rect.height) {
    y -= 275;
  }
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${y}px`;
}

function hideTooltip() {
  tooltip.classList.add("hidden");
}

function renderTree(data) {
  state.treeData = data;
  const layout = buildTreeLayout(data);
  svg.replaceChildren();

  if (!layout.nodes.length) {
    updateStats([]);
    setGraphState("No tasks available yet.", "empty");
    return;
  }

  const viewport = createSvgElement("g", { class: "viewport" });
  const edgesGroup = createSvgElement("g", { class: "edges" });
  const nodesGroup = createSvgElement("g", { class: "nodes" });
  viewport.appendChild(edgesGroup);
  viewport.appendChild(nodesGroup);
  svg.appendChild(viewport);
  state.viewportEl = viewport;

  const nodeMap = new Map(layout.nodes.map((node) => [node.key, node]));
  layout.edges.forEach((edge) => {
    const fromNode = nodeMap.get(edge.from);
    const toNode = nodeMap.get(edge.to);
    if (!fromNode || !toNode) {
      return;
    }
    const dx = toNode.x - fromNode.x;
    const dy = toNode.y - fromNode.y;
    const cx = fromNode.x + dx * 0.5;
    const cy = fromNode.y + dy * 0.5 + Math.sign(dy || 1) * Math.abs(dx) * 0.18;
    const path = createSvgElement("path", {
      d: `M ${fromNode.x} ${fromNode.y} Q ${cx} ${cy} ${toNode.x} ${toNode.y}`,
      stroke: "#cbd5e1",
      "stroke-width": "2.5",
      "stroke-opacity": "0.85",
      fill: "none",
    });
    edgesGroup.appendChild(path);
  });

  layout.nodes.forEach((node) => {
    const group = createSvgElement("g", {
      class: "node",
      transform: `translate(${node.x} ${node.y})`,
      "data-key": node.key,
    });
    renderNodeShape(group, node);
    group.addEventListener("mouseenter", (event) => showTooltip(event, node));
    group.addEventListener("mousemove", moveTooltip);
    group.addEventListener("mouseleave", hideTooltip);
    nodesGroup.appendChild(group);
  });

  if (!state.userAdjustedViewport) {
    state.currentTransform = fitTransform(layout.nodes);
  }
  applyViewportTransform();
  updateStats(layout.nodes);
  hideGraphState();
}

async function loadRun(runId) {
  if (!runId) {
    return false;
  }
  setGraphState(`Loading run ${runId}...`);
  const data = await fetchJson(`/run/${encodeURIComponent(runId)}/tree`);
  renderTree(data);
  runIdDisplay.textContent = `Run: ${runId}`;
  return true;
}

async function loadLatestRun() {
  setGraphState("Loading latest run...");
  const payload = await fetchJson("/latest_run");
  const runId = payload && payload.run_id ? String(payload.run_id).trim() : "";
  if (!runId) {
    return false;
  }
  runIdInput.value = runId;
  await loadRun(runId);
  return true;
}

function connectSSE() {
  closeLive();
  runIdDisplay.textContent = "LIVE";
  setGraphState("Connecting live task updates...");

  fetchJson("/current")
    .then((data) => {
      if (data && Array.isArray(data.nodes) && data.nodes.length) {
        renderTree(data);
        runIdDisplay.textContent = "LIVE";
      } else if (!state.treeData) {
        setGraphState("Waiting for live task data...", "empty");
      }
    })
    .catch(() => {
      if (!state.treeData) {
        setGraphState("Waiting for live task data...", "empty");
      }
    });

  state.eventSource = new EventSource("/events");
  state.eventSource.addEventListener("tree_update", (event) => {
    try {
      const data = JSON.parse(event.data);
      renderTree(data);
      runIdDisplay.textContent = "LIVE";
    } catch (error) {
      console.error("Failed to parse tree update:", error);
    }
  });
  state.eventSource.onerror = () => {
    if (!state.treeData) {
      setGraphState("Live connection unavailable.", "error");
    }
  };
}

function clampScale(nextScale) {
  return Math.min(2.5, Math.max(0.3, nextScale));
}

svg.addEventListener("pointerdown", (event) => {
  if (event.target.closest(".node")) {
    return;
  }
  state.isPanning = true;
  state.panStart = {
    pointerX: event.clientX,
    pointerY: event.clientY,
    originX: state.currentTransform.x,
    originY: state.currentTransform.y,
  };
  svg.classList.add("dragging");
  svg.setPointerCapture(event.pointerId);
  hideTooltip();
});

svg.addEventListener("pointermove", (event) => {
  if (!state.isPanning || !state.panStart) {
    return;
  }
  state.userAdjustedViewport = true;
  state.currentTransform.x = state.panStart.originX + (event.clientX - state.panStart.pointerX);
  state.currentTransform.y = state.panStart.originY + (event.clientY - state.panStart.pointerY);
  applyViewportTransform();
});

function endPan(event) {
  if (state.isPanning) {
    state.isPanning = false;
    state.panStart = null;
    svg.classList.remove("dragging");
    if (event) {
      svg.releasePointerCapture(event.pointerId);
    }
  }
}

svg.addEventListener("pointerup", endPan);
svg.addEventListener("pointercancel", endPan);
svg.addEventListener("pointerleave", () => {
  if (!state.isPanning) {
    svg.classList.remove("dragging");
  }
});

svg.addEventListener(
  "wheel",
  (event) => {
    if (!state.viewportEl) {
      return;
    }
    event.preventDefault();
    state.userAdjustedViewport = true;
    const rect = svg.getBoundingClientRect();
    const pointerX = event.clientX - rect.left;
    const pointerY = event.clientY - rect.top;
    const direction = event.deltaY > 0 ? 0.9 : 1.1;
    const nextScale = clampScale(state.currentTransform.k * direction);
    const scaleRatio = nextScale / state.currentTransform.k;
    state.currentTransform.x = pointerX - (pointerX - state.currentTransform.x) * scaleRatio;
    state.currentTransform.y = pointerY - (pointerY - state.currentTransform.y) * scaleRatio;
    state.currentTransform.k = nextScale;
    applyViewportTransform();
  },
  { passive: false }
);

loadBtn.addEventListener("click", async () => {
  const runId = runIdInput.value.trim();
  closeLive();
  try {
    await loadRun(runId);
  } catch (error) {
    setGraphState(`Run not found: ${runId}`, "error");
  }
});

runIdInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    loadBtn.click();
  }
});

liveBtn.addEventListener("click", () => {
  connectSSE();
});

window.addEventListener("resize", () => {
  if (state.treeData) {
    renderTree(state.treeData);
  }
});

loadLatestRun()
  .then((loaded) => {
    if (!loaded) {
      connectSSE();
    }
  })
  .catch(() => {
    connectSSE();
  });
