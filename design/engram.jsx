import { useState, useEffect, useRef } from "react";

const TREE = {
  id: "kernel",
  label: "Claude Code",
  sub: "Engram Kernel",
  icon: "⬡",
  color: "#00ff9d",
  desc: "Central orchestrator. Manages tool dispatch, context windows, multi-turn reasoning, and subprocess spawning across all branches.",
  detail: ["Agentic task decomposition", "MCP server orchestration", "Bash / filesystem / git native", "Session persistence & checkpointing"],
  children: [
    {
      id: "rag",
      label: "RAG Engine",
      sub: "Retrieval-Augmented Generation",
      icon: "◈",
      color: "#ff6b6b",
      desc: "Hybrid retrieval pipeline combining dense vector search with sparse BM25 scoring over local knowledge stores.",
      detail: ["RRF reranking", "Context budget-aware injection"],
      children: [
        { id: "rag-ingest", label: "Ingest Pipeline", sub: "Document Processing", icon: "▾", color: "#ff6b6b", desc: "Watches Obsidian vault for changes. Chunks documents using semantic + sliding window strategies.", detail: ["Obsidian vault watcher", "Semantic chunking", "Sliding window fallback", "Metadata extraction"], children: [] },
        { id: "rag-vector", label: "Vector Store", sub: "Dense Retrieval", icon: "◆", color: "#ff6b6b", desc: "ChromaDB / LanceDB local embedding store with sentence-transformer models.", detail: ["ChromaDB local", "sentence-transformers", "Cosine similarity search", "Collection partitioning"], children: [] },
        { id: "rag-sparse", label: "Sparse Index", sub: "BM25 Retrieval", icon: "◇", color: "#ff6b6b", desc: "Tantivy-based BM25 index for keyword-level recall. Fused with vector results via RRF.", detail: ["tantivy engine", "BM25 scoring", "Reciprocal Rank Fusion"], children: [] },
      ]
    },
    {
      id: "memory",
      label: "Memory System",
      sub: "Persistent Agent Memory",
      icon: "◉",
      color: "#ffd93d",
      desc: "Multi-tier memory: episodic conversation logs, semantic long-term graph, and working context window state.",
      detail: ["Three-tier architecture", "Auto-summarization", "Backlink associative recall"],
      children: [
        { id: "mem-episodic", label: "Episodic Memory", sub: "Conversation Logs", icon: "▸", color: "#ffd93d", desc: "Conversation sessions logged as Obsidian daily notes with YAML frontmatter for queryability.", detail: ["Daily note format", "YAML frontmatter tags", "Session boundaries", "Dataview queryable"], children: [] },
        { id: "mem-semantic", label: "Semantic Memory", sub: "Knowledge Graph", icon: "◎", color: "#ffd93d", desc: "Entity and concept graph built from Obsidian backlinks. Consolidated from episodic logs by the memory daemon.", detail: ["Obsidian backlink graph", "Entity extraction", "Concept linking", "Templater templates"], children: [] },
        { id: "mem-working", label: "Working Memory", sub: "Context Window", icon: "▪", color: "#ffd93d", desc: "Active context window state management. Tracks what's loaded, budget remaining, and eviction priority.", detail: ["Token budget tracking", "Priority eviction", "Pinned context slots", "Scratchpad buffer"], children: [] },
      ]
    },
    {
      id: "playbooks",
      label: "Playbook System",
      sub: "Executable Runbooks",
      icon: "▣",
      color: "#a29bfe",
      desc: "Structured, reusable analysis workflows as Jupyter notebooks and Obsidian runbook templates.",
      detail: ["Parameterized execution", "Git-tracked versioning", "Output to KB ingest"],
      children: [
        { id: "pb-jupyter", label: "Jupyter Runner", sub: "Notebook Execution", icon: "▤", color: "#a29bfe", desc: "Headless notebook execution via Papermill. Parameterized cells accept inputs from Claude Code.", detail: ["Papermill headless", "nbconvert export", "Parameterized cells", "Kernel management"], children: [] },
        { id: "pb-templates", label: "Template Library", sub: "Workflow Templates", icon: "▦", color: "#a29bfe", desc: "Pre-built templates: PCAP analysis, malware triage, OSINT collection, incident response.", detail: ["PCAP analysis template", "Malware triage template", "OSINT collection", "IR playbook"], children: [] },
        { id: "pb-ground", label: "Source Grounding", sub: "NotebookLM-Style", icon: "▧", color: "#a29bfe", desc: "RAG context injected into notebook parameters before execution. Grounds outputs in retrieved sources.", detail: ["RAG context injection", "Source citation tracking", "Grounded outputs", "Claude API verification"], children: [] },
      ]
    },
    {
      id: "research",
      label: "Research System",
      sub: "Multi-Source Intelligence",
      icon: "⊙",
      color: "#74b9ff",
      desc: "Automated research: web search, academic papers, doc scraping, structured extraction with provenance.",
      detail: ["Source provenance tracking", "Dedup check before ingest"],
      children: [
        { id: "res-web", label: "Web Extraction", sub: "Firecrawl + Search", icon: "◁", color: "#74b9ff", desc: "Self-hosted Firecrawl for structured web extraction. Tavily / SearXNG for search queries.", detail: ["Firecrawl (Docker)", "Tavily API", "SearXNG instance", "Structured output"], children: [] },
        { id: "res-academic", label: "Academic Fetch", sub: "Papers & Docs", icon: "◃", color: "#74b9ff", desc: "ArXiv and Semantic Scholar paper retrieval. Documentation site crawling for technical references.", detail: ["arxiv CLI", "Semantic Scholar API", "Doc site crawler", "PDF extraction"], children: [] },
        { id: "res-provenance", label: "Provenance Tracker", sub: "Source Lineage", icon: "◂", color: "#74b9ff", desc: "Every research artifact tagged with source URL, retrieval timestamp, and confidence score.", detail: ["Source URL tagging", "Timestamp tracking", "Confidence scoring", "Citation graph"], children: [] },
      ]
    },
    {
      id: "kb",
      label: "Knowledge Base",
      sub: "Deduplicated Store",
      icon: "⬢",
      color: "#fd79a8",
      desc: "Content-addressed storage with deduplication. All subsystems write here; RAG indexes from here.",
      detail: ["Obsidian vault canonical store", "Human-browsable"],
      children: [
        { id: "kb-dedup", label: "Dedup Engine", sub: "Duplicate Detection", icon: "⬡", color: "#fd79a8", desc: "SHA-256 exact match rejection, SimHash near-duplicate detection (hamming distance < 3), auto-merge.", detail: ["SHA-256 content hash", "SimHash fingerprints", "Hamming distance < 3", "Auto-merge overlaps"], children: [] },
        { id: "kb-index", label: "Metadata Index", sub: "YAML Frontmatter", icon: "⬠", color: "#fd79a8", desc: "Every KB entry has YAML frontmatter: source, timestamp, staleness score, tags, lineage pointers.", detail: ["YAML frontmatter", "Staleness scoring", "Tag taxonomy", "Lineage pointers"], children: [] },
        { id: "kb-refresh", label: "Refresh Daemon", sub: "Staleness Management", icon: "⬟", color: "#fd79a8", desc: "Cron job scores entries for staleness. Triggers re-fetch from Research when score exceeds threshold.", detail: ["Staleness cron job", "Threshold triggers", "Re-fetch dispatch", "TTL policies"], children: [] },
      ]
    },
    {
      id: "mcp",
      label: "MCP Layer",
      sub: "Model Context Protocol",
      icon: "⊛",
      color: "#e17055",
      desc: "MCP server mesh. Each subsystem exposes tools and resources via standardized JSON-RPC interfaces.",
      detail: ["Uniform tool access", "JSON-RPC 2.0"],
      children: [
        { id: "mcp-obsidian", label: "MCP Obsidian", sub: "Vault Access", icon: "⊙", color: "#e17055", desc: "Read, write, search, and manage Obsidian vault notes and metadata from Claude Code.", detail: ["Vault read/write", "Full-text search", "Frontmatter queries", "Template instantiation"], children: [] },
        { id: "mcp-tools", label: "MCP Tools", sub: "CLI & Analysis", icon: "⊕", color: "#e17055", desc: "Exposes native CLI tools (radare2, tshark, binwalk, foremost) as MCP-callable operations.", detail: ["radare2 / binwalk", "tshark / tcpdump", "foremost carving", "Custom bash wrappers"], children: [] },
        { id: "mcp-custom", label: "Custom Servers", sub: "Extension Point", icon: "⊗", color: "#e17055", desc: "User-defined MCP servers for new integrations. TypeScript SDK, stdio or SSE transport.", detail: ["MCP SDK (TS)", "stdio transport", "SSE transport", "Hot-reload dev"], children: [] },
      ]
    },
  ]
};

