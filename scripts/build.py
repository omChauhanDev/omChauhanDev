#!/usr/bin/env python3
"""Redraw the profile README from profile.toml and live GitHub numbers.

    pip install -r scripts/requirements.txt
    GITHUB_TOKEN=$(gh auth token) python scripts/build.py
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import tomllib
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from svgtext import Font, num

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FONTS = ASSETS / "fonts"
CONFIG = tomllib.loads((ROOT / "profile.toml").read_text())
USER = CONFIG["user"]
RAW = f"https://raw.githubusercontent.com/{USER}/{USER}/main/assets"

SANS = Font(FONTS / "Geist-Regular.ttf", "r")
MEDIUM = Font(FONTS / "Geist-Medium.ttf", "m")
SEMIBOLD = Font(FONTS / "Geist-SemiBold.ttf", "s")
MONO = Font(FONTS / "GeistMono-Regular.ttf", "o")

# GitHub's own surface, border and text colours, so the cards sit in the page like native boxes.
THEMES = {
    "dark": {"card": "#151b23", "edge": "#3d444d", "rule": "#262c36",
             "fg": "#f0f6fc", "muted": "#9198a1", "live": "#3fb950"},
    "light": {"card": "#f6f8fa", "edge": "#d1d9e0", "rule": "#dfe3e8",
              "fg": "#1f2328", "muted": "#59636e", "live": "#1a7f37"},
}

# The README column is 846 px wide from a 1295 px window up: two 422 px cards per row.
# Narrower windows get one card per row, drawn at the width that column has, so the type
# stays the same size everywhere (GitHub's sidebar shrinks below 1012 px, then stacks at 768).
# Below 768 px GitHub stacks the sidebar above the README and the column is the window minus 82;
# from 768 to 1011 it is the window minus 370, then minus 434 up to its 846 px cap.
CARD_W, STATEMENT_W = 422, 844
DRAWINGS = {"phone-": 340, "w420-": 420, "w580-": 580, "w640-": 640, "w700-": 700}
CARD_TIERS = [  # first match wins, so narrowest first
    ("phone-", "(max-width: 501px)"),
    ("w420-", "(max-width: 661px)"),
    ("w580-", "(max-width: 721px)"),
    ("w640-", "(max-width: 767px)"),
    ("w420-", "(max-width: 899px)"),
    ("w580-", "(max-width: 1099px)"),
    ("w700-", "(max-width: 1294px)"),
]
STATEMENT_DRAWINGS = {"phone-": (340, 24, 31), "w420-": (420, 28, 36)}
STATEMENT_TIERS = [("phone-", "(max-width: 501px)"), ("w420-", "(max-width: 1099px)")]
GUTTER = 8                       # transparent edge around each card; two make the gaps
HEIGHTS = {"tall": 244, "compact": 162}   # before the header band grows (see SHIFT)
PAD = 24


# ---------------------------------------------------------------- data


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("set GITHUB_TOKEN (locally: export GITHUB_TOKEN=$(gh auth token))")
    return token


def graphql(query: str, **variables) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {_token()}", "User-Agent": USER},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"GraphQL error: {body['errors']}")
    return body["data"]


def rest(path: str):
    req = urllib.request.Request(f"https://api.github.com/{path}",
                                 headers={"Authorization": f"bearer {_token()}", "User-Agent": USER})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { title mergedAt repository { nameWithOwner owner { login } } } }
  }
}
"""


def merged_prs() -> list[dict]:
    query = f"is:pr is:public is:merged author:{USER} -user:{USER}"
    prs, after = [], None
    while True:
        page = graphql(SEARCH, q=query, after=after)["search"]
        prs += [n for n in page["nodes"]
                if n and n["repository"]["nameWithOwner"] not in CONFIG["exclude_repos"]]
        if not page["pageInfo"]["hasNextPage"]:
            return prs
        after = page["pageInfo"]["endCursor"]


