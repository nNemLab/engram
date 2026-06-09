# Engram brand assets

The Engram signature mark is a stylized **neuron** — radiating dendrites in a
magenta→violet gradient around a cyan synaptic core. It reads as a memory trace
(an *engram*): the thing the platform exists to store.

| Token | Value |
|---|---|
| Gradient start | `#FF2E9A` (magenta) |
| Gradient end | `#8A2BFF` (violet) |
| Synaptic core | `#23E8FF` (cyan) |
| Ink (text on light) | `#0a0912` |
| Paper (text on dark) | `#f4f1ea` |
| Wordmark typeface | Plus Jakarta Sans, 600 |

## Files

**Lockups** (mark + wordmark) — `-ink` for light backgrounds, `-white` for dark:

- `engram-lockup-horizontal-{ink,white}.{svg,png}` — primary, used in the README header
- `engram-lockup-stacked-{ink,white}.{svg,png}` — vertical variant for narrow spaces

**Mark only** (the neuron, no wordmark):

- `engram-mark.svg` — full-color, scalable
- `engram-mark-mono-{dark,white}.svg` — single-color silhouettes
- `engram-mark-{16,32,48,64,256,512}.png` — rasterized sizes

**App / platform icons:**

- `engram-avatar-{512,1024}.png` — square avatar on a dark plate (GitHub org / social)
- `engram-appletouch-180.png` — Apple touch icon
- `favicon.ico` — multi-resolution favicon

## Usage

- Prefer the **SVG** lockups/mark anywhere they'll render (docs, web). Use PNGs
  only where SVG isn't supported.
- Keep clear space around the mark equal to the core diameter; don't recolor the
  gradient or the cyan core.
- On dark surfaces use the `-white` lockup or `engram-mark-mono-white.svg`; on
  light surfaces use `-ink` / `engram-mark-mono-dark.svg`.