const AUTOMATION = {
  label: "Automation Layer",
  sub: "Orchestrated Pipelines Spanning All Branches",
  color: "#00cec9",
  items: [
    { id: "auto-cron", label: "Cron Scheduler", desc: "Timed triggers: memory consolidation, staleness refresh, vault re-index, report gen.", icon: "⏱" },
    { id: "auto-watch", label: "FS Watcher", desc: "inotifywait on vault, inbox, drop zones. Triggers ingest on file change.", icon: "👁" },
    { id: "auto-hook", label: "Git Hooks", desc: "Pre-commit: lint. Post-commit: re-index KB. Push: sync remote backup.", icon: "⎇" },
    { id: "auto-queue", label: "Task Queue", desc: "Task spooler (ts) for background: crawls, embeddings, batch notebooks.", icon: "☰" },
    { id: "auto-pipe", label: "Pipeline Composer", desc: "Chain subsystems: Research→Dedup→KB→RAG→Notify. YAML specs.", icon: "⟿" },
    { id: "auto-health", label: "Health Monitor", desc: "Watchdog for MCP, vector DB, Jupyter. Auto-restart, alert on degrade.", icon: "♥" },
  ]
};

const LY = { kernel: 55, branch: 180, leaf: 315, autoBar: 435, autoNodes: 470 };

