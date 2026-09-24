#!/usr/bin/env python3
"""Generate README assets: banner-dark/light (.svg + .png) and demo.gif.

Needs cairosvg and Pillow (dev only, not a Jevflow dependency):
    python -m venv /tmp/svgenv && /tmp/svgenv/bin/pip install cairosvg==2.7.1 pillow==10.4.0
    /tmp/svgenv/bin/python docs/assets/gen_assets.py
The GIF replays a real journal (default: docs/assets/demo_run.json).
"""
import io
import json
import os
import sys

import cairosvg
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

THEMES = {
    "dark": dict(
        bg0="#0b1020", bg1="#121a3a", bg2="#1b1140", grid="#ffffff", grid_op="0.045",
        glowA="#6d5dfc", glowA_op="0.35", glowB="#20d6a4", glowB_op="0.28",
        w0="#ffffff", w1="#c9c3ff", w2="#7cf5d0",
        tag="#e7e5ff", sub="#9aa3c7", pill_fill="#ffffff", pill_fop="0.07", pill_stroke="#ffffff",
        pill_sop="0.15", pill_text="#c9c3ff",
        node_bg="#0f1733", label="#c8d1f0", ok="#20d6a4", ok_ink="#7cf5d0", bad="#ff7a93",
        bad_fill="#ff5d7a", bad_ink="#ffb3c1", bad_chip="#ffc2cd", ok_chip="#9ff7da",
        act="#8a82ff", act_ink="#c9c3ff", pend="#ffffff", pend_op="0.35", edge="#8a82ff",
        edge_op="0.55", readout="#7f89b3", star="#e7e5ff"),
    "light": dict(
        bg0="#fbfbff", bg1="#f1f2ff", bg2="#eafaf4", grid="#1b1140", grid_op="0.05",
        glowA="#6d5dfc", glowA_op="0.18", glowB="#20d6a4", glowB_op="0.18",
        w0="#1b1140", w1="#4b3fd0", w2="#0f9f7a",
        tag="#1b1b3a", sub="#5a6280", pill_fill="#4b3fd0", pill_fop="0.06", pill_stroke="#4b3fd0",
        pill_sop="0.25", pill_text="#4b3fd0",
        node_bg="#ffffff", label="#3a4266", ok="#10a37f", ok_ink="#0c8a6a", bad="#e5485d",
        bad_fill="#e5485d", bad_ink="#d03a4f", bad_chip="#b42a3e", ok_chip="#0c7a5e",
        act="#6d5dfc", act_ink="#6d5dfc", pend="#1b1140", pend_op="0.35", edge="#6d5dfc",
        edge_op="0.45", readout="#6b7394", star="#4b3fd0"),
}


