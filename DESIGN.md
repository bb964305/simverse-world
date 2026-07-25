# Simverse World — Marketing Site Design System

## 0. Research Log

- Embedded refs: shortlisted `voltagent` (void-black + emerald terminal), `raycast` (dark chrome + gradient glow), `runwayml` (cinematic dark media) → picked **Layer A `soft-skill`** + **Layer B `voltagent`** remapped to product neon (cyan/magenta) because Simverse is a living AI world, not a SaaS dashboard — needs dark depth + one luminous accent story.
- Lazyweb: skipped — network research optional; product screenshots in `assets/screenshots/` used as primary visual truth.
- Imagen drafts: skipped — existing game/forge screenshots serve as reference-fidelity product frames.
- Product DNA extracted from app: `--bg-page #0f0f17`, accent cyan `#0ea5e9`, soul-red `#e94560`, zinc surfaces, pixel isometric world.

## 1. Atmosphere & Identity

A night city that never sleeps — dense, electric, slightly haunted by memory. Surfaces feel like wet glass over neon alleys: tinted panels, thin cyan rims, magenta heat in the distance. The signature is **living neon skyline** — a deep void canvas with a luminous cyan-magenta atmospheric band, a soft pixel grid underfoot, and product frames that look like windows into an always-on world.

One memorable moment: the hero headline “赛博永生开放世界” floating above a glowing city-band, with a primary CTA that blooms cyan light on hover.

## 2. Color

### Palette

| Role | Token | Dark | Usage |
|------|-------|------|-------|
| Surface/void | `--mkt-void` | `#07070d` | Page background |
| Surface/primary | `--mkt-surface` | `#0f0f17` | Sections |
| Surface/elevated | `--mkt-elevated` | `#14141f` | Cards, nav glass |
| Surface/glass | `--mkt-glass` | `rgba(20,20,32,0.72)` | Frosted panels |
| Text/primary | `--mkt-text` | `#f4f4f8` | Headlines, body |
| Text/secondary | `--mkt-text-2` | `#a1a1b5` | Supporting copy |
| Text/muted | `--mkt-text-3` | `#6b6b80` | Meta, captions |
| Border/default | `--mkt-border` | `rgba(255,255,255,0.08)` | Dividers |
| Border/glow | `--mkt-border-glow` | `rgba(14,165,233,0.35)` | Focused / hover rims |
| Accent/cyan | `--mkt-cyan` | `#22d3ee` | Primary CTAs, links |
| Accent/cyan-deep | `--mkt-cyan-deep` | `#0ea5e9` | Gradients, icons |
| Accent/magenta | `--mkt-magenta` | `#e94560` | Secondary heat, badges |
| Accent/violet | `--mkt-violet` | `#8b5cf6` | Tertiary glow |
| Status/success | `--mkt-ok` | `#53d769` | Live indicators |
| Ramp cyan | `--mkt-cyan-ramp` | `#67e8f9 → #22d3ee → #0ea5e9 → #0369a1` | Perceptual accent ramp |
| Ramp magenta | `--mkt-mag-ramp` | `#fb7185 → #e94560 → #be123c` | Heat ramp |

### Rules

- Void + glass depth first; borders are optional and always low-opacity.
- Cyan is the interactive accent; magenta is atmosphere / rare emphasis only.
- Never use flat purple-blue SaaS gradients as the whole page fill.

## 3. Typography

### Scale

| Level | Size | Weight | Line Height | Tracking | Usage |
|-------|------|--------|-------------|----------|-------|
| Display | clamp(2.5rem, 6vw, 4.25rem) | 800 | 1.05 | -0.03em | Hero |
| H1 | clamp(1.75rem, 3vw, 2.5rem) | 700 | 1.15 | -0.02em | Section titles |
| H2 | 1.375rem | 650 | 1.3 | -0.01em | Card titles |
| Body/lg | 1.125rem | 400 | 1.65 | 0 | Lead copy |
| Body | 1rem | 400 | 1.6 | 0 | Default |
| Caption | 0.75rem | 600 | 1.4 | 0.06em | Eyebrows, labels (uppercase ok) |
| Mono | 0.8125rem | 500 | 1.5 | 0 | Tech chips, stats |