def stars(repos: list[str]) -> dict[str, int]:
    fields = " ".join(
        f'r{i}: repository(owner: "{owner}", name: "{name}") {{ stargazerCount }}'
        for i, (owner, name) in enumerate(repo.split("/") for repo in repos)
    )
    data = graphql(f"query {{ {fields} }}")
    return {repo: data[f"r{i}"]["stargazerCount"] for i, repo in enumerate(repos)}


def contributor_rank(repo: str) -> tuple[int, int]:
    """Place among the repo's human contributors, as GitHub's contributors list ranks them."""
    rank = total = 0
    page = 1
    while batch := rest(f"repos/{repo}/contributors?per_page=100&page={page}"):
        for person in batch:
            if person.get("type") == "User":
                total += 1
                if person["login"].lower() == USER.lower():
                    rank = total
        page += 1
    return rank, total


def pull_requests(refs: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    fields = " ".join(
        f'p{i}: repository(owner: "{repo.split("/")[0]}", name: "{repo.split("/")[1]}") '
        f'{{ pullRequest(number: {number}) {{ merged url }} }}'
        for i, (repo, number) in enumerate(refs)
    )
    data = graphql(f"query {{ {fields} }}")
    return {ref: data[f"p{i}"]["pullRequest"] for i, ref in enumerate(refs)}


def search_link(*orgs: str) -> str:
    query = f"is:pr is:merged author:{USER} " + " ".join(f"org:{o}" for o in orgs)
    return "https://github.com/search?" + urllib.parse.urlencode({"q": query, "type": "pullrequests"})


# ------------------------------------------------------------- helpers


def short(n: int) -> str:
    """Counts the way GitHub prints them: 430, 1.5k, 15k, 16.9k."""
    return str(n) if n < 1000 else f"{n / 1000:.1f}".removesuffix(".0") + "k"


def ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def write_if_changed(path: Path, text: str) -> None:
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        print(f"updated {path.relative_to(ROOT)}")


class Svg:
    def __init__(self, width: int, height: int, label: str):
        self.width, self.height, self.label = width, height, label
        self.defs: dict[str, str] = {}
        self.body: list[str] = []
        self.dy = 0.0   # added to every y below the header band (see head())

    def add(self, element: str) -> None:
        self.body.append(element)

    def text(self, font: Font, text: str, size: float, x: float, y: float, fill: str,
             tracking: float = 0.0, anchor: str = "start") -> float:
        placed, width = font.place(text, size, x, y + self.dy, tracking, anchor)
        scale = f"{size / font.upem:.6g}"
        uses = []
        for gid, gx, gy in placed:
            outline = font.outline(gid)
            if outline:
                ref = f"{font.key}{gid}"
                self.defs.setdefault(ref, outline)
                uses.append(f'<use href="#{ref}" transform="translate({num(gx)} {num(gy)}) scale({scale})"/>')
        if uses:
            self.add(f'<g fill="{fill}">{"".join(uses)}</g>')
        return width

    def __str__(self) -> str:
        label = html.escape(self.label)
        defs = "".join(f'<path id="{ref}" d="{d}"/>' for ref, d in self.defs.items())
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
                f'viewBox="0 0 {self.width} {self.height}" role="img" aria-label="{label}">'
                f"<title>{label}</title><defs>{defs}</defs>{''.join(self.body)}</svg>\n")


# --------------------------------------------------------------- cards


def frame(svg: Svg, t: dict, x0: float, w: float) -> tuple[float, float]:
    svg.add(f'<rect x="{num(x0 + .5)}" y="{GUTTER + .5}" width="{num(w - 1)}" '
            f'height="{svg.height - 2 * GUTTER - 1}" rx="6" fill="{t["card"]}" stroke="{t["edge"]}"/>')
    left, right = x0 + PAD, x0 + w - PAD
    arrow(svg, t, right, BAND_MID)
    return left, right


def arrow(svg: Svg, t: dict, right: float, mid: float) -> None:
    """The north-east arrow that marks a link, its ink ending at `right` and centred on `mid`."""
    top = mid - 4.5
    svg.add(f'<path d="M{num(right - 9)} {num(top)}h9v9M{num(right)} {num(top)}l-10 10" fill="none" stroke="{t["muted"]}" '
            'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>')


