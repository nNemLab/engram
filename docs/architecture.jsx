import React from "react";

/**
 * EngramArchitecture — embeddable architecture diagram for Engram.
 *
 * Self-contained React + inline SVG. No external dependencies. Drop it into any
 * React / Next.js / Vite / Docusaurus-MDX site to render the diagram in a
 * browser (it replaces the ASCII version):
 *
 *   import EngramArchitecture from "./architecture.jsx";
 *   <EngramArchitecture />
 *
 * It scales to its container (responsive viewBox) and ships its own colors, so
 * it renders consistently light/dark-agnostic on a white backdrop.
 */

const C = {
  kernel: "#6366f1",
  log: "#0ea5e9",
  view: "#14b8a6",
  human: "#f59e0b",
  source: "#a855f7",
  text: "#0f172a",
  sub: "#475569",
  line: "#94a3b8",
  bg: "#ffffff",
};

function Node({ x, y, w, h, color, title, subtitle }) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx="10" fill="#fff" stroke={color} strokeWidth="2" />
      <rect x={x} y={y} width="6" height={h} rx="3" fill={color} />
      <text
        x={x + w / 2}
        y={subtitle ? y + h / 2 - 3 : y + h / 2 + 5}
        textAnchor="middle"
        fontSize="15"
        fontWeight="600"
        fill={C.text}
      >
        {title}
      </text>
      {subtitle && (
        <text x={x + w / 2} y={y + h / 2 + 15} textAnchor="middle" fontSize="11" fill={C.sub}>
          {subtitle}
        </text>
      )}
    </g>
  );
}

function Arrow({ d, label, lx, ly, dashed }) {
  return (
    <g>
      <path
        d={d}
        fill="none"
        stroke={C.line}
        strokeWidth="2"
        strokeDasharray={dashed ? "5 4" : undefined}
        markerEnd="url(#engram-arrow)"
      />
      {label && (
        <text x={lx} y={ly} fontSize="11" fill={C.sub} textAnchor="middle">
          {label}
        </text>
      )}
    </g>
  );
}

export default function EngramArchitecture() {
  return (
    <figure style={{ margin: 0, fontFamily: "ui-sans-serif, system-ui, sans-serif" }}>
      <svg
        viewBox="0 0 860 560"
        role="img"
        aria-label="Engram architecture: the Claude Code kernel talks over MCP to an append-only SQLite event log. The log is projected to an Obsidian vault, indexed for hybrid retrieval, and reacted to for embedding and staleness, while a poller curates external sources back into the log."
        style={{ width: "100%", height: "auto", background: C.bg }}
      >
        <defs>
          <marker
            id="engram-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" fill={C.line} />
          </marker>
        </defs>

        {/* nodes */}
        <Node x={280} y={20} w={300} h={46} color={C.kernel} title="Claude Code (kernel)" />
        <Node
          x={180}
          y={110}
          w={500}
          h={64}
          color={C.log}
          title="Event Log — SQLite, append-only"
          subtitle="ingested · merged · superseded · retrieved · edit · source_polled"
        />
        <Node x={60} y={240} w={150} h={54} color={C.view} title="Projector" subtitle="log → vault" />
        <Node x={250} y={240} w={150} h={54} color={C.view} title="RAG view" subtitle="vec0 + FTS5" />
        <Node x={440} y={240} w={150} h={54} color={C.view} title="Reactor" subtitle="embed · staleness" />
        <Node x={630} y={240} w={150} h={54} color={C.source} title="Poller" subtitle="due sources" />
        <Node x={60} y={360} w={150} h={54} color={C.human} title="Obsidian" subtitle="human edits" />
        <Node x={250} y={360} w={150} h={54} color={C.human} title="Watcher" subtitle="edits → log" />
        <Node
          x={630}
          y={360}
          w={180}
          h={64}
          color={C.source}
          title="Adapters"
          subtitle="sitemap · github-repo · mediawiki · urls"
        />

        {/* kernel <-> log */}
        <Arrow d="M430,66 L430,110" label="MCP stdio" lx={478} ly={92} />

        {/* log -> branches */}
        <Arrow d="M300,174 L150,240" />
        <Arrow d="M380,174 L325,240" />
        <Arrow d="M470,174 L515,240" />
        <Arrow d="M560,174 L700,240" />

        {/* projector -> obsidian -> watcher */}
        <Arrow d="M135,294 L135,360" />
        <Arrow d="M210,387 L250,387" />

        {/* watcher -> log (feedback, far left) */}
        <Arrow d="M250,378 L30,378 L30,142 L180,142" label="edits → log" lx={74} ly={134} dashed />

        {/* reactor -> log (feedback) */}
        <Arrow d="M515,240 L498,174" label="embed / merge" lx={590} ly={212} dashed />

        {/* poller -> adapters */}
        <Arrow d="M705,294 L715,360" />

        {/* adapters -> log (feedback, far right) */}
        <Arrow d="M810,392 L832,392 L832,142 L680,142" label="candidates → gate" lx={786} ly={134} dashed />
      </svg>
      <figcaption style={{ fontSize: 12, color: C.sub, marginTop: 8, textAlign: "center" }}>
        The event log is canonical; the vault, indexes, and entity graph are materialized views rebuildable from it.
      </figcaption>
    </figure>
  );
}