function Edge({ x1, y1, x2, y2, color }) {
  const my = y1 + (y2 - y1) * 0.55;
  return <path d={`M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`} fill="none" stroke={`${color}30`} strokeWidth="1" />;
}

function Dot({ x1, y1, x2, y2, color, delay }) {
  const my = y1 + (y2 - y1) * 0.55;
  return (
    <circle r="1.8" fill={color} opacity="0.65">
      <animateMotion dur="3s" begin={`${delay}s`} repeatCount="indefinite" path={`M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`} />
    </circle>
  );
}

function DetailSidebar({ node, onClose }) {
  if (!node) return null;
  return (
    <div style={{
      position: "absolute", top: 0, right: 0, width: 330, height: "100%",
      background: "linear-gradient(180deg,#0b0b12,#0e0e16)",
      borderLeft: `1px solid ${node.color}30`,
      padding: "24px 20px", overflowY: "auto", zIndex: 20,
      animation: "sIn .2s ease-out",
    }}>
      <style>{`@keyframes sIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}`}</style>
      <button onClick={onClose} style={{ position:"absolute",top:14,right:14,background:"none",border:"none",color:"#555",fontSize:18,cursor:"pointer",fontFamily:"monospace" }}>✕</button>
      <span style={{ fontSize: 26, color: node.color }}>{node.icon}</span>
      <div style={{ color: node.color, fontSize: 16, fontWeight: 700, fontFamily: "'JetBrains Mono',monospace", marginTop: 6 }}>{node.label}</div>
      <div style={{ color: "#777", fontSize: 10, fontFamily: "monospace", marginBottom: 12 }}>{node.sub}</div>
      <div style={{ height: 1, background: `linear-gradient(90deg,${node.color}40,transparent)`, marginBottom: 14 }} />
      <p style={{ color: "#bbb", fontSize: 12.5, lineHeight: 1.7, margin: "0 0 16px" }}>{node.desc}</p>
      {node.detail && node.detail.map((d, i) => (
        <div key={i} style={{ display: "flex", gap: 8, marginBottom: 6, padding: "5px 8px", borderRadius: 4, background: i%2===0 ? "#ffffff05" : "transparent" }}>
          <span style={{ color: node.color, fontSize: 9, marginTop: 2 }}>▸</span>
          <span style={{ color: "#ccc", fontSize: 11.5, fontFamily: "'IBM Plex Mono',monospace" }}>{d}</span>
        </div>
      ))}
    </div>
  );
}