### Fonts

- Display / UI: `"Space Grotesk", "Inter", system-ui, sans-serif`
- Mono: `"JetBrains Mono", ui-monospace, monospace`

## 4. Spacing

Base unit: 4px. Scale: 4 / 8 / 12 / 16 / 24 / 32 / 48 / 64 / 96 / 128.

- Section vertical padding: `96px` desktop, `64px` tablet, `48px` mobile
- Content max width: `1120px`
- Card gap: `16–24px`
- Nav height: `64px`

## 5. Components (Primitives)

### Button / Primary

- Fill: linear cyan ramp (`#22d3ee → #0ea5e9`)
- Text: `#041016`, weight 700
- Radius: `999px` (pill)
- Padding: `12px 22px`
- Hover: brighter cyan + outer glow `0 0 24px rgba(34,211,238,0.45)`
- Active: scale 0.98
- Focus-visible: 2px cyan ring offset 2px

### Button / Ghost

- Transparent fill, border `var(--mkt-border)`, text primary
- Hover: border cyan glow + glass fill

### Nav

- Sticky glass bar: `backdrop-filter: blur(16px) saturate(1.2)`, bottom hairline border
- Logo wordmark left; links center-right; CTA right

### Feature Card

- Elevated glass surface, 1px border, inner top highlight
- Icon in 40px rounded square with cyan/magenta soft glow
- Hover: lift `translateY(-2px)` + stronger rim (transform only)

### Media Frame

- Rounded 16px, 1px border, outer cyan-magenta dual shadow
- Aspect media with subtle scanline overlay (opacity ≤ 0.06)

### Badge / Chip

- Mono caption, pill, border glow, optional live-dot

### Footer

- Void surface, muted links, thin top border

## 6. Motion

- Duration: 160ms UI, 280ms section reveals
- Easing: `cubic-bezier(0.22, 1, 0.36, 1)`
- GPU only: `transform`, `opacity`, `filter`
- Signature: hero atmospheric band slow pulse (opacity 0.55↔0.85, 6s)
- No decorative motion on non-interactive static cards beyond hover affordance

## 7. Depth & Material

Glass elevation recipe (from voltagent dark chrome, remapped):

1. Base fill `rgba(20,20,32,0.72)`
2. Backdrop blur 16–20px + saturate 1.2
3. Border `rgba(255,255,255,0.08)`
4. Top edge highlight `inset 0 1px 0 rgba(255,255,255,0.06)`
5. Optional outer glow `0 0 40px rgba(14,165,233,0.12)`

Atmosphere band: radial gradients cyan + magenta at 20–40% opacity over void.

Pixel grid: 32px CSS grid lines at 3–4% white opacity.

## 8. Accessibility & Accepted Debt

### Accessibility

- Contrast: body text ≥ 4.5:1 on void; cyan CTAs use dark text for ≥ 4.5:1
- Focus-visible on all interactive controls
- Prefer reduced motion: disable hero pulse and hover lifts
- Landmark structure: header / main / footer; skip link optional
- Images: meaningful alt for product screenshots

### Accepted Debt

- Marketing tokens shared via `styles/marketing-tokens.css` (`.mkt` scopes layout/components)
- Google Fonts loaded from `index.html` with preconnect + `display=swap` (self-host optional later)
- Marketing media optimized to WebP + compressed WebM under `frontend/public/marketing/`
- Landing content centralized in `pages/landing/content.ts`; full multi-file component tree optional later
- react-grab / react-scan install deferred unless user wants dev tooling on this pass
- Mobile menu uses sticky panel (not full focus-trap dialog) — acceptable for marketing nav

## 9. Site IA (content jobs)

| Section | Job |
|---------|-----|
| Nav | navigate + convert |
| Hero | hook |
| Live strip | prove (world is alive) |
| Features | explain |
| Showcase | prove (product frames) |
| How it works | explain path to value |
| Stack | prove (tech credibility) |
| Final CTA | convert |
| Footer | navigate + retain |
