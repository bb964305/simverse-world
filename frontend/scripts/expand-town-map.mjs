import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ORIGINAL_WIDTH = 140;
const ORIGINAL_HEIGHT = 100;
const EXPANDED_WIDTH = 180;
const EXPANDED_HEIGHT = 128;

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const villageDirectory = path.resolve(scriptDirectory, "../public/assets/village");
const tilemapPath = path.join(villageDirectory, "tilemap/tilemap.json");
const mazePath = path.join(villageDirectory, "maze.json");
// Backend bundles its own tilemap copy so pathfinding works in the container
// image (built from backend/ only). Kept byte-identical to the frontend source.
const backendTilemapPath = path.resolve(
  scriptDirectory,
  "../../backend/app/assets/village/tilemap/tilemap.json",
);

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
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
    for (let x = x1; x <= x2; x += 1) {
      data[y * EXPANDED_WIDTH + x] = value;
    }
  }
}

function pasteRectangle(
  target,
  source,
  sourceX,
  sourceY,
  targetX,
  targetY,
  width,
  height,
) {
  for (let offsetY = 0; offsetY < height; offsetY += 1) {
    for (let offsetX = 0; offsetX < width; offsetX += 1) {
      target[(targetY + offsetY) * EXPANDED_WIDTH + targetX + offsetX] =
        source[
          (sourceY + offsetY) * ORIGINAL_WIDTH + sourceX + offsetX
        ];
    }
  }
}

function pasteMirroredRectangle(
  target,
  source,
  sourceX,
  sourceY,
  targetX,
  targetY,
  width,
  height,
) {
  for (let offsetY = 0; offsetY < height; offsetY += 1) {
    for (let offsetX = 0; offsetX < width; offsetX += 1) {
      const tile =
        source[
          (sourceY + offsetY) * ORIGINAL_WIDTH +
            sourceX +
            width -
            1 -
            offsetX
        ];
      target[(targetY + offsetY) * EXPANDED_WIDTH + targetX + offsetX] =
        tile === 0 ? 0 : (tile ^ 0x80000000) >>> 0;
    }
  }
}

function carveGreenConnection(data, emptyTile, rows) {
  for (const [y, x1, x2] of rows) {
    fillRectangle(data, x1, y, x2, y, emptyTile);
  }
}

function paintParkPath(layer, data) {
  const emptyTile = layer.name === "Bottom Ground" ? 2 : 0;
  fillRectangle(data, 157, 50, 159, 58, emptyTile);

  if (layer.name !== "Exterior Ground") {
    return;
  }

  const rows = [
    [50, 81, 82, 83],
    [51, 97, 98, 99],
    [52, 97, 98, 99],
    [53, 97, 98, 99],
    [54, 97, 98, 99],
    [55, 97, 98, 99],
    [56, 97, 98, 99],
    [57, 97, 98, 99],
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
  const expanded = new Array(EXPANDED_WIDTH * EXPANDED_HEIGHT).fill(
    emptyTile,
  );

  // Preserve the original town, then compose complete landscape chunks outside it.
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

  // Restore a continuous natural boundary along the expanded map's south edge.
  pasteRectangle(expanded, original, 0, 96, 0, 124, 140, 4);
  pasteMirroredRectangle(expanded, original, 68, 96, 140, 124, 35, 4);
  pasteRectangle(expanded, original, 135, 96, 175, 124, 5, 4);

  pasteRectangle(expanded, original, 119, 35, 130, 35, 17, 8);
  carveGreenConnection(expanded, emptyTile, [
    [54, 133, 144],
    [55, 132, 145],
    [56, 132, 145],
    [57, 133, 144],
  ]);
  carveGreenConnection(expanded, emptyTile, [
    [92, 71, 75],
    [93, 70, 76],
    [94, 70, 76],
    [95, 70, 76],
    [96, 70, 76],
    [97, 70, 76],
    [98, 70, 76],
    [99, 70, 76],
    [100, 70, 76],
    [101, 70, 76],
    [102, 70, 76],
    [103, 70, 76],
    [104, 71, 75],
  ]);
  paintParkPath(layer, expanded);

  layer.width = EXPANDED_WIDTH;
  layer.height = EXPANDED_HEIGHT;
  layer.data = expanded;
}

function verifyLayer(layer) {
  assert(
    layer.width === EXPANDED_WIDTH && layer.height === EXPANDED_HEIGHT,
    `${layer.name}: expanded dimensions are invalid`,
  );
  assert(
    layer.data.length === EXPANDED_WIDTH * EXPANDED_HEIGHT,
    `${layer.name}: expanded tile count is invalid`,
  );
}

const arguments_ = process.argv.slice(2);
assert(
  arguments_.length === 0 ||
    (arguments_.length === 2 && arguments_[0] === "--source"),
  "Usage: node scripts/expand-town-map.mjs [--source path/to/tilemap.json]",
);
const sourcePath =
  arguments_.length === 0
    ? tilemapPath
    : path.resolve(process.cwd(), arguments_[1]);

const tilemap = JSON.parse(await readFile(sourcePath, "utf8"));
const maze = JSON.parse(await readFile(mazePath, "utf8"));
const inputWidth = tilemap.width;
const inputHeight = tilemap.height;
const shouldRebuild = inputWidth === ORIGINAL_WIDTH;

assert(
  (inputWidth === ORIGINAL_WIDTH && inputHeight === ORIGINAL_HEIGHT) ||
    (inputWidth === EXPANDED_WIDTH && inputHeight === EXPANDED_HEIGHT),
  `Expected a ${ORIGINAL_WIDTH}x${ORIGINAL_HEIGHT} or ${EXPANDED_WIDTH}x${EXPANDED_HEIGHT} map, found ${inputWidth}x${inputHeight}`,
);

const tileLayers = tilemap.layers.filter(
  (layer) => layer.type === "tilelayer" && Array.isArray(layer.data),
);
assert(tileLayers.length > 0, "The tilemap has no tile layers");
assert(
  tileLayers.some((layer) => layer.name === "Bottom Ground"),
  "The tilemap is missing Bottom Ground",
);
assert(
  tileLayers.some((layer) => layer.name === "Exterior Ground"),
  "The tilemap is missing Exterior Ground",
);
assert(
  tileLayers.some((layer) => layer.name === "Collisions"),
  "The tilemap is missing Collisions",
);

for (const layer of tileLayers) {
  if (shouldRebuild) {
    const original = extractOriginalLayer(layer, inputWidth, inputHeight);
    expandLayer(layer, original);
  }
  verifyLayer(layer);
}

tilemap.width = EXPANDED_WIDTH;
tilemap.height = EXPANDED_HEIGHT;
maze.size = [EXPANDED_HEIGHT, EXPANDED_WIDTH];

const tilemapJson = `${JSON.stringify(tilemap, null, 3)}\n`;
await writeFile(tilemapPath, tilemapJson);
await writeFile(mazePath, `${JSON.stringify(maze, null, 4)}\n`);
// Keep the backend-bundled copy byte-identical (same serialization).
await mkdir(path.dirname(backendTilemapPath), { recursive: true });
await writeFile(backendTilemapPath, tilemapJson);

console.log(
  `${shouldRebuild ? "Expanded" : "Validated"} town map at ${EXPANDED_WIDTH}x${EXPANDED_HEIGHT}: ${tileLayers.length} layers, ${EXPANDED_WIDTH * EXPANDED_HEIGHT} tiles per layer.`,
);