LOGO = int(os.environ.get("LOGO_SIZE", "24"))   # organisation mark, px
BAND_TOP = GUTTER + PAD                          # the mark sits one padding from the card's top edge
BAND_MID = BAND_TOP + LOGO / 2                   # label and arrow centre on the mark
LABEL_Y = BAND_MID + 3.4                         # 12 px mono: baseline that centres its x-height
SHIFT = BAND_TOP + LOGO + 28 - 76                # title baseline sits 12 px under the mark, plus cap height


def logo_mark(svg: Svg, t: dict, x: float, y: float, path: str, size: float = LOGO) -> None:
    """An organisation's own mark, unaltered: raster marks as GitHub-style rounded tiles,
    vector marks drawn as they are."""
    file = ASSETS / path
    if file.suffix == ".svg":
        source = file.read_text()
        box = [float(v) for v in re.search(r'viewBox="([^"]+)"', source).group(1).split()]
        inner = re.search(r"<svg[^>]*>(.*)</svg>", source, re.S).group(1)
        svg.add(f'<g transform="translate({num(x)} {num(y)}) scale({size / box[3]:.6g})">{inner}</g>')
        return
    data = base64.b64encode(file.read_bytes()).decode()
    corner = 5 * size / LOGO
    svg.add(f'<clipPath id="logo"><rect x="{num(x)}" y="{num(y)}" width="{num(size)}" height="{num(size)}" '
            f'rx="{num(corner)}"/></clipPath>'
            f'<image href="data:image/png;base64,{data}" x="{num(x)}" y="{num(y)}" width="{num(size)}" '
            f'height="{num(size)}" clip-path="url(#logo)"/>'
            f'<rect x="{num(x + .5)}" y="{num(y + .5)}" width="{num(size - 1)}" height="{num(size - 1)}" '
            f'rx="{num(corner - .5)}" fill="none" stroke="{t["edge"]}"/>')


def band(svg: Svg, t: dict, left: float, label: str, logo: str | None = None) -> None:
    """The header band: an optional organisation mark and the label, then shift what follows."""
    x = left
    if logo:
        logo_mark(svg, t, left, BAND_TOP, logo)
        x += LOGO + 10
    svg.text(MONO, label, 12, x, LABEL_Y, t["muted"])
    svg.dy = SHIFT


def head(svg: Svg, t: dict, left: float, label: str, title: str, logo: str | None = None) -> None:
    band(svg, t, left, label, logo)
    svg.text(SEMIBOLD, title, 22, left, 76, t["fg"], tracking=-0.3)


def figure(svg: Svg, t: dict, left: float, text: str, y: float = 190) -> None:
    svg.text(SEMIBOLD, text, 48, left - SEMIBOLD.left_bearing(text, 48), y, t["fg"], tracking=-1.4)


def meta_right(svg: Svg, t: dict, right: float, text: str, y: float) -> None:
    """Right-aligned footnote whose ink, not its advance, ends flush with the arrow above it."""
    svg.text(MONO, text, 12, right + MONO.right_bearing(text, 12), y, t["muted"], anchor="end")


def fit(svg: Svg, lines: list, shrink: bool) -> float:
    """On one-card-per-row layouts, close the gap a missing second line of text would leave."""
    lift = 19 * (2 - len(lines)) if shrink else 0
    svg.height -= lift
    return lift


def lines_of(text: str, width: float, most: int, name: str) -> list[str]:
    lines = SANS.wrap(text, 14.5, width)
    if len(lines) > most:
        raise SystemExit(f"{name}: '{text}' needs {len(lines)} lines at this width; {most} fit")
    return lines


