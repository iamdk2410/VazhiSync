"""Grouped bar charts as plain SVG (B19): no plotting library needed.

One group per scenario, one bar per arm, labelled values; colours are fixed per arm and come
with a legend, so a chart reads the same in the docs and in a browser.
"""

from html import escape

COLOURS = {"control": "#9aa5b1", "advisor": "#2f6fdd", "advisor_ew": "#16a37b"}
LABELS = {"control": "No control", "advisor": "Advisor", "advisor_ew": "Advisor + early warning"}


def grouped_bars(
    title: str,
    unit: str,
    groups: list[str],
    series: dict[str, list[float | None]],
    *,
    lower_is_better: bool = True,
) -> str:
    width, height = 860, 380
    left, right, top, bottom = 60, 20, 60, 90
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [v for vs in series.values() for v in vs if v is not None]
    peak = max(values) if values else 1.0
    peak = peak * 1.15 or 1.0
    n_groups, n_series = len(groups), len(series)
    group_w = plot_w / max(n_groups, 1)
    bar_w = group_w * 0.8 / max(n_series, 1)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Segoe UI, Arial, sans-serif" font-size="11">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="24" font-size="15" font-weight="600">{escape(title)}</text>',
        f'<text x="{left}" y="42" fill="#555">{escape(unit)} - '
        f"{'lower' if lower_is_better else 'higher'} is better</text>",
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" '
        'stroke="#333"/>',
    ]
    for k in range(5):
        y = top + plot_h - plot_h * k / 4
        out.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#eee"/>'
            f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#666">'
            f"{peak * k / 4:.3g}</text>"
        )
    for g, name in enumerate(groups):
        x0 = left + g * group_w + group_w * 0.1
        for s, (arm, vs) in enumerate(series.items()):
            v = vs[g]
            if v is None:
                continue
            h = plot_h * v / peak
            x = x0 + s * bar_w
            out.append(
                f'<rect x="{x:.1f}" y="{top + plot_h - h:.1f}" width="{bar_w - 2:.1f}" '
                f'height="{h:.1f}" fill="{COLOURS.get(arm, "#888")}"/>'
                f'<text x="{x + bar_w / 2 - 1:.1f}" y="{top + plot_h - h - 3:.1f}" '
                f'text-anchor="middle" fill="#333" font-size="9">{v:.3g}</text>'
            )
        out.append(
            f'<text x="{x0 + group_w * 0.4:.1f}" y="{top + plot_h + 16}" text-anchor="middle">'
            f"{escape(name.replace('_', ' '))}</text>"
        )
    lx = left
    for arm in series:
        out.append(
            f'<rect x="{lx}" y="{height - 30}" width="12" height="12" '
            f'fill="{COLOURS.get(arm, "#888")}"/>'
            f'<text x="{lx + 16}" y="{height - 20}">{escape(LABELS.get(arm, arm))}</text>'
        )
        lx += 30 + 7 * len(LABELS.get(arm, arm))
    out.append("</svg>")
    return "\n".join(out)