export default function EngramTree() {
  const [selected, setSelected] = useState(null);
  const ref = useRef(null);
  const [dims, setDims] = useState({ w: 1100, h: 580 });

  useEffect(() => {
    const obs = new ResizeObserver(entries => {
      for (const e of entries) setDims({ w: e.contentRect.width, h: e.contentRect.height });
    });
    if (ref.current) obs.observe(ref.current);
    return () => obs.disconnect();
  }, []);

  const panelW = selected ? 330 : 0;
  const svgW = dims.w - panelW;
  const branches = TREE.children;
  const bSpacing = svgW / (branches.length + 1);

  const pos = {};
  const kernelX = svgW / 2;
  pos[TREE.id] = { x: kernelX, y: LY.kernel };

  branches.forEach((b, i) => {
    const bx = bSpacing * (i + 1);
    pos[b.id] = { x: bx, y: LY.branch };
    const lc = b.children.length;
    const zone = 128;
    const totalW = lc * zone;
    const start = bx - totalW / 2 + zone / 2;
    b.children.forEach((l, j) => { pos[l.id] = { x: start + j * zone, y: LY.leaf }; });
  });

  const allNodes = {};
  allNodes[TREE.id] = TREE;
  branches.forEach(b => { allNodes[b.id] = b; b.children.forEach(l => { allNodes[l.id] = l; }); });
  AUTOMATION.items.forEach(a => { allNodes[a.id] = { ...a, color: AUTOMATION.color, sub: "", detail: [a.desc] }; });

  const selectedNode = selected ? allNodes[selected] : null;
  const autoSpacing = svgW / (AUTOMATION.items.length + 1);

  return (
    <div ref={ref} style={{
      width: "100%", height: "100vh", background: "#08080d",
      position: "relative", overflow: "hidden",
      fontFamily: "'IBM Plex Mono','JetBrains Mono',monospace",
    }}>
      <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet" />

      {/* grid */}
      <svg style={{ position:"absolute",inset:0,width:"100%",height:"100%",opacity:0.035 }}>
        <defs><pattern id="g" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M50 0L0 0 0 50" fill="none" stroke="#fff" strokeWidth=".5"/></pattern></defs>
        <rect width="100%" height="100%" fill="url(#g)"/>
      </svg>

      {/* layer labels */}
      {[
        { y: LY.kernel - 5, l: "KERNEL", c: "#00ff9d" },
        { y: LY.branch - 5, l: "SUBSYSTEMS", c: "#888" },
        { y: LY.leaf - 5, l: "COMPONENTS", c: "#666" },
        { y: LY.autoBar + 10, l: "AUTOMATION", c: "#00cec9" },
      ].map(r => (
        <div key={r.l} style={{ position:"absolute",left:6,top:r.y,color:`${r.c}60`,fontSize:8,fontFamily:"monospace",letterSpacing:2,writingMode:"vertical-rl",transform:"rotate(180deg)",zIndex:2 }}>{r.l}</div>
      ))}

      {/* title */}
      <div style={{ position:"absolute",top:10,left:28,zIndex:5 }}>
        <span style={{ fontSize:18,fontWeight:700,color:"#00ff9d",letterSpacing:3 }}>AGENTIC</span>
        <span style={{ fontSize:18,fontWeight:300,color:"#444",letterSpacing:3 }}>OS</span>
        <div style={{ fontSize:9,color:"#3a3a3a",letterSpacing:1,marginTop:1 }}>hierarchical architecture · click any node to inspect</div>
      </div>

      <svg width={svgW} height={dims.h} style={{ transition:"width .25s" }}>
        {/* layer dividers */}
        {[130, 255, 400].map(y => <line key={y} x1={24} y1={y} x2={svgW - 8} y2={y} stroke="#ffffff05" strokeWidth="1" strokeDasharray="2 8" />)}

        {/* kernel → branch edges */}
        {branches.map((b, i) => (
          <g key={`ek-${b.id}`}>
            <Edge x1={kernelX} y1={LY.kernel + 22} x2={pos[b.id].x} y2={LY.branch - 34} color={b.color} />
            <Dot x1={kernelX} y1={LY.kernel + 22} x2={pos[b.id].x} y2={LY.branch - 34} color={b.color} delay={i * 0.4} />
          </g>
        ))}

        {/* branch → leaf edges */}
        {branches.map(b => b.children.map((l, j) => (
          <g key={`el-${l.id}`}>
            <Edge x1={pos[b.id].x} y1={LY.branch + 34} x2={pos[l.id].x} y2={LY.leaf - 28} color={b.color} />
            <Dot x1={pos[b.id].x} y1={LY.branch + 34} x2={pos[l.id].x} y2={LY.leaf - 28} color={b.color} delay={j * 0.6 + 0.2} />
          </g>
        )))}

        {/* leaf → automation drip lines */}
        {branches.map(b => b.children.map(l => (
          <line key={`da-${l.id}`} x1={pos[l.id].x} y1={LY.leaf + 28} x2={pos[l.id].x} y2={LY.autoBar - 4} stroke="#00cec910" strokeWidth=".6" strokeDasharray="2 5" />
        )))}

        {/* automation bar */}
        <rect x={24} y={LY.autoBar - 4} width={svgW - 32} height={80} rx={6} fill="#00cec904" stroke="#00cec918" strokeWidth="1" />
        <line x1={28} y1={LY.autoBar - 3} x2={svgW - 36} y2={LY.autoBar - 3} stroke="#00cec930" strokeWidth="1.5" />

        {/* automation nodes */}
        {AUTOMATION.items.map((a, i) => {
          const ax = autoSpacing * (i + 1);
          const active = selected === a.id;
          return (
            <g key={a.id} style={{ cursor:"pointer" }} onClick={() => setSelected(a.id)}>
              <rect x={ax - 50} y={LY.autoNodes - 22} width={100} height={44} rx={4}
                fill={active ? "#00cec90e" : "transparent"} stroke={active ? "#00cec9" : "#00cec918"} strokeWidth={active ? 1.2 : .5} />
              <text x={ax} y={LY.autoNodes - 2} textAnchor="middle" fill="#00cec9" fontSize={13} fontFamily="monospace">{a.icon}</text>
              <text x={ax} y={LY.autoNodes + 12} textAnchor="middle" fill="#bbb" fontSize={7.5} fontWeight="600" fontFamily="'IBM Plex Mono',monospace">{a.label}</text>
            </g>
          );
        })}

        {/* kernel node */}
        <g style={{ cursor:"pointer" }} onClick={() => setSelected(TREE.id)}>
          <rect x={kernelX - 85} y={LY.kernel - 22} width={170} height={44} rx={7}
            fill={selected === TREE.id ? "#00ff9d0d" : "#0c0c12"} stroke={selected === TREE.id ? "#00ff9d" : "#00ff9d45"} strokeWidth={selected === TREE.id ? 1.8 : 1} />
          <text x={kernelX} y={LY.kernel - 2} textAnchor="middle" fill="#00ff9d" fontSize={14} fontWeight="700" fontFamily="'JetBrains Mono',monospace">{TREE.icon}  {TREE.label}</text>
          <text x={kernelX} y={LY.kernel + 13} textAnchor="middle" fill="#555" fontSize={7.5} fontFamily="monospace">{TREE.sub}</text>
        </g>

        {/* branch nodes */}
        {branches.map(b => {
          const p = pos[b.id]; const act = selected === b.id;
          return (
            <g key={b.id} style={{ cursor:"pointer" }} onClick={() => setSelected(b.id)}>
              <rect x={p.x - 62} y={LY.branch - 34} width={124} height={68} rx={5}
                fill={act ? `${b.color}0d` : "#0c0c12"} stroke={act ? b.color : `${b.color}35`} strokeWidth={act ? 1.4 : .7} />
              <text x={p.x} y={LY.branch - 12} textAnchor="middle" fill={b.color} fontSize={15} fontFamily="monospace">{b.icon}</text>
              <text x={p.x} y={LY.branch + 4} textAnchor="middle" fill="#ddd" fontSize={9} fontWeight="600" fontFamily="'IBM Plex Mono',monospace">{b.label}</text>
              <text x={p.x} y={LY.branch + 17} textAnchor="middle" fill="#555" fontSize={6.5} fontFamily="monospace">{b.sub}</text>
            </g>
          );
        })}

        {/* leaf nodes */}
        {branches.map(b => b.children.map(l => {
          const p = pos[l.id]; const act = selected === l.id;
          return (
            <g key={l.id} style={{ cursor:"pointer" }} onClick={() => setSelected(l.id)}>
              <rect x={p.x - 56} y={LY.leaf - 28} width={112} height={56} rx={4}
                fill={act ? `${l.color}0d` : "#0b0b10"} stroke={act ? l.color : `${l.color}25`} strokeWidth={act ? 1.2 : .5} />
              <text x={p.x} y={LY.leaf - 8} textAnchor="middle" fill={l.color} fontSize={12} fontFamily="monospace">{l.icon}</text>
              <text x={p.x} y={LY.leaf + 5} textAnchor="middle" fill="#ccc" fontSize={7.5} fontWeight="600" fontFamily="'IBM Plex Mono',monospace">{l.label}</text>
              <text x={p.x} y={LY.leaf + 16} textAnchor="middle" fill="#555" fontSize={6} fontFamily="monospace">{l.sub}</text>
            </g>
          );
        }))}
      </svg>

      {selectedNode && <DetailSidebar node={selectedNode} onClose={() => setSelected(null)} />}
    </div>
  );
}
