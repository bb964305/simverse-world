"""双通道 token 估计器（研究口径，见 COST_RESEARCH_LOG.md §0.2）。

绝对值 ±25%；比较类结论请用比值口径。
用法: python3 est_tokens.py <file>...  或 import est_tokens
"""
import sys
import unicodedata


def est_tokens(text: str) -> float:
    ascii_chars = cjk = other = 0
    for ch in text:
        cp = ord(ch)
        if cp < 128:
            ascii_chars += 1
        elif (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x3000 <= cp <= 0x303F
            or 0xFF00 <= cp <= 0xFFEF
            or unicodedata.category(ch).startswith("Lo")
        ):
            cjk += 1
        else:
            other += 1
    return ascii_chars / 3.6 + cjk * 1.0 + other / 2.0


def breakdown(text: str) -> dict:
    return {
        "chars": len(text),
        "est_tokens": round(est_tokens(text)),
    }


if __name__ == "__main__":
    for path in sys.argv[1:]:
        with open(path) as f:
            t = f.read()
        print(path, breakdown(t))