def project_card(svg, t, x0, w, card, big, unit, meta, data=None, shrink=False):
    """Tall card: label, title, up to two lines of text, a large figure with its unit, and a footer.

    `data` is an optional (number, words) line set like the compact cards' footers, on the
    second text line, level with the card beside it.
    """
    lines = lines_of(card["text"], w - 2 * PAD, 1 if data else 2, card["title"])
    lift = fit(svg, lines + ([""] if data else []), shrink)
    left, right = frame(svg, t, x0, w)
    head(svg, t, left, card["label"], card["title"], card.get("logo"))
    for i, line in enumerate(lines):
        svg.text(SANS, line, 14.5, left, 102 + 19 * i, t["muted"])
    if data:
        x = left + svg.text(MEDIUM, data[0], 14.5, left, 121, t["fg"])
        svg.text(SANS, data[1], 14.5, x, 121, t["muted"])
    figure(svg, t, left, big, 190 - lift)
    svg.text(SANS, unit, 13, left, 214 - lift, t["muted"])
    meta_right(svg, t, right, meta, 214 - lift)


def compact_card(svg, t, x0, w, card, count, meta):
    """Short card for smaller contributions: no display number."""
    left, right = frame(svg, t, x0, w)
    head(svg, t, left, card["label"], card["title"], card.get("logo"))
    svg.text(SANS, lines_of(card["text"], right - left, 1, card["title"])[0], 14.5, left, 102, t["muted"])
    x = left + svg.text(MEDIUM, str(count), 13, left, 132, t["fg"])
    svg.text(SANS, " merged pull request" + ("s" if count != 1 else ""), 13, x, 132, t["muted"])
    meta_right(svg, t, right, meta, 132)


def statement(t: dict, width: int, size: float, leading: float) -> Svg:
    lead, rest = CONFIG["statement"]
    lines = ([(line, t["fg"]) for line in MEDIUM.wrap(lead, size, width)]
             + [(line, t["muted"]) for line in MEDIUM.wrap(rest, size, width)])
    svg = Svg(width, round(size + leading * (len(lines) - 1) + size * 0.3), f"{lead} {rest}")
    for i, (line, fill) in enumerate(lines):
        svg.text(MEDIUM, line, size, -MEDIUM.left_bearing(line, size), size + leading * i, fill,
                 tracking=-0.02 * size)
    return svg


# ------------------------------------------------------------ selected

# The full-width block under the cards: a header, then one row per pull request. GitHub links a
# whole image, so every row is an image of its own and the box is cut across them; each cut
# falls on the rule between two rows, where a seam from scaling cannot show.
ROW_WIDE = 560      # from this drawing width up, a row's mark sits beside its title
ROW_LOGO = 20       # the mark on wide rows; narrow rows use the cards' header band
ROW_TOP = 32        # wide rows: title baseline, from the row's top rule


def slice_box(svg: Svg, t: dict, top: float, bottom: float, rule: bool) -> None:
    """The block's box as it crosses this slice; whatever lies past the slice's edges is cut off."""
    svg.add(f'<rect x=".5" y="{num(top + .5)}" width="{svg.width - 1}" height="{num(bottom - top - 1)}" '
            f'rx="6" fill="{t["card"]}" stroke="{t["edge"]}"/>')
    if rule:
        svg.add(f'<path d="M1 .5H{svg.width - 1}" stroke="{t["rule"]}"/>')


def selected_head(t: dict, width: int, title: str, more: str) -> Svg:
    """The block's title, and on its right the way to every merged pull request."""
    left, right = PAD, width - PAD
    baseline = GUTTER + PAD + 16
    size = 22
    if SEMIBOLD.width(title, size, -0.3) + 16 + SANS.width(more, 13) + 19 > right - left:
        size, more = 20, more.rsplit(" ", 1)[0]   # phones: "All 89" beside a smaller title
    svg = Svg(width, baseline + PAD, f"{title}. {more}.")
    slice_box(svg, t, GUTTER, svg.height + 8, rule=False)
    svg.text(SEMIBOLD, title, size, left, baseline, t["fg"], tracking=-0.3)
    arrow(svg, t, right, baseline - 3.6)
    svg.text(SANS, more, 13, right - 19, baseline, t["muted"], anchor="end")
    return svg


