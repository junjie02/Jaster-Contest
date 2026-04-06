const SVG_NS = "http://www.w3.org/2000/svg";

const STATUS_COLORS = {
  unexplored: "#9ca3af",
  exploring: "#f59e0b",
  success: "#22c55e",
  failed: "#ef4444",
};

const KIND_SHAPES = {
  target: "star",
  asset: "rect",
  entry: "circle",
  weakness: "diamond",
  technique: "rect",
  hypothesis: "hexagon",
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
  nodeCount.textContent = `Nodes: ${nodes.length}`;
}

function createSvgElement(name, attrs = {}) {
  const el = document.createElementNS(SVG_NS, name);
  Object.entries(attrs).forEach(([key, value]) => {
    el.setAttribute(key, String(value));
  });
  return el;
}

function getNodeRadius(node) {
  const priority = Number(node.priority || 0);
  return Math.max(8, Math.min(22, priority / 7 || 10));
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
    items.sort((left, right) => {
      const priorityDelta = Number(right.priority || 0) - Number(left.priority || 0);
      if (priorityDelta !== 0) return priorityDelta;
      return String(left.title).localeCompare(String(right.title));
    });
  });

  const root = roots[0] || nodes[0] || null;
  if (!root) {
    return { nodes: [] };
  }

  const positioned = [];
  const layerMap = new Map();
  const visited = new Set();
  const queue = [{ node: root, depth: 0 }];

  while (queue.length) {
    const current = queue.shift();
    if (!current || visited.has(current.node.key)) continue;
    visited.add(current.node.key);
    const layer = layerMap.get(current.depth) || [];
    layer.push(current.node);
    layerMap.set(current.depth, layer);
    const nextChildren = children.get(current.node.key) || [];
    nextChildren.forEach((child) => queue.push({ node: child, depth: current.depth + 1 }));
  }

  nodes.forEach((node) => {
    if (!visited.has(node.key)) {
      const depth = 1;
      const layer = layerMap.get(depth) || [];
      layer.push(node);
      layerMap.set(depth, layer);
    }
  });

  const layerGap = 260;
  const rowGap = 120;
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

  // Calculate edges from parent_key
  const edges = [];
  const nodeMap = new Map(positioned.map((n) => [n.key, n]));
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
  const minX = Math.min(...positionedNodes.map((node) => node.x - getNodeRadius(node)));
  const maxX = Math.max(...positionedNodes.map((node) => node.x + getNodeRadius(node) + 120));
  const minY = Math.min(...positionedNodes.map((node) => node.y - getNodeRadius(node) - 40));
  const maxY = Math.max(...positionedNodes.map((node) => node.y + getNodeRadius(node) + 60));
  const graphWidth = Math.max(1, maxX - minX);
  const graphHeight = Math.max(1, maxY - minY);
  const width = Math.max(320, container.clientWidth || 0);
  const height = Math.max(320, container.clientHeight || 0);
  const scale = Math.max(0.35, Math.min(1.4, Math.min((width - padding) / graphWidth, (height - padding) / graphHeight)));
  const x = (width - graphWidth * scale) / 2 - minX * scale;
  const y = (height - graphHeight * scale) / 2 - minY * scale;
  return { x, y, k: scale };
}

function applyViewportTransform() {
  if (!state.viewportEl) return;
  const { x, y, k } = state.currentTransform;
  state.viewportEl.setAttribute("transform", `translate(${x} ${y}) scale(${k})`);
}

function renderNodeShape(parent, node) {
  const radius = getNodeRadius(node);
  const fill = STATUS_COLORS[node.status] || STATUS_COLORS.unexplored;
  const shape = KIND_SHAPES[node.kind] || "circle";
  let shapeEl;

  if (shape === "circle") {
    shapeEl = createSvgElement("circle", { r: radius });
  } else if (shape === "rect") {
    shapeEl = createSvgElement("rect", {
      x: -radius,
      y: -radius * 0.7,
      width: radius * 2,
      height: radius * 1.4,
      rx: 4,
    });
  } else if (shape === "diamond") {
    shapeEl = createSvgElement("polygon", {
      points: `0,${-radius} ${radius},0 0,${radius} ${-radius},0`,
    });
  } else if (shape === "star") {
    const points = [];
    for (let index = 0; index < 10; index += 1) {
      const angle = -Math.PI / 2 + (index * Math.PI) / 5;
      const size = index % 2 === 0 ? radius : radius / 2;
      points.push(`${Math.cos(angle) * size},${Math.sin(angle) * size}`);
    }
    shapeEl = createSvgElement("polygon", { points: points.join(" ") });
  } else {
    const hx = radius * Math.cos(Math.PI / 6);
    shapeEl = createSvgElement("polygon", {
      points: `${hx},${-radius / 2} ${hx},${radius / 2} 0,${radius} ${-hx},${radius / 2} ${-hx},${-radius / 2} 0,${-radius}`,
    });
  }

  shapeEl.setAttribute("fill", fill);
  shapeEl.setAttribute("stroke", "#ffffff");
  shapeEl.setAttribute("stroke-width", "2");
  parent.appendChild(shapeEl);

  const label = createSvgElement("text", {
    x: 0,
    y: radius + 16,
    "text-anchor": "middle",
    "font-size": "11",
    fill: "#e5e7eb",
  });
  const title = String(node.title || "");
  label.textContent = title.length > 22 ? `${title.slice(0, 20)}…` : title;
  parent.appendChild(label);
}

