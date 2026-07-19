// Deterministic town-map generator (V16/V17, hard-constraint #4: the map is
// only ever authored by this generator; never hand-edit the final tilemap JSON,
// and the frontend/backend copies must stay byte-identical).
//
// Split into a PURE core (`buildExpandedTilemap`, no disk I/O — importable by
// verify-lab-art.mjs) and a CLI write wrapper at the bottom. The core expands
// the original 140x100 town to 180x128 (idempotent on an already-expanded map)
// and then paints the Experiment Building blockout via `paintExperimentBuilding`.
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

export const ORIGINAL_WIDTH = 140;
export const ORIGINAL_HEIGHT = 100;
export const EXPANDED_WIDTH = 180;
export const EXPANDED_HEIGHT = 128;

// ── Experiment Building authoritative geometry (art-spec §权威坐标) ───────────
// Inclusive footprint (108,72)-(124,86) = 17x15. Local u=0..16, v=0..14.
export const LAB = {
  x0: 108, y0: 72, w: 17, h: 15,
  entrance: [116, 72], // (u8,v0)
  center: [116, 79],   // (u8,v7)
  accessPath: { x1: 115, y1: 66, x2: 117, y2: 72 }, // 3-wide north approach
  accessSeed: [116, 65], // open grass just north of the path
  hubSeed: [75, 56],     // town hub — flood-fill origin
  // Author-metadata hotspots (local u,v) — v1 has NO runtime consumer.
  hotspots: {
    task: [8, 3], sandbox: [4, 7], verify: [13, 7], archive: [4, 12], governor: [13, 12],
  },
};

// Functional blockout (art-spec §空间分区). One char per tile, 17 wide x 15 tall.
// # wall/maintenance, E entrance, C corridor, T task console, S sandbox,
// V verification, A archive, G World Governor.
export const LAB_BLOCKOUT = [
  "#######EEE#######",
  "#......CCC......#",
  "#....TTCCCTT....#",
  "#....TTCCCTT....#",
  "#SSSSSCCCCCVVVVV#",
  "#SSSSSCCCCCVVVVV#",
  "#SSSSSCCCCCVVVVV#",
  "#SSSSSCCCCCVVVVV#",
  "#SSSSSCCCCCVVVVV#",
  "#SSSSSCCCCCVVVVV#",
  "#AAAAAACCCGGGGGG#",
  "#AAAAAACCCGGGGGG#",
  "#AAAAAACCCGGGGGG#",
  "#AAAAAACCCGGGGGG#",
  "#################",
];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function extractOriginalLayer(layer, inputWidth, inputHeight) {
  assert(
    layer.width === inputWidth && layer.height === inputHeight,
    `${layer.name}: layer dimensions ${layer.width}x${layer.height} do not match the map`,
  );
  assert(
    layer.data.length === inputWidth * inputHeight,
    `${layer.name}: expected ${inputWidth * inputHeight} tiles, found ${layer.data.length}`,
  );
  const original = new Array(ORIGINAL_WIDTH * ORIGINAL_HEIGHT);
  for (let y = 0; y < ORIGINAL_HEIGHT; y += 1) {
    for (let x = 0; x < ORIGINAL_WIDTH; x += 1) {
      original[y * ORIGINAL_WIDTH + x] = layer.data[y * inputWidth + x];
    }
  }
  return original;
}

function fillRectangle(data, x1, y1, x2, y2, value) {
  for (let y = y1; y <= y2; y += 1) {
    for (let x = x1; x <= x2; x += 1) data[y * EXPANDED_WIDTH + x] = value;
  }
}

function pasteRectangle(target, source, sourceX, sourceY, targetX, targetY, width, height) {
  for (let offsetY = 0; offsetY < height; offsetY += 1) {
    for (let offsetX = 0; offsetX < width; offsetX += 1) {
      target[(targetY + offsetY) * EXPANDED_WIDTH + targetX + offsetX] =
        source[(sourceY + offsetY) * ORIGINAL_WIDTH + sourceX + offsetX];
    }
  }
}

