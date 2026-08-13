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

// ── Post office authoritative geometry (art-spec §邮局地图) ────────────────
// Inclusive footprint (44,100)-(48,106) = 5x7. The center column remains the
// public route from the north entrance to the sorting-room work station.
export const POST_OFFICE = {
  x0: 44, y0: 100, w: 5, h: 7,
  entrance: [46, 100],
  center: [46, 103],
};

// ── Market hall authoritative geometry ─────────────────────────────────────
// A permanent 15x11 trading hall sits directly east of the market avenue.
// Its five-tile-wide west doorway lets the 64px caravan turn off the avenue
// and unfold inside without clipping the facade or the fixed produce stalls.
export const MARKET_HALL = {
  x0: 105, y0: 89, w: 15, h: 11,
  entrance: [105, 94],
  center: [112, 94],
  caravanParking: [109, 94],
  doorway: { y1: 92, y2: 96 },
  accessPath: { x1: 102, y1: 92, x2: 105, y2: 96 },
};

// # = masonry/wood shell, E = five-wide west loading door, . = open trading
// aisle, S = permanent merchant bay.  The middle five rows stay empty so the
// wagon has a full two-tile visual envelope all the way to its parking anchor.
export const MARKET_HALL_BLOCKOUT = [
  "###############",
  "#SSS.......SSS#",
  "#SSS.......SSS#",
  "E.............#",
  "E.............#",
  "E.............#",
  "E.............#",
  "E.............#",
  "#SSS.......SSS#",
  "#SSS.......SSS#",
  "###############",
];

// ── South gate + market avenue authoritative geometry ──────────────────────
// The old logical `town_entrance` covered (50,85)-(90,99), which is a wooded
// residential belt rather than an entrance.  A caravan could therefore be
// collision-valid while visibly driving through trees.  The avenue below is a
// deliberately authored, five-tile-wide civic spine: it passes Central Plaza,
// serves the standalone market hall, separates housing from the civic/forest belt,
// and continues through a visible gate to the south edge of the map.
export const MARKET_AVENUE = {
  x1: 100, x2: 104,
  y1: 58, y2: EXPANDED_HEIGHT - 1,
  shoulderX1: 98, shoulderX2: 106,
  plazaExtension: { x1: 96, x2: 104, y1: 56, y2: 60 },
  plazaLaneClearX: 75,
  entranceBounds: [100, 119, 104, 122],
  entranceCenter: [102, 121],
  gate: { x1: 99, x2: 105, topY: 121, postY: 122 },
};

// Building bounds mirrored from map_data.  Forest belongs in green belts, not
// on top of facades or immediately against a doorway, so the deterministic art
// pass maintains a two-tile tree-free setback around every planned structure.
export const PLANNED_BUILDING_FOOTPRINTS = [
  [15, 18, 42, 34], [72, 13, 83, 26], [53, 14, 62, 26], [108, 20, 124, 34],
  [57, 43, 70, 53], [75, 43, 93, 53], [106, 45, 132, 62], [108, 72, 124, 86],
  [65, 14, 69, 26], [86, 13, 90, 25], [93, 13, 97, 25],
  [20, 59, 24, 70], [27, 59, 33, 70], [36, 59, 40, 70],
  [51, 65, 62, 75], [69, 65, 80, 75], [87, 65, 99, 75],
  [20, 104, 24, 115], [27, 104, 33, 115], [36, 104, 40, 115],
  [51, 110, 62, 120], [69, 110, 80, 120], [87, 110, 99, 120],
  [141, 65, 152, 75], [159, 65, 170, 75], [143, 110, 155, 120], [162, 110, 173, 120],
  [44, 100, 48, 106], [105, 89, 119, 99],
];
export const BUILDING_FOREST_SETBACK = 2;