function showTooltip(event, node) {
  const fields = [
    ["key", node.key],
    ["parent_key", node.parent_key],
    ["title", node.title],
    ["kind", node.kind],
    ["status", node.status],
    ["priority", node.priority],
    ["reason", node.reason],
  ].filter(([, value]) => value);

  tooltip.innerHTML = fields
    .map(([key, value]) => `<div class="tip-row"><span class="tip-key">${escapeHtml(key)}:</span> <span class="tip-val">${escapeHtml(value)}</span></div>`)
    .join("");
  tooltip.classList.remove("hidden");
  moveTooltip(event);
}

function moveTooltip(event) {
  if (tooltip.classList.contains("hidden")) return;
  const rect = container.getBoundingClientRect();
  let x = event.clientX - rect.left + 15;
  let y = event.clientY - rect.top + 15;
  if (x + 320 > rect.width) x -= 335;
  if (y + 220 > rect.height) y -= 235;
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
    setGraphState("No nodes available yet.", "empty");
    return;
  }

  const viewport = createSvgElement("g", { class: "viewport" });
  const edgesGroup = createSvgElement("g", { class: "edges" });
  const nodesGroup = createSvgElement("g", { class: "nodes" });
  viewport.appendChild(edgesGroup);
  viewport.appendChild(nodesGroup);
  svg.appendChild(viewport);
  state.viewportEl = viewport;

  // Render edges first (below nodes)
  const nodeMap = new Map(layout.nodes.map((n) => [n.key, n]));

  // Render parent-child edges (solid white)
  layout.edges.forEach((edge) => {
    const fromNode = nodeMap.get(edge.from);
    const toNode = nodeMap.get(edge.to);
    if (fromNode && toNode) {
      const dx = toNode.x - fromNode.x;
      const dy = toNode.y - fromNode.y;
      const cx = fromNode.x + dx * 0.5;
      const cy = fromNode.y + dy * 0.5 + Math.sign(dy) * Math.abs(dx) * 0.3;
      const path = createSvgElement("path", {
        d: `M ${fromNode.x} ${fromNode.y} Q ${cx} ${cy} ${toNode.x} ${toNode.y}`,
        stroke: "#ffffff",
        "stroke-width": "3",
        "stroke-opacity": "0.9",
        fill: "none",
      });
      edgesGroup.appendChild(path);
    }
  });

  // Render shared_refs edges (dashed gray)
  layout.nodes.forEach((node) => {
    (node.shared_refs || []).forEach((refKey) => {
      const refNode = nodeMap.get(refKey);
      if (refNode) {
        const dx = refNode.x - node.x;
        const dy = refNode.y - node.y;
        const cx = node.x + dx * 0.5;
        const cy = node.y + dy * 0.5 + Math.sign(dy) * Math.abs(dx) * 0.3;
        const path = createSvgElement("path", {
          d: `M ${node.x} ${node.y} Q ${cx} ${cy} ${refNode.x} ${refNode.y}`,
          stroke: "#9ca3af",
          "stroke-width": "1.5",
          "stroke-opacity": "0.6",
          "stroke-dasharray": "6 4",
          fill: "none",
        });
        edgesGroup.appendChild(path);
      }
    });
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
  if (!runId) return false;
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
  if (!runId) return false;
  runIdInput.value = runId;
  await loadRun(runId);
  return true;
}

function connectSSE() {
  closeLive();
  runIdDisplay.textContent = "LIVE";
  setGraphState("Connecting live updates...");

  fetchJson("/current")
    .then((data) => {
      if (data && Array.isArray(data.nodes) && data.nodes.length) {
        renderTree(data);
        runIdDisplay.textContent = "LIVE";
      } else if (!state.treeData) {
        setGraphState("Waiting for live data...", "empty");
      }
    })
    .catch(() => {
      if (!state.treeData) {
        setGraphState("Waiting for live data...", "empty");
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
  if (event.target.closest(".node")) return;
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
  if (!state.isPanning || !state.panStart) return;
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
    if (!state.viewportEl) return;
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