function pasteMirroredRectangle(target, source, sourceX, sourceY, targetX, targetY, width, height) {
  for (let offsetY = 0; offsetY < height; offsetY += 1) {
    for (let offsetX = 0; offsetX < width; offsetX += 1) {
      const tile = source[(sourceY + offsetY) * ORIGINAL_WIDTH + sourceX + width - 1 - offsetX];
      target[(targetY + offsetY) * EXPANDED_WIDTH + targetX + offsetX] =
        tile === 0 ? 0 : (tile ^ 0x80000000) >>> 0;
    }
  }
}

function carveGreenConnection(data, emptyTile, rows) {
  for (const [y, x1, x2] of rows) fillRectangle(data, x1, y, x2, y, emptyTile);
}

function paintParkPath(layer, data) {
  const emptyTile = layer.name === "Bottom Ground" ? 2 : 0;
  fillRectangle(data, 157, 50, 159, 58, emptyTile);
  if (layer.name !== "Exterior Ground") return;
  const rows = [
    [50, 81, 82, 83], [51, 97, 98, 99], [52, 97, 98, 99], [53, 97, 98, 99],
    [54, 97, 98, 99], [55, 97, 98, 99], [56, 97, 98, 99], [57, 97, 98, 99],
    [58, 113, 114, 115],
  ];
  for (const [y, left, middle, right] of rows) {
    data[y * EXPANDED_WIDTH + 157] = left;
    data[y * EXPANDED_WIDTH + 158] = middle;
    data[y * EXPANDED_WIDTH + 159] = right;
  }
}

function expandLayer(layer, original) {
  const emptyTile = layer.name === "Bottom Ground" ? 2 : 0;
  const expanded = new Array(EXPANDED_WIDTH * EXPANDED_HEIGHT).fill(emptyTile);
  pasteRectangle(expanded, original, 0, 0, 0, 0, 140, 100);
  pasteRectangle(expanded, original, 100, 0, 140, 0, 40, 100);
  fillRectangle(expanded, 144, 10, 174, 65, emptyTile);
  fillRectangle(expanded, 140, 0, 143, 33, emptyTile);
  pasteRectangle(expanded, original, 24, 0, 144, 12, 27, 14);
  pasteRectangle(expanded, original, 17, 38, 145, 34, 28, 19);
  pasteRectangle(expanded, original, 49, 59, 139, 59, 35, 28);
  pasteRectangle(expanded, original, 135, 0, 175, 0, 5, 100);
  pasteRectangle(expanded, original, 0, 76, 0, 104, 140, 24);
  pasteRectangle(expanded, original, 135, 76, 175, 104, 5, 24);
  pasteRectangle(expanded, original, 16, 57, 16, 102, 28, 20);
  pasteRectangle(expanded, original, 48, 59, 48, 104, 55, 24);
  pasteMirroredRectangle(expanded, original, 68, 59, 140, 104, 35, 24);
  pasteRectangle(expanded, original, 0, 96, 0, 124, 140, 4);
  pasteMirroredRectangle(expanded, original, 68, 96, 140, 124, 35, 4);
  pasteRectangle(expanded, original, 135, 96, 175, 124, 5, 4);
  pasteRectangle(expanded, original, 119, 35, 130, 35, 17, 8);
  carveGreenConnection(expanded, emptyTile, [
    [54, 133, 144], [55, 132, 145], [56, 132, 145], [57, 133, 144],
  ]);
  carveGreenConnection(expanded, emptyTile, [
    [92, 71, 75], [93, 70, 76], [94, 70, 76], [95, 70, 76], [96, 70, 76],
    [97, 70, 76], [98, 70, 76], [99, 70, 76], [100, 70, 76], [101, 70, 76],
    [102, 70, 76], [103, 70, 76], [104, 71, 75],
  ]);
  paintParkPath(layer, expanded);
  layer.width = EXPANDED_WIDTH;
  layer.height = EXPANDED_HEIGHT;
  layer.data = expanded;
}