def banner(t):
    check = 'M-10 1 l6 6 l14 -15'
    def node_ok(x, y, label, chip=None):
        c = (f'<rect x="-40" y="-66" width="80" height="24" rx="7" fill="{t["ok"]}" fill-opacity="0.16" '
             f'stroke="{t["ok"]}" stroke-opacity="0.7"/><text y="-49" text-anchor="middle" font-size="11" '
             f'font-weight="700" fill="{t["ok_chip"]}">{chip}</text>') if chip else ""
        return (f'<g transform="translate({x} {y})"><circle r="26" fill="{t["node_bg"]}" stroke="{t["ok"]}" stroke-width="2.5"/>'
                f'<circle r="26" fill="{t["ok"]}" fill-opacity="0.16"/>'
                f'<path d="{check}" fill="none" stroke="{t["ok_ink"]}" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>'
                f'<text y="46" text-anchor="middle" font-size="13" fill="{t["label"]}">{label}</text>{c}</g>')

    edges = [  # (d, style)
        ("M790 205 C 830 205, 840 140, 890 140", "solid"),   # scaffold -> implement
        ("M790 205 C 830 205, 840 270, 890 270", "solid"),   # scaffold -> docs
        ("M890 140 H 1000", "solid"),                          # implement -> test
        ("M890 270 C 950 270, 950 140, 1000 140", "solid"),  # docs -> test
        ("M1000 140 C 1060 140, 1060 270, 1100 270", "fail"),# test -on_fail-> debug
        ("M1000 140 C 1110 140, 1150 205, 1200 205", "todo"),# test -> release
        ("M1100 270 C 1150 270, 1160 205, 1200 205", "todo"),# debug -> release
    ]
    e_svg = ""
    for d, style in edges:
        if style == "solid":
            e_svg += f'<path d="{d}" fill="none" stroke="url(#flow)" stroke-width="3" opacity="0.85"/>'
        elif style == "fail":
            e_svg += f'<path d="{d}" fill="none" stroke="{t["bad"]}" stroke-width="2" stroke-dasharray="6 5" opacity="0.8"/>'
        else:
            e_svg += f'<path d="{d}" fill="none" stroke="{t["pend"]}" stroke-opacity="{t["pend_op"]}" stroke-width="2" stroke-dasharray="4 5"/>'

    nodes = (
        node_ok(790, 205, "scaffold")
        + node_ok(890, 140, "implement")
        + node_ok(890, 270, "docs", chip="ADVANCE")
        # test: blocked with retry loop
        + f'''<g transform="translate(1000 140)">
      <circle r="38" fill="{t["bad_fill"]}" fill-opacity="0.12" filter="url(#soft)"/>
      <circle r="26" fill="{t["node_bg"]}" stroke="{t["bad"]}" stroke-width="2.5"/>
      <circle r="26" fill="{t["bad_fill"]}" fill-opacity="0.14"/>
      <path d="M-8 -8 L8 8 M8 -8 L-8 8" stroke="{t["bad_ink"]}" stroke-width="3.5" stroke-linecap="round"/>
      <text y="46" text-anchor="middle" font-size="13" fill="{t["label"]}">test</text>
      <rect x="-34" y="-66" width="68" height="24" rx="7" fill="{t["bad_fill"]}" fill-opacity="0.18" stroke="{t["bad"]}" stroke-opacity="0.7"/>
      <text y="-49" text-anchor="middle" font-size="11" font-weight="700" fill="{t["bad_chip"]}">BLOCK</text>
      <path d="M-20 -24 C -46 -44, 46 -44, 20 -24" fill="none" stroke="{t["edge"]}" stroke-width="2" stroke-dasharray="4 4" marker-end="url(#arrow)"/>
    </g>'''
        # debug: pending branch target
        + f'''<g transform="translate(1100 270)">
      <circle r="22" fill="{t["node_bg"]}" stroke="{t["bad"]}" stroke-opacity="0.8" stroke-width="2" stroke-dasharray="4 4"/>
      <circle cx="-2" cy="-2" r="7" fill="none" stroke="{t["bad"]}" stroke-width="2.5"/><path d="M3 3 L9 9" stroke="{t["bad"]}" stroke-width="3" stroke-linecap="round"/>
      <text y="42" text-anchor="middle" font-size="13" fill="{t["label"]}">debug</text>
      <text y="-30" text-anchor="middle" font-size="11" fill="{t["bad"]}" opacity="0.9">on_fail</text>
    </g>'''
        # release: goal
        + f'''<g transform="translate(1200 205)">
      <rect x="-26" y="-26" width="52" height="52" rx="13" fill="{t["node_bg"]}" stroke="{t["pend"]}" stroke-opacity="{t["pend_op"]}" stroke-width="2" stroke-dasharray="4 4"/>
      <path d="M0 -12 L3.5 -3.9 L12.3 -3.8 L5.5 1.8 L7.9 10.4 L0 5.6 L-7.9 10.4 L-5.5 1.8 L-12.3 -3.8 L-3.5 -3.9 Z" fill="{t["star"]}" fill-opacity="0.85"/>
      <text y="46" text-anchor="middle" font-size="13" fill="{t["label"]}">release</text>
    </g>'''
    )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="400" viewBox="0 0 1280 400" role="img" aria-labelledby="t d">
  <title id="t">Jevflow</title>
  <desc id="d">Jevflow banner: a branching phase DAG where tests are blocked and a debug branch waits on failure.</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{t["bg0"]}"/><stop offset="0.55" stop-color="{t["bg1"]}"/><stop offset="1" stop-color="{t["bg2"]}"/></linearGradient>
    <radialGradient id="glowA" cx="0.18" cy="0.2" r="0.55"><stop offset="0" stop-color="{t["glowA"]}" stop-opacity="{t["glowA_op"]}"/><stop offset="1" stop-color="{t["glowA"]}" stop-opacity="0"/></radialGradient>
    <radialGradient id="glowB" cx="0.9" cy="0.95" r="0.6"><stop offset="0" stop-color="{t["glowB"]}" stop-opacity="{t["glowB_op"]}"/><stop offset="1" stop-color="{t["glowB"]}" stop-opacity="0"/></radialGradient>
    <linearGradient id="word" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{t["w0"]}"/><stop offset="0.6" stop-color="{t["w1"]}"/><stop offset="1" stop-color="{t["w2"]}"/></linearGradient>
    <linearGradient id="flow" gradientUnits="userSpaceOnUse" x1="780" y1="0" x2="1210" y2="0"><stop offset="0" stop-color="#6d5dfc"/><stop offset="1" stop-color="#20d6a4"/></linearGradient>
    <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" stroke="{t["grid"]}" stroke-opacity="{t["grid_op"]}" stroke-width="1"/></pattern>
    <filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter>
    <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{t["edge"]}"/></marker>
  </defs>
  <rect width="1280" height="400" fill="url(#bg)"/>
  <rect width="1280" height="400" fill="url(#grid)"/>
  <rect width="1280" height="400" fill="url(#glowA)"/>
  <rect width="1280" height="400" fill="url(#glowB)"/>
  <g font-family="Inter, 'Segoe UI', Helvetica, Arial, sans-serif">
    <text x="80" y="150" font-size="92" font-weight="800" letter-spacing="-3" fill="url(#word)">Jevflow</text>
    <text x="84" y="196" font-size="23" font-weight="600" fill="{t["tag"]}">Your coding agent says “done”. Jevflow checks.</text>
    <text x="84" y="230" font-size="17" fill="{t["sub"]}">Phase-by-phase guardrails for Claude Code.</text>
    <text x="84" y="254" font-size="17" fill="{t["sub"]}">Judged by Jev, decided by code.</text>
    <g font-size="14" font-weight="600">
      <rect x="84" y="282" width="150" height="30" rx="15" fill="{t["pill_fill"]}" fill-opacity="{t["pill_fop"]}" stroke="{t["pill_stroke"]}" stroke-opacity="{t["pill_sop"]}"/>
      <text x="159" y="302" text-anchor="middle" fill="{t["pill_text"]}">Claude Code plugin</text>
      <rect x="244" y="282" width="118" height="30" rx="15" fill="{t["pill_fill"]}" fill-opacity="{t["pill_fop"]}" stroke="{t["pill_stroke"]}" stroke-opacity="{t["pill_sop"]}"/>
      <text x="303" y="302" text-anchor="middle" fill="{t["pill_text"]}">stdlib only</text>
      <rect x="372" y="282" width="136" height="30" rx="15" fill="{t["pill_fill"]}" fill-opacity="{t["pill_fop"]}" stroke="{t["pill_stroke"]}" stroke-opacity="{t["pill_sop"]}"/>
      <text x="440" y="302" text-anchor="middle" fill="{t["pill_text"]}">auto-restart</text>
    </g>
  </g>
  <g font-family="'JetBrains Mono', 'DejaVu Sans Mono', Menlo, Consolas, monospace">
    {e_svg}
    {nodes}
    <text x="995" y="365" text-anchor="middle" font-size="13" fill="{t["readout"]}">claims_done 0.92   phase_done 0.02   check FAIL   -&gt; keep going</text>
  </g>
