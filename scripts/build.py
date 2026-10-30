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
from datetime import date
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
HEIGHTS = {"tall": 244, "story": 181, "compact": 162}   # before the header band grows (see SHIFT)
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
        f'{{ pullRequest(number: {number}) {{ additions deletions mergedAt url }} }}'
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
    top = BAND_MID - 4.5
    svg.add(f'<path d="M{num(right - 9)} {num(top)}h9v9M{num(right)} {num(top)}l-10 10" fill="none" stroke="{t["muted"]}" '
            'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>')
    return left, right


LOGO = int(os.environ.get("LOGO_SIZE", "24"))   # organisation mark, px
BAND_TOP = GUTTER + PAD                          # the mark sits one padding from the card's top edge
BAND_MID = BAND_TOP + LOGO / 2                   # label and arrow centre on the mark
LABEL_Y = BAND_MID + 3.4                         # 12 px mono: baseline that centres its x-height
SHIFT = BAND_TOP + LOGO + 28 - 76                # title baseline sits 12 px under the mark, plus cap height


def logo_mark(svg: Svg, t: dict, x: float, y: float, path: str) -> None:
    """An organisation's own mark, unaltered: raster marks as GitHub-style rounded tiles,
    vector marks drawn as they are."""
    file = ASSETS / path
    if file.suffix == ".svg":
        source = file.read_text()
        box = [float(v) for v in re.search(r'viewBox="([^"]+)"', source).group(1).split()]
        inner = re.search(r"<svg[^>]*>(.*)</svg>", source, re.S).group(1)
        svg.add(f'<g transform="translate({num(x)} {num(y)}) scale({LOGO / box[3]:.6g})">{inner}</g>')
        return
    data = base64.b64encode(file.read_bytes()).decode()
    svg.add(f'<clipPath id="logo"><rect x="{num(x)}" y="{num(y)}" width="{LOGO}" height="{LOGO}" rx="5"/></clipPath>'
            f'<image href="data:image/png;base64,{data}" x="{num(x)}" y="{num(y)}" width="{LOGO}" height="{LOGO}" '
            'clip-path="url(#logo)"/>'
            f'<rect x="{num(x + .5)}" y="{num(y + .5)}" width="{LOGO - 1}" height="{LOGO - 1}" rx="4.5" '
            f'fill="none" stroke="{t["edge"]}"/>')


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


def story_card(svg, t, x0, w, card, pr, shrink=False):
    """One merged pull request in a sentence: what was broken and what the change does."""
    lines = lines_of(card["text"], w - 2 * PAD, 2, card["title"])
    lift = fit(svg, lines, shrink)
    left, right = frame(svg, t, x0, w)
    head(svg, t, left, card["label"], card["title"], card.get("logo"))
    for i, line in enumerate(lines):
        svg.text(SANS, line, 14.5, left, 102 + 19 * i, t["muted"])
    merged = date.fromisoformat(pr["mergedAt"][:10])
    svg.text(SANS, f"Merged {merged:%b %Y}", 13, left, 151 - lift, t["muted"])
    meta_right(svg, t, right, f"+{pr['additions']} −{pr['deletions']}", 151 - lift)


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
    repo_stars = stars([c["repo"] for c in CONFIG["cards"] if c["kind"] == "project" and "repo" in c])
    stories = pull_requests([(c["repo"], c["number"]) for c in CONFIG["cards"] if c["kind"] == "pr"])

    drawn = []   # (file name, alt, link), in reading order
    for i, card in enumerate(CONFIG["cards"]):
        kind, size = card["kind"], card.get("size", "tall")
        if kind == "pr":
            pr = stories[(card["repo"], card["number"])]
            card = {**card, "label": f"{card['repo']} #{card['number']}"}
            name = f"pr-{card['repo'].split('/')[1]}-{card['number']}"
            alt = f"{card['title']}, {card['label']}: {card['text']} +{pr['additions']} −{pr['deletions']}."
            link = pr["url"]

            def draw(svg, t, x0, w, shrink, card=card, pr=pr):
                story_card(svg, t, x0, w, card, pr, shrink)
        else:
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

    everything = html.escape(search_link(*by_org))
    pictures = [f'<a href="{html.escape(link)}">{picture(name, alt, CARD_TIERS)}</a>' for name, alt, link in drawn]
    rows = ["".join(pictures[j:j + 2]) for j in range(0, len(pictures), 2)]
    lead, rest = CONFIG["statement"]
    readme = (
        "<!-- Built by scripts/build.py from profile.toml. Edit those; this file is overwritten. -->\n\n"
        f'<p><a href="{everything}">{picture("statement", f"{lead} {rest}", STATEMENT_TIERS)}</a></p>\n\n'
        "<p>\n" + "<br>\n".join(rows) + "\n</p>\n\n"
        f'<p><a href="{everything}">All {total} merged pull requests →</a></p>\n'
    )
    write_if_changed(ROOT / "README.md", readme)
    print(f"{total} merged PRs across {len(by_org)} organizations")


if __name__ == "__main__":
    main()