const POST_OFFICE_LAYOUT = [
  "##E##",
  "#...#",
  "#...#",
  "#...#",
  "#...#",
  "#...#",
  "#####",
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

// Post office visual pass. All tile choices are existing registered assets;
// resolveGid keeps the painter independent of tileset ordering.
export function paintPostOffice(tilemap) {
  const resolve = makeGidResolver(tilemap);
  const { x0, y0, w, h } = POST_OFFICE;
  const idx = (x, y) => y * EXPANDED_WIDTH + x;
  const FLOOR = resolve("Room_Builder_32x32", 4658);
  const WALL = resolve("Room_Builder_32x32", 4164);
  const BLOCK = resolve("blocks", 0);
  const FURNITURE = {
    // The mailbox is deliberately a saturated red single-tile object so it
    // reads at the town-map zoom; the envelope sign completes the facade.
    mailbox: resolve("interiors_pt3", 3973),
    envelopePlain: resolve("interiors_pt4", 1915),
    envelopeStamped: resolve("interiors_pt4", 1916),
    // These are the left/right pieces of the same 2-tile service counter.
    counterLeft: resolve("interiors_pt2", 3056),
    counterLeftRight: resolve("interiors_pt2", 3057),
    counterRight: resolve("interiors_pt2", 3059),
    counterRightSide: resolve("interiors_pt2", 3060),
    // Two complete pigeonhole runs: warm wood on the west wall and postal red
    // on the east wall. They are built-in against the wall crown.
    pigeonholesGoldLeft: resolve("interiors_pt4", 1947),
    pigeonholesGoldRight: resolve("interiors_pt4", 1948),
    pigeonholesRedLeft: resolve("interiors_pt4", 1931),
    pigeonholesRedRight: resolve("interiors_pt4", 1932),
    parcelShelfLeft: resolve("interiors_pt4", 1950),
    parcelShelfRight: resolve("interiors_pt4", 1951),
    parcelSmall: resolve("interiors_pt4", 1923),
    parcelSmallAlt: resolve("interiors_pt4", 1924),
    // Existing 2x2 table construction used by the neighbouring apartment.
    tableTopLeft: resolve("interiors_pt1", 2579),
    tableTopRight: resolve("interiors_pt1", 2580),
    tableBottomLeft: resolve("interiors_pt1", 2595),
    tableBottomRight: resolve("interiors_pt1", 2596),
    restChair: resolve("interiors_pt1", 2610),
    benchLeft: resolve("interiors_pt4", 1934),
    benchRight: resolve("interiors_pt4", 1935),
  };
  const decoL1 = layerByName(tilemap, "Exterior Decoration L1").data;
  const decoL2 = layerByName(tilemap, "Exterior Decoration L2").data;
  const interior = layerByName(tilemap, "Interior Ground").data;
  const wall = layerByName(tilemap, "Wall").data;
  const furnitureL1 = layerByName(tilemap, "Interior Furniture L1").data;
  const furnitureL2 = layerByName(tilemap, "Interior Furniture L2 ").data;
  const collisions = layerByName(tilemap, "Collisions").data;

  // Remove vegetation only from the construction rectangle. The single
  // mailbox tile just north-west of the door is intentionally outside the
  // rectangle and is already a clear grass tile in the survey.
  for (let y = y0; y < y0 + h; y += 1)
    for (let x = x0; x < x0 + w; x += 1) { decoL1[idx(x, y)] = 0; decoL2[idx(x, y)] = 0; }

  // Reset the shell first; furniture is placed in a second pass so composite
  // atlas objects can span the wall edge without making the route ambiguous.
  for (let v = 0; v < h; v += 1) {
    for (let u = 0; u < w; u += 1) {
      const x = x0 + u, y = y0 + v, i = idx(x, y);
      furnitureL1[i] = 0;
      furnitureL2[i] = 0;
      if (POST_OFFICE_LAYOUT[v][u] === "#") {
        wall[i] = WALL; interior[i] = 0; collisions[i] = BLOCK;
      } else {
        wall[i] = 0; interior[i] = FLOOR; collisions[i] = 0;
      }
    }
  }

  const setFurniture = (x, y, gid, { layer = furnitureL1, blocks = true } = {}) => {
    layer[idx(x, y)] = gid;
    if (blocks) collisions[idx(x, y)] = BLOCK;
  };
  const setPair = (x, y, left, right, options) => {
    setFurniture(x, y, left, options);
    setFurniture(x + 1, y, right, options);
  };

  // Facade identity: keep the red mailbox unobscured beside the doorway; the
  // stamped-envelope sign is mounted above the service counter below.
  decoL1[idx(45, 99)] = FURNITURE.mailbox;
  decoL2[idx(45, 99)] = 0;
  collisions[idx(45, 99)] = BLOCK;

  // Service hall. The counter and pigeonhole pairs deliberately sit against
  // the side walls, leaving x=46 as the public approach through y=103.
  setPair(44, 101, FURNITURE.counterLeft, FURNITURE.counterLeftRight);
  setPair(47, 101, FURNITURE.counterRight, FURNITURE.counterRightSide);
  setPair(44, 102, FURNITURE.pigeonholesGoldLeft, FURNITURE.pigeonholesGoldRight);
  setPair(47, 102, FURNITURE.pigeonholesRedLeft, FURNITURE.pigeonholesRedRight);
  setFurniture(45, 102, FURNITURE.envelopePlain, { layer: furnitureL2, blocks: false });
  setFurniture(47, 102, FURNITURE.envelopeStamped, { layer: furnitureL2, blocks: false });

  // Parcel rack and loose parcels on the east wall.
  setPair(47, 103, FURNITURE.parcelShelfLeft, FURNITURE.parcelShelfRight);
  setFurniture(47, 103, FURNITURE.parcelSmall, { layer: furnitureL2, blocks: false });

  // A complete 2x2 sorting table occupies the back-left bay. Its top-right
  // tile is below the route's center marker, so the player can reach the work
  // station without stepping through the furniture.
  setFurniture(45, 104, FURNITURE.tableTopLeft);
  setFurniture(46, 104, FURNITURE.tableTopRight);
  setFurniture(45, 105, FURNITURE.tableBottomLeft);
  setFurniture(46, 105, FURNITURE.tableBottomRight);
  setFurniture(45, 104, FURNITURE.parcelSmallAlt, { layer: furnitureL2, blocks: false });

  // Rest corner: a red chair beside a compact built-in bench.
  setFurniture(47, 104, FURNITURE.restChair);
  setPair(47, 105, FURNITURE.benchLeft, FURNITURE.benchRight);

  // Entrance and central aisle are always walkable, even if a future layout
  // symbol accidentally places a decorative tile there. The sorting table is
  // intentionally behind the center marker, so the route ends at (46,103).
  for (const [x, y] of [[46, 100], [46, 101], [46, 102], [46, 103]]) {
    collisions[idx(x, y)] = 0;
    furnitureL1[idx(x, y)] = 0;
    furnitureL2[idx(x, y)] = 0;
    if (y !== 100) interior[idx(x, y)] = FLOOR;
  }
  return tilemap;
}

// Permanent market-hall visual pass.  Unlike the market-day caravan sprite,
// this shell and its fixed merchant bays are present every day, making the
// market a legible civic destination even while the hall is closed.
export function paintMarketHall(tilemap) {
  const resolve = makeGidResolver(tilemap);
  const { x0, y0, w, h, doorway, accessPath } = MARKET_HALL;
  const idx = (x, y) => y * EXPANDED_WIDTH + x;
  const FLOOR = resolve("Room_Builder_32x32", 4658);
  const WALL = resolve("Room_Builder_32x32", 4164);
  const BLOCK = resolve("blocks", 0);
  const MARKET_FIXTURES = {
    noticeLeft: resolve("CuteRPG_Village_B", 40),
    noticeRight: resolve("CuteRPG_Village_B", 41),
    counterLeft: resolve("CuteRPG_Village_B", 56),
    counterRight: resolve("CuteRPG_Village_B", 57),
    produceGold: resolve("CuteRPG_Village_B", 42),
    produceGreen: resolve("CuteRPG_Village_B", 43),
    produceRed: resolve("CuteRPG_Village_B", 44),
    produceBrown: resolve("CuteRPG_Village_B", 45),
    basketGold: resolve("CuteRPG_Village_B", 58),
    basketGreen: resolve("CuteRPG_Village_B", 59),
    basketRed: resolve("CuteRPG_Village_B", 60),
    basketBrown: resolve("CuteRPG_Village_B", 61),
  };

  const exterior = layerByName(tilemap, "Exterior Ground").data;
  const decoL1 = layerByName(tilemap, "Exterior Decoration L1").data;
  const decoL2 = layerByName(tilemap, "Exterior Decoration L2").data;
  const interior = layerByName(tilemap, "Interior Ground").data;
  const wall = layerByName(tilemap, "Wall").data;
  const furnitureL1 = layerByName(tilemap, "Interior Furniture L1").data;
  const furnitureL2 = layerByName(tilemap, "Interior Furniture L2 ").data;
  const foregroundL1 = layerByName(tilemap, "Foreground L1").data;
  const foregroundL2 = layerByName(tilemap, "Foreground L2").data;
  const collisions = layerByName(tilemap, "Collisions").data;

  // Clear the surveyed construction plot and five-wide loading throat.  Every
  // touched visual/collision layer is reset so repeat generation is identical.
  for (let y = y0; y < y0 + h; y += 1) {
    for (let x = x0; x < x0 + w; x += 1) {
      const i = idx(x, y);
      exterior[i] = 0;
      decoL1[i] = 0;
      decoL2[i] = 0;
      foregroundL1[i] = 0;
      foregroundL2[i] = 0;
      furnitureL1[i] = 0;
      furnitureL2[i] = 0;
    }
  }
  for (let y = accessPath.y1; y <= accessPath.y2; y += 1) {
    for (let x = accessPath.x1; x <= accessPath.x2; x += 1) {
      const i = idx(x, y);
      decoL1[i] = 0;
      decoL2[i] = 0;
      foregroundL1[i] = 0;
      foregroundL2[i] = 0;
      collisions[i] = 0;
    }
  }

  for (let v = 0; v < h; v += 1) {
    for (let u = 0; u < w; u += 1) {
      const x = x0 + u, y = y0 + v, i = idx(x, y);
      const cell = MARKET_HALL_BLOCKOUT[v][u];
      if (cell === "#") {
        interior[i] = 0;
        wall[i] = WALL;
        collisions[i] = BLOCK;
      } else {
        interior[i] = FLOOR;
        wall[i] = 0;
        collisions[i] = 0;
      }
    }
  }

  const setFixture = (x, y, gid) => {
    furnitureL1[idx(x, y)] = gid;
    collisions[idx(x, y)] = BLOCK;
  };
  const setPair = (x, y, left, right) => {
    setFixture(x, y, left);
    setFixture(x + 1, y, right);
  };

  // Four permanent bays frame the central loading aisle.  Warm notice/counter
  // pairs and distinct produce baskets give each corner a recognizable stall
  // while keeping every fixture at least three tiles from the wagon anchor.
  setPair(106, 91, MARKET_FIXTURES.noticeLeft, MARKET_FIXTURES.noticeRight);
  setFixture(108, 91, MARKET_FIXTURES.produceGold);
  setFixture(114, 91, MARKET_FIXTURES.produceGreen);
  setPair(115, 91, MARKET_FIXTURES.noticeLeft, MARKET_FIXTURES.noticeRight);
  setPair(106, 97, MARKET_FIXTURES.counterLeft, MARKET_FIXTURES.counterRight);
  setFixture(108, 97, MARKET_FIXTURES.produceRed);
  setFixture(114, 97, MARKET_FIXTURES.produceBrown);
  setPair(115, 97, MARKET_FIXTURES.counterLeft, MARKET_FIXTURES.counterRight);
  setFixture(107, 90, MARKET_FIXTURES.basketGold);
  setFixture(116, 90, MARKET_FIXTURES.basketGreen);
  setFixture(107, 98, MARKET_FIXTURES.basketRed);
  setFixture(116, 98, MARKET_FIXTURES.basketBrown);

  // Reassert the complete loading envelope after fixture placement.  This is
  // the server-authoritative centerline plus two visual-overhang rows.
  for (let y = doorway.y1; y <= doorway.y2; y += 1) {
    for (let x = x0; x <= MARKET_HALL.center[0]; x += 1) {
      const i = idx(x, y);
      collisions[i] = 0;
      furnitureL1[i] = 0;
      furnitureL2[i] = 0;
      wall[i] = 0;
      interior[i] = FLOOR;
    }
  }

  return tilemap;
}

// Paint the canonical caravan route as actual map art.  This is intentionally
// part of the deterministic generator (rather than a runtime overlay) so the
// frontend image, backend collision authority, minimap, and A* route all share
// one piece of geometry.
export function paintMarketAvenue(tilemap) {
  const resolve = makeGidResolver(tilemap);
  const idx = (x, y) => y * EXPANDED_WIDTH + x;
  const exterior = layerByName(tilemap, "Exterior Ground").data;
  const decoL1 = layerByName(tilemap, "Exterior Decoration L1").data;
  const decoL2 = layerByName(tilemap, "Exterior Decoration L2").data;
  const interior = layerByName(tilemap, "Interior Ground").data;
  const wall = layerByName(tilemap, "Wall").data;
  const furnitureL1 = layerByName(tilemap, "Interior Furniture L1").data;
  const furnitureL2 = layerByName(tilemap, "Interior Furniture L2 ").data;
  const foregroundL1 = layerByName(tilemap, "Foreground L1").data;
  const foregroundL2 = layerByName(tilemap, "Foreground L2").data;
  const collisions = layerByName(tilemap, "Collisions").data;

  // CuteRPG_Field_B's established cream-road autotiles.  These are the same
  // left/middle/right tiles already used by the original town streets.
  const ROAD_LEFT = resolve("CuteRPG_Field_B", 96);
  const ROAD_MIDDLE = resolve("CuteRPG_Field_B", 97);
  const ROAD_RIGHT = resolve("CuteRPG_Field_B", 98);
  const ROAD_TOP = resolve("CuteRPG_Field_B", 81);
  const ROAD_TOP_RIGHT = resolve("CuteRPG_Field_B", 82);
  const ROAD_BOTTOM = resolve("CuteRPG_Field_B", 113);

  // Extend Central Plaza eastward before laying the north/south avenue.  The
  // existing plaza ends at x=95; this short boulevard creates a legible
  // T-junction and a one-turn route to the market bay at (75,58).
  const extension = MARKET_AVENUE.plazaExtension;
  for (let y = extension.y1; y <= extension.y2; y += 1) {
    for (let x = extension.x1; x <= extension.x2; x += 1) {
      const i = idx(x, y);
      assert(interior[i] === 0 && wall[i] === 0 && furnitureL1[i] === 0 && furnitureL2[i] === 0,
        `market boulevard overlaps a structure at (${x},${y})`);
      exterior[i] = y === extension.y1
        ? (x === extension.x2 ? ROAD_TOP_RIGHT : ROAD_TOP)
        : y === extension.y2 ? ROAD_BOTTOM
          : (x === extension.x2 ? ROAD_RIGHT : ROAD_MIDDLE);
      decoL1[i] = 0;
      decoL2[i] = 0;
      foregroundL1[i] = 0;
      foregroundL2[i] = 0;
      collisions[i] = 0;
    }
  }

  // Keep the pre-existing east-west plaza lane visually clean. It is no longer
  // the caravan destination, but remains the avenue's northern public apron.
  for (let y = 56; y <= 58; y += 1) {
    for (let x = MARKET_AVENUE.plazaLaneClearX; x <= extension.x2; x += 1) {
      decoL1[idx(x, y)] = 0;
      decoL2[idx(x, y)] = 0;
      foregroundL1[idx(x, y)] = 0;
      foregroundL2[idx(x, y)] = 0;
    }
  }

  // Clear a two-tile landscaped shoulder on either side.  Only the road body
  // clears structural layers/collisions; shoulders remain grass, which keeps
  // adjacent homes intact while separating them visually from the forest.
  for (let y = MARKET_AVENUE.y1; y <= MARKET_AVENUE.y2; y += 1) {
    for (let x = MARKET_AVENUE.shoulderX1; x <= MARKET_AVENUE.shoulderX2; x += 1) {
      decoL1[idx(x, y)] = 0;
      decoL2[idx(x, y)] = 0;
    }
    for (let x = MARKET_AVENUE.x1; x <= MARKET_AVENUE.x2; x += 1) {
      const i = idx(x, y);
      assert(interior[i] === 0 && wall[i] === 0 && furnitureL1[i] === 0 && furnitureL2[i] === 0,
        `market avenue overlaps a structure at (${x},${y})`);
      exterior[i] = x === MARKET_AVENUE.x1 ? ROAD_LEFT
        : x === MARKET_AVENUE.x2 ? ROAD_RIGHT : ROAD_MIDDLE;
      foregroundL1[i] = 0;
      foregroundL2[i] = 0;
      collisions[i] = 0;
    }
  }

  // Shoulder-mounted gate posts make the entrance unmistakable without dropping
  // foreground pixels directly onto the caravan lane.
  const { x1, x2, topY, postY } = MARKET_AVENUE.gate;
  foregroundL1[idx(x1, topY)] = resolve("CuteRPG_Village_B", 37);
  foregroundL1[idx(x2, topY)] = resolve("CuteRPG_Village_B", 39);
  foregroundL1[idx(x1, postY)] = resolve("CuteRPG_Village_B", 53);
  foregroundL1[idx(x2, postY)] = resolve("CuteRPG_Village_B", 55);
  collisions[idx(x1, postY)] = resolve("blocks", 0);
  collisions[idx(x2, postY)] = resolve("blocks", 0);

  return tilemap;
}

export function paintBuildingForestSetbacks(tilemap) {
  const idx = (x, y) => y * EXPANDED_WIDTH + x;
  const decoL1 = layerByName(tilemap, "Exterior Decoration L1").data;
  const decoL2 = layerByName(tilemap, "Exterior Decoration L2").data;
  const interior = layerByName(tilemap, "Interior Ground").data;
  const wall = layerByName(tilemap, "Wall").data;
  const furnitureL1 = layerByName(tilemap, "Interior Furniture L1").data;
  const furnitureL2 = layerByName(tilemap, "Interior Furniture L2 ").data;
  const collisions = layerByName(tilemap, "Collisions").data;
  const forestRanges = tilemap.tilesets
    .filter((tileset) => tileset.name === "CuteRPG_Forest_B" || tileset.name === "CuteRPG_Forest_C")
    .map((tileset) => [tileset.firstgid, tileset.firstgid + tileset.tilecount - 1]);
  // Field_B contains the small/large standalone tree pieces used around the
  // original homes; include just that atlas' tree band, not flowers or props.
  const field = tilemap.tilesets.find((tileset) => tileset.name === "CuteRPG_Field_B");
  if (field) forestRanges.push([field.firstgid + 133, field.firstgid + 154]);
  const isForest = (rawGid) => {
    const gid = rawGid & 0x1fffffff;
    return gid !== 0 && forestRanges.some(([first, last]) => first <= gid && gid <= last);
  };

  for (const [x1, y1, x2, y2] of PLANNED_BUILDING_FOOTPRINTS) {
    const sx1 = Math.max(0, x1 - BUILDING_FOREST_SETBACK);
    const sy1 = Math.max(0, y1 - BUILDING_FOREST_SETBACK);
    const sx2 = Math.min(EXPANDED_WIDTH - 1, x2 + BUILDING_FOREST_SETBACK);
    const sy2 = Math.min(EXPANDED_HEIGHT - 1, y2 + BUILDING_FOREST_SETBACK);
    for (let y = sy1; y <= sy2; y += 1) {
      for (let x = sx1; x <= sx2; x += 1) {
        const i = idx(x, y);
        if (!isForest(decoL1[i]) && !isForest(decoL2[i])) continue;
        // Structural layers win: never erase a building collision merely
        // because a legacy tree tile was also pasted over the same cell.
        if (interior[i] || wall[i] || furnitureL1[i] || furnitureL2[i]) continue;
        decoL1[i] = 0;
        decoL2[i] = 0;
        collisions[i] = 0;
      }
    }
  }
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
  paintPostOffice(tilemap);
  paintMarketHall(tilemap);
  paintBuildingForestSetbacks(tilemap);
  paintMarketAvenue(tilemap);

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
    `${rebuilt ? "Expanded" : "Validated"} town map at ${EXPANDED_WIDTH}x${EXPANDED_HEIGHT}: ${layerCount} layers, painted Experiment Building, post office, market hall, and market avenue.`,
  );
}

// Only run the CLI when invoked directly, not when imported by the verifier.
if (import.meta.url === `file://${process.argv[1]}`) {
  await main();
}