</svg>
'''


# ---------------------------------------------------------------- demo gif
SPIN = "|/-" + chr(92)
PHASES = ["scaffold", "implement", "docs", "test", "debug"]
C = dict(bg="#0d1117", bar="#161b22", fg="#c9d1d9", dim="#6e7681", ok="#3fb950", bad="#f85149",
         warn="#d29922", act="#a5a0ff", cyan="#56d4dd", prompt="#7ee787")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def gif_frame(lines, statuses, current, spinner=None, footer=""):
    W, H = 1200, 600
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" rx="14" fill="{C["bg"]}"/>',
           f'<rect width="{W}" height="42" rx="14" fill="{C["bar"]}"/><rect y="28" width="{W}" height="14" fill="{C["bar"]}"/>',
           '<circle cx="24" cy="21" r="7" fill="#ff5f57"/><circle cx="46" cy="21" r="7" fill="#febc2e"/><circle cx="68" cy="21" r="7" fill="#28c840"/>',
           f'<text x="600" y="26" text-anchor="middle" font-family="DejaVu Sans" font-size="14" fill="{C["dim"]}">claude --plugin-dir ~/jevflow  ·  todo</text>',
           '<g font-family="DejaVu Sans Mono" font-size="15">']
    # phase strip
    x0, y0, gap = 70, 88, 220
    for i, p in enumerate(PHASES):
        x = x0 + i * gap
        st = statuses.get(p, "pending")
        col = {"done": C["ok"], "active": C["act"], "pending": C["dim"], "branch": C["dim"]}[st]
        if i < len(PHASES) - 1:
            dash = ' stroke-dasharray="5 5"' if PHASES[i + 1] == "debug" else ""
            out.append(f'<line x1="{x+18}" y1="{y0}" x2="{x+gap-18}" y2="{y0}" stroke="{C["dim"]}" stroke-width="2"{dash}/>')
        fill = col if st == "done" else "none"
        out.append(f'<circle cx="{x}" cy="{y0}" r="12" fill="{fill}" fill-opacity="0.25" stroke="{col}" stroke-width="2.5"'
                   + (' stroke-dasharray="4 3"' if p == "debug" else "") + '/>')
        if st == "done":
            out.append(f'<path d="M{x-5} {y0} l3.5 3.5 l7 -7.5" fill="none" stroke="{C["ok"]}" stroke-width="2.5" stroke-linecap="round"/>')
        if p == current and st != "done":
            out.append(f'<circle cx="{x}" cy="{y0}" r="4.5" fill="{C["act"]}"/>')
        out.append(f'<text x="{x}" y="{y0+32}" text-anchor="middle" fill="{col}" font-size="14">{p}</text>')
    out.append(f'<line x1="30" y1="142" x2="{W-30}" y2="142" stroke="#21262d" stroke-width="1"/>')
    y = 176
    for segs in lines[-14:]:
        x = 34
        parts = []
        for text, color in segs:
            parts.append(f'<tspan fill="{color}">{esc(text)}</tspan>')
        out.append(f'<text x="{x}" y="{y}" xml:space="preserve">{"".join(parts)}</text>')
        y += 27
    if spinner is not None:
        out.append(f'<text x="34" y="{y}" fill="{C["warn"]}" xml:space="preserve">{SPIN[spinner % 4]} Claude is working...</text>')
    if footer:
        out.append(f'<text x="34" y="{H-24}" fill="{C["dim"]}" font-size="13">{esc(footer)}</text>')
    out.append('</g></svg>')
    return "".join(out)


def build_gif(journal_path, out_path):
    hist = json.load(open(journal_path))["history"]
    t0 = hist[0]["ts"]
    statuses = {p: "pending" for p in PHASES}
    statuses["debug"] = "branch"
    current = "scaffold"
    statuses["scaffold"] = "active"
    lines = [[("$ ", C["prompt"]), ("claude --plugin-dir ~/jevflow", C["fg"])],
             [("> ", C["act"]), ("Work toward the jevflow goal.", C["fg"])]]
    frames = []  # (svg, ms)
    frames.append((gif_frame(lines, statuses, current), 1400))
    for h in hist:
        mmss = f'{int((h["ts"]-t0)//60):02d}:{int((h["ts"]-t0)%60):02d}'
        if h["event"] == "session_start":
            lines.append([(f"{mmss} ", C["dim"]), ("jevflow ", C["cyan"]), ("goal + 5 phases injected; current: scaffold", C["fg"])])
            frames.append((gif_frame(lines, statuses, current), 1100))
            continue
        for k in range(4):  # working spinner
            frames.append((gif_frame(lines, statuses, current, spinner=k), 180))
        dec, cond, p = h["decision"], h["condition"], h["probs"] or {}
        colr = {"BLOCK": C["bad"], "ADVANCE": C["ok"], "ALLOW_STOP": C["act"]}[dec]
        lines.append([(f"{mmss} ", C["dim"]), ("stop  ", C["fg"]), (f"{dec:<12}", colr), (f"{cond}", C["warn"])])
        if dec == "BLOCK":
            reason = h["reason"].split("\n")[0]
            lines.append([("      ", C["fg"]), ("-> ", C["bad"]), (reason if len(reason) <= 88 else reason[:87] + "...", C["dim"])])
        elif dec == "ADVANCE":
            prev = current
            statuses[prev] = "done"
            current = h["to_phase"]
            statuses[current] = "active"
            lines.append([("      ", C["fg"]), ("-> ", C["ok"]), (f"'{prev}' done, now working on '{current}'", C["dim"])])
        else:
            statuses[current] = "done"
            lines.append([("      ", C["fg"]), ("-> ", C["act"]), ("goal complete: every phase done, every check passes", C["fg"])])
        if p:
            cp = p.get("current_phase", ["?", 0])
            lines.append([("      ", C["fg"]), (f"jev  phase={cp[0]} {cp[1]:.2f}  claims_done={p.get('claims_done', 0):.2f}  stuck={p.get('stuck', 0):.2f}", C["dim"])])
        frames.append((gif_frame(lines, statuses, current), 2200))
    frames.append((gif_frame(lines, statuses, current, footer="6 stops · 2 early claims caught · goal complete in 1m34s · real Claude Code run"), 4000))

    imgs, durs = [], []
    for svg, ms in frames:
        png = cairosvg.svg2png(bytestring=svg.encode(), output_width=1200)
        imgs.append(Image.open(io.BytesIO(png)).convert("RGB").quantize(colors=128, method=Image.Quantize.MEDIANCUT))
        durs.append(ms)
    imgs[0].save(out_path, save_all=True, append_images=imgs[1:], duration=durs, loop=0, optimize=True, disposal=1)
    return len(imgs)


def main():
    for name, theme in THEMES.items():
        svg = banner(theme)
        open(os.path.join(HERE, f"banner-{name}.svg"), "w").write(svg)
        cairosvg.svg2png(bytestring=svg.encode(), write_to=os.path.join(HERE, f"banner-{name}.png"), output_width=2560)
    journal = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "demo_run.json")
    n = build_gif(journal, os.path.join(HERE, "demo.gif"))
    print(f"banners written; demo.gif {n} frames")


if __name__ == "__main__":
    main()