def row_measure(width: int) -> float:
    """How wide a row's text may run."""
    return width - 2 * PAD - (ROW_LOGO + 14 if width >= ROW_WIDE else 0)


def selected_row(t: dict, width: int, pr: dict, count: int, last: bool) -> Svg:
    """One pull request: a short title, then what was broken and what the change does, set in
    `count` lines so that every row of the block has the same height."""
    left, right = PAD, width - PAD
    label = f"#{pr['number']}"   # the mark beside it already names the repository
    lines = SANS.wrap(pr["text"], 14.5, row_measure(width), count)
    if width >= ROW_WIDE:   # the mark beside the title, the repository at the end of its line
        x, title_y = left + ROW_LOGO + 14, ROW_TOP
        if SEMIBOLD.width(pr["title"], 16, -0.1) > right - 19 - 16 - MONO.width(label, 12) - x:
            raise SystemExit(f"{label}: title '{pr['title']}' runs into the repository at {width} px")
    else:                   # the cards' header band, then the title and text at full width
        x, title_y = left, 20 + LOGO + 20
    text_y = title_y + 21
    bottom = text_y + 19 * (len(lines) - 1) + 22
    svg = Svg(width, bottom + (GUTTER if last else 0), f"{pr['title']}, {pr['repo']} {label}: {pr['text']}")
    slice_box(svg, t, -8, bottom if last else bottom + 8, rule=True)
    if width >= ROW_WIDE:
        logo_mark(svg, t, left, title_y - 5.6 - ROW_LOGO / 2, pr["logo"], ROW_LOGO)
        arrow(svg, t, right, title_y - 4.6)
        svg.text(MONO, label, 12, right - 19, title_y, t["muted"], anchor="end")
    else:
        logo_mark(svg, t, left, 20, pr["logo"])
        svg.text(MONO, label, 12, left + LOGO + 10, 20 + LOGO / 2 + 3.4, t["muted"])
        arrow(svg, t, right, 20 + LOGO / 2)
    svg.text(SEMIBOLD, pr["title"], 16, x, title_y, t["fg"], tracking=-0.1)
    for i, line in enumerate(lines):
        svg.text(SANS, line, 14.5, x, text_y + 19 * i, t["muted"])
    return svg


# -------------------------------------------------------------- readme


def asset_url(name: str) -> str:
    """Raw URL with a fingerprint of the file, so GitHub's image proxy never serves an old drawing."""
    return f"{RAW}/{name}?v={hashlib.sha1((ASSETS / name).read_bytes()).hexdigest()[:10]}"


def picture(name: str, alt: str, tiers: list[tuple[str, str]]) -> str:
    """<picture> serving each width tier's drawing, dark first; the light desktop drawing is the fallback."""
    sources = []
    for tag, media in tiers:
        sources.append(f'<source media="{media} and (prefers-color-scheme: dark)" srcset="{asset_url(f"{name}-{tag}dark.svg")}">')
        sources.append(f'<source media="{media}" srcset="{asset_url(f"{name}-{tag}light.svg")}">')
    sources.append(f'<source media="(prefers-color-scheme: dark)" srcset="{asset_url(f"{name}-dark.svg")}">')
    return (f"<picture>{''.join(sources)}"
            f'<img src="{asset_url(f"{name}-light.svg")}" align="top" alt="{html.escape(alt)}">'
            "</picture>")


