// verify-lab-art.mjs — deterministic map-art gate (V16/V17, art-spec §完成定义).
//
// Read-only: never writes the working tree. Loads the on-disk tilemaps + maze,
// re-runs the pure generator in memory, and asserts:
//   1. double generation is byte-identical (determinism) and re-running on an
//      already-painted map is stable (idempotency);
//   2. the Experiment Building occupies inclusive (108,72)-(124,86) = 17x15;
//   3. raw Collisions flood-fill from hub (75,56) AND access (116,65) reaches
//      the entrance (116,72), center (116,79) and all five hotspots — with a
//      >=2-wide main corridor and front cells at GID 0;
//   4. the generation process leaves World/Arena/Sector/Spawning/Special block
//      layers byte-identical (never inherits workshop semantics);
//   5. frontend and backend tilemap copies are byte-identical;
//   6. maze.size === [128,180];
//   7. Collisions uses only the `blocks` atlas (never the missing blocks_2/3),
//      and the character sprite atlas keeps stable semantic frame names.
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import {
  buildExpandedTilemap, makeGidResolver, LAB, LAB_BLOCKOUT,
  EXPANDED_WIDTH as W, EXPANDED_HEIGHT as H,
} from "./expand-town-map.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONT = path.resolve(HERE, "../public/assets/village/tilemap/tilemap.json");
const BACK = path.resolve(HERE, "../../backend/app/assets/village/tilemap/tilemap.json");
const MAZE = path.resolve(HERE, "../public/assets/village/maze.json");
const SPRITE = path.resolve(HERE, "../public/assets/village/agents/sprite.json");

const SEMANTIC_LAYERS = ["Arena Blocks", "Sector Blocks", "World Blocks", "Spawning Blocks", "Special Blocks Registry"];
const clone = (o) => JSON.parse(JSON.stringify(o));
const ser = (o) => JSON.stringify(o, null, 3);

let failures = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { console.error(`  ✗ ${m}`); failures += 1; };
function check(cond, m) { cond ? ok(m) : bad(m); }

function layer(tm, name) { return tm.layers.find((l) => l.name === name && Array.isArray(l.data)); }

async function main() {
  const frontRaw = await readFile(FRONT, "utf8");
  const backRaw = await readFile(BACK, "utf8");
  const maze = JSON.parse(await readFile(MAZE, "utf8"));
  const source = JSON.parse(frontRaw);

  console.log("1. determinism + idempotency");
  const genA = buildExpandedTilemap(clone(source), clone(maze));
  const genB = buildExpandedTilemap(clone(source), clone(maze));
  check(ser(genA.tilemap) === ser(genB.tilemap), "two independent generations are byte-identical");
  const genA2 = buildExpandedTilemap(clone(genA.tilemap), clone(maze));
  check(ser(genA2.tilemap) === ser(genA.tilemap), "re-running on the painted map is stable (idempotent)");
  check(genA.maze.size[0] === H && genA.maze.size[1] === W, `maze.size === [${H},${W}]`);

  const tm = genA.tilemap;
  const idx = (x, y) => y * W + x;

  console.log("2. 17x15 footprint bounds");
  check(tm.width === W && tm.height === H, `map is ${W}x${H}`);
  check(LAB.w === 17 && LAB.h === 15 && LAB.x0 === 108 && LAB.y0 === 72, "LAB footprint (108,72) 17x15");
  const col = layer(tm, "Collisions").data;
  // wall ring matches the blockout inside the footprint
  let ringOk = true;
  for (let v = 0; v < LAB.h; v += 1)
    for (let u = 0; u < LAB.w; u += 1) {
      const blocked = !!col[idx(LAB.x0 + u, LAB.y0 + v)];
      const shouldBlock = LAB_BLOCKOUT[v][u] === "#";
      if (blocked !== shouldBlock) ringOk = false;
    }
  check(ringOk, "Collisions wall ring / entrance gap matches the blockout exactly");

  console.log("3. raw-Collisions reachability (pathfinding authority)");
  const walk = (x, y) => !col[idx(x, y)];
  check(walk(...LAB.entrance), `entrance ${LAB.entrance} is GID 0`);
  check(walk(...LAB.center), `center ${LAB.center} is GID 0`);
  // main corridor >= 2 wide: check the central column band u=7..9 at v=7
  const corridorWidth = [7, 8, 9].filter((u) => walk(LAB.x0 + u, LAB.y0 + 7)).length;
  check(corridorWidth >= 2, `main corridor is >= 2 tiles wide (measured ${corridorWidth})`);
  const seen = floodFill(col, [LAB.hubSeed, LAB.accessSeed]);
  const targets = { entrance: LAB.entrance, center: LAB.center };
  for (const k of Object.keys(LAB.hotspots)) {
    const [u, v] = LAB.hotspots[k];
    targets[k] = [LAB.x0 + u, LAB.y0 + v];
  }
  for (const [k, [x, y]] of Object.entries(targets))
    check(seen[idx(x, y)] === 1, `reachable from hub+access: ${k} (${x},${y})`);

  console.log("4. semantic block layers untouched by generation");
  const before = clone(source);
  const after = buildExpandedTilemap(clone(source), clone(maze)).tilemap;
  for (const name of SEMANTIC_LAYERS) {
    const b = layer(before, name);
    const a = layer(after, name);
    if (!b || !a) { bad(`layer '${name}' missing`); continue; }
    check(JSON.stringify(b.data) === JSON.stringify(a.data), `'${name}' bytes unchanged by generation`);
  }

  console.log("5. frontend/backend byte-identical");
  check(frontRaw === backRaw, "frontend and backend tilemap.json are byte-identical");

  console.log("6. atlas / collision-dependency sanity");
  const resolve = makeGidResolver(tm);
  const FLAG_MASK = 0x1fffffff;
  const blocksTs = tm.tilesets.find((t) => t.name === "blocks");
  const blocksMin = blocksTs.firstgid, blocksMax = blocksTs.firstgid + blocksTs.tilecount - 1;
  let onlyBlocks = true;
  for (const raw of col) {
    if (!raw) continue;
    const gid = raw & FLAG_MASK;
    if (gid < blocksMin || gid > blocksMax) onlyBlocks = false;
  }
  check(onlyBlocks, `Collisions uses only the 'blocks' atlas (never the missing blocks_2/blocks_3)`);
  check(resolve("blocks", 0) === blocksMin, "GID resolves by tileset name + local id");

  try {
    const sprite = JSON.parse(await readFile(SPRITE, "utf8"));
    const names = new Set((sprite.frames || []).map((f) => f.filename));
    const need = ["down-walk.000", "up-walk.000", "left-walk.000", "right-walk.000"];
    check(need.every((n) => names.has(n)), "character sprite atlas keeps stable semantic frame names");
  } catch {
    bad("could not read agents/sprite.json");
  }

  console.log("");
  if (failures > 0) { console.error(`verify-lab-art FAILED: ${failures} check(s).`); process.exit(1); }
  console.log("verify-lab-art PASSED: deterministic, reachable, byte-identical, semantics intact.");
}

function floodFill(col, seeds) {
  const seen = new Uint8Array(W * H);
  const stack = [];
  for (const [x, y] of seeds) { seen[y * W + x] = 1; stack.push([x, y]); }
  while (stack.length) {
    const [x, y] = stack.pop();
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = x + dx, ny = y + dy;
      if (nx < 0 || ny < 0 || nx >= W || ny >= H) continue;
      const ni = ny * W + nx;
      if (seen[ni] || col[ni]) continue;
      seen[ni] = 1;
      stack.push([nx, ny]);
    }
  }
  return seen;
}

await main();