function verifyLayer(layer) {
  assert(layer.width === EXPANDED_WIDTH && layer.height === EXPANDED_HEIGHT, `${layer.name}: expanded dimensions are invalid`);
  assert(layer.data.length === EXPANDED_WIDTH * EXPANDED_HEIGHT, `${layer.name}: expanded tile count is invalid`);
}

// ── GID resolution by tileset NAME + LOCAL id (art-spec GID 规则) ─────────────
// Never bake a global GID as a constant: resolve firstgid at paint time so a
// tileset reorder can't silently point at the wrong tile.
export function makeGidResolver(tilemap) {
  const byName = new Map(tilemap.tilesets.map((t) => [t.name, t]));
  return function resolveGid(tilesetName, localId) {
    const ts = byName.get(tilesetName);
    assert(ts, `tileset '${tilesetName}' not found`);
    assert(
      localId >= 0 && (ts.tilecount === undefined || localId < ts.tilecount),
      `local id ${localId} out of range for '${tilesetName}' (tilecount ${ts.tilecount})`,
    );
    return ts.firstgid + localId;
  };
}

function layerByName(tilemap, name) {
  const l = tilemap.layers.find((x) => x.name === name && Array.isArray(x.data));
  assert(l, `missing tile layer '${name}'`);
  return l;
}

// ── Experiment Building painter (idempotent; art-spec §实验楼地图) ────────────
// Only touches: Exterior Decoration L1/L2 (clear forest), Interior Ground
// (floor), Wall (perimeter outline), Collisions (authoritative wall ring with
// the 3-tile north entrance gap), Object Interaction Blocks (author metadata).
// NEVER touches World/Arena/Sector/Spawning/Special block layers (hard-#4).
export function paintExperimentBuilding(tilemap) {
  const resolve = makeGidResolver(tilemap);
  const { x0, y0, w, h, accessPath, hotspots } = LAB;
  const idx = (x, y) => y * EXPANDED_WIDTH + x;

  // Grounded tile choices reused from the workshop shell (proven, same
  // tilesets art-spec prescribes: Room_Builder walls, CuteRPG floor).
  const FLOOR = resolve("CuteRPG_Field_C", 233); // workshop interior floor (GID 490)
  const WALL = resolve("Room_Builder_32x32", 4842); // workshop wall tile (GID 5611)
  const BLOCK = resolve("blocks", 0); // Collisions atlas local 0 (GID 32125)
  const MARK = resolve("blocks", 0); // OIB author marker (no runtime consumer)

  const decoL1 = layerByName(tilemap, "Exterior Decoration L1").data;
  const decoL2 = layerByName(tilemap, "Exterior Decoration L2").data;
  const interior = layerByName(tilemap, "Interior Ground").data;
  const wall = layerByName(tilemap, "Wall").data;
  const collisions = layerByName(tilemap, "Collisions").data;
  const oib = layerByName(tilemap, "Object Interaction Blocks").data;

  // 1. Clear forest inside the footprint AND the north access path so nothing
  //    overlaps the building / blocks the approach (art-spec: 清除占地内树木,
  //    从密林清出 3 格通道).
  for (let y = y0; y < y0 + h; y += 1)
    for (let x = x0; x < x0 + w; x += 1) { decoL1[idx(x, y)] = 0; decoL2[idx(x, y)] = 0; }
  for (let y = accessPath.y1; y <= accessPath.y2; y += 1)
    for (let x = accessPath.x1; x <= accessPath.x2; x += 1) { decoL1[idx(x, y)] = 0; decoL2[idx(x, y)] = 0; }

  // 2. Paint the shell + Collisions per blockout. Reset the whole footprint on
  //    the touched layers first so re-runs are byte-identical (idempotent).
  for (let v = 0; v < h; v += 1) {
    for (let u = 0; u < w; u += 1) {
      const x = x0 + u, y = y0 + v, i = idx(x, y);
      const cell = LAB_BLOCKOUT[v][u];
      if (cell === "#") {
        wall[i] = WALL;
        interior[i] = 0;
        collisions[i] = BLOCK; // wall / maintenance strip blocks
      } else {
        // E entrance, C corridor, and all furniture zones are walkable floor.
        // Furniture-body collisions are added with the visible furniture art in
        // the A2 visual pass — adding them now would be an invisible wall on a
        // bright floor (art-spec forbids that).
        wall[i] = 0;
        interior[i] = FLOOR;
        collisions[i] = 0;
      }
    }
  }

  // 3. Object Interaction Blocks — author metadata markers at the 5 hotspots.
  for (const [u, v] of Object.values(hotspots)) oib[idx(x0 + u, y0 + v)] = MARK;

  return tilemap;
}

