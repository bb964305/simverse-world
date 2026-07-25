"""E-01: 居民 system prompt 稳定前缀尺寸分布 vs Haiku 4096 缓存阈值。"""
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from est_tokens import est_tokens

DB = Path(__file__).parents[3] / "backend" / "skills_world_dev.db"


def stable_prefix(name, district, soul, persona, ability):
    parts = [f"你是 {name}，住在 Simverse World 的{district}街区。", ""]
    for header, body in [
        ("## 灵魂（你为什么这样做）", soul),
        ("## 人格（你怎么做、怎么说）", persona),
        ("## 能力（你能做什么）", ability),
    ]:
        if body:
            parts += [header, body, ""]
    return "\n".join(parts)


con = sqlite3.connect(DB)
rows = con.execute(
    "SELECT name, district, soul_md, persona_md, ability_md FROM residents"
).fetchall()

sizes = []
for name, district, soul, persona, ability in rows:
    t = est_tokens(stable_prefix(name, district or "", soul, persona, ability))
    sizes.append((round(t), name))

sizes.sort(reverse=True)
vals = [s for s, _ in sizes]
print(f"n={len(vals)}")
print(f"min={min(vals)} median={statistics.median(vals):.0f} max={max(vals)} mean={statistics.mean(vals):.0f}")
print(f">=4096 (haiku threshold): {sum(1 for v in vals if v >= 4096)}")
print(f">=1024 (sonnet4.5-era threshold): {sum(1 for v in vals if v >= 1024)}")
print(f">=2048 (fable/sonnet4.6 threshold): {sum(1 for v in vals if v >= 2048)}")
print("\nper-resident (est_tokens, name):")
for t, name in sizes:
    print(f"  {t:>6}  {name}")