def main() -> None:
    prs = merged_prs()
    by_org = Counter(pr["repository"]["owner"]["login"] for pr in prs)
    total = sum(by_org.values())
    repo_stars = stars([c["repo"] for c in CONFIG["cards"] if "repo" in c])
    selected = CONFIG["selected"]
    found = pull_requests([(pr["repo"], pr["number"]) for pr in selected["prs"]])

    drawn = []   # (file name, alt, link), in reading order
    for i, card in enumerate(CONFIG["cards"]):
        size = card.get("size", "tall")
        count = by_org.get(card["org"], 0)
        name = card["title"].lower().replace(" ", "-")
        meta = card.get("meta") or f"{short(repo_stars[card['repo']])} stars"
        prs_line = f"{count} merged pull request{'s' if count != 1 else ''}"
        big, unit, extra = str(count), prs_line.split(" ", 1)[1], None
        if "rank_repo" in card:
            rank, people = contributor_rank(card["rank_repo"])
            if rank:  # the standing among contributors outranks the raw count
                big, unit, extra = ordinal(rank), f"of {people} contributors", (str(count), prs_line[len(str(count)):])
        alt = f"{card['title']}: {card['text']} {f'{big} {unit}; ' if extra else ''}{prs_line}. {meta}."
        link = card.get("link") or search_link(card["org"])

        def draw(svg, t, x0, w, shrink, card=card, count=count, big=big, unit=unit, meta=meta,
                 extra=extra, size=size):
            if size == "compact":
                compact_card(svg, t, x0, w, card, count, meta)
            else:
                project_card(svg, t, x0, w, card, big, unit, meta, extra, shrink)

        height = HEIGHTS[size] + round(SHIFT)
        for theme, t in THEMES.items():
            for variant, width, x0, w in ([("", CARD_W, 0 if i % 2 == 0 else GUTTER, CARD_W - GUTTER)]
                                          + [(tag, px, 0, px) for tag, px in DRAWINGS.items()]):
                svg = Svg(width, height, alt)
                draw(svg, t, x0, w, shrink=bool(variant))
                write_if_changed(ASSETS / f"{name}-{variant}{theme}.svg", str(svg))
        drawn.append((name, alt, link))

    for theme, t in THEMES.items():
        write_if_changed(ASSETS / f"statement-{theme}.svg", str(statement(t, STATEMENT_W, 34, 42)))
        for tag, (width, size, leading) in STATEMENT_DRAWINGS.items():
            write_if_changed(ASSETS / f"statement-{tag}{theme}.svg", str(statement(t, width, size, leading)))

    everything = search_link(*by_org)
    widths = [("", STATEMENT_W)] + list(DRAWINGS.items())
    more = f"All {total} merged"
    block = [("selected", f"{selected['title']}. {more}.", everything)]
    for theme, t in THEMES.items():
        for tag, width in widths:
            write_if_changed(ASSETS / f"selected-{tag}{theme}.svg", str(selected_head(t, width, selected["title"], more)))
    counts = {width: max(len(SANS.wrap(pr["text"], 14.5, row_measure(width))) for pr in selected["prs"])
              for _, width in widths}
    for i, pr in enumerate(selected["prs"]):
        found_pr = found[(pr["repo"], pr["number"])]
        if not found_pr["merged"]:
            raise SystemExit(f"{pr['repo']} #{pr['number']} is not merged")
        name = f"selected-{pr['repo'].split('/')[1]}-{pr['number']}"
        last = i == len(selected["prs"]) - 1
        for theme, t in THEMES.items():
            for tag, width in widths:
                svg = selected_row(t, width, pr, counts[width], last)
                write_if_changed(ASSETS / f"{name}-{tag}{theme}.svg", str(svg))
        block.append((name, svg.label, found_pr["url"]))

    pictures = [f'<a href="{html.escape(link)}">{picture(name, alt, CARD_TIERS)}</a>' for name, alt, link in drawn]
    rows = ["".join(pictures[j:j + 2]) for j in range(0, len(pictures), 2)]
    rows += [f'<a href="{html.escape(link)}">{picture(name, alt, CARD_TIERS)}</a>' for name, alt, link in block]
    everything = html.escape(everything)
    lead, rest = CONFIG["statement"]
    readme = (
        "<!-- Built by scripts/build.py from profile.toml. Edit those; this file is overwritten. -->\n\n"
        f'<p><a href="{everything}">{picture("statement", f"{lead} {rest}", STATEMENT_TIERS)}</a></p>\n\n'
        "<p>\n" + "<br>\n".join(rows) + "\n</p>\n"
    )
    write_if_changed(ROOT / "README.md", readme)
    print(f"{total} merged PRs across {len(by_org)} organizations")


if __name__ == "__main__":
    main()