// ── Pure build core (no disk I/O) ────────────────────────────────────────────
export function buildExpandedTilemap(tilemap, maze) {
  const inputWidth = tilemap.width;
  const inputHeight = tilemap.height;
  const shouldRebuild = inputWidth === ORIGINAL_WIDTH;
  assert(
    (inputWidth === ORIGINAL_WIDTH && inputHeight === ORIGINAL_HEIGHT) ||
      (inputWidth === EXPANDED_WIDTH && inputHeight === EXPANDED_HEIGHT),
    `Expected a ${ORIGINAL_WIDTH}x${ORIGINAL_HEIGHT} or ${EXPANDED_WIDTH}x${EXPANDED_HEIGHT} map, found ${inputWidth}x${inputHeight}`,
  );

  const tileLayers = tilemap.layers.filter((l) => l.type === "tilelayer" && Array.isArray(l.data));
  assert(tileLayers.length > 0, "The tilemap has no tile layers");
  for (const req of ["Bottom Ground", "Exterior Ground", "Collisions"])
    assert(tileLayers.some((l) => l.name === req), `The tilemap is missing ${req}`);

  for (const layer of tileLayers) {
    if (shouldRebuild) expandLayer(layer, extractOriginalLayer(layer, inputWidth, inputHeight));
    verifyLayer(layer);
  }
  tilemap.width = EXPANDED_WIDTH;
  tilemap.height = EXPANDED_HEIGHT;

  paintExperimentBuilding(tilemap);

  if (maze) maze.size = [EXPANDED_HEIGHT, EXPANDED_WIDTH];
  return { tilemap, maze, rebuilt: shouldRebuild, layerCount: tileLayers.length };
}

// ── CLI write wrapper ─────────────────────────────────────────────────────────
async function main() {
  const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
  const villageDirectory = path.resolve(scriptDirectory, "../public/assets/village");
  const tilemapPath = path.join(villageDirectory, "tilemap/tilemap.json");
  const mazePath = path.join(villageDirectory, "maze.json");
  const backendTilemapPath = path.resolve(
    scriptDirectory, "../../backend/app/assets/village/tilemap/tilemap.json",
  );

  const args = process.argv.slice(2);
  assert(
    args.length === 0 || (args.length === 2 && args[0] === "--source"),
    "Usage: node scripts/expand-town-map.mjs [--source path/to/tilemap.json]",
  );
  const sourcePath = args.length === 0 ? tilemapPath : path.resolve(process.cwd(), args[1]);

  const tilemap = JSON.parse(await readFile(sourcePath, "utf8"));
  const maze = JSON.parse(await readFile(mazePath, "utf8"));
  const { rebuilt, layerCount } = buildExpandedTilemap(tilemap, maze);

  const tilemapJson = `${JSON.stringify(tilemap, null, 3)}\n`;
  await writeFile(tilemapPath, tilemapJson);
  await writeFile(mazePath, `${JSON.stringify(maze, null, 4)}\n`);
  await mkdir(path.dirname(backendTilemapPath), { recursive: true });
  await writeFile(backendTilemapPath, tilemapJson); // byte-identical backend copy

  console.log(
    `${rebuilt ? "Expanded" : "Validated"} town map at ${EXPANDED_WIDTH}x${EXPANDED_HEIGHT}: ${layerCount} layers, painted Experiment Building blockout (108,72)-(124,86).`,
  );
}

// Only run the CLI when invoked directly, not when imported by the verifier.
if (import.meta.url === `file://${process.argv[1]}`) {
  await main();
}
