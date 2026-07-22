"""Netlist parsers that produce a normalized BoardDescription.

Tier 1 supports the KiCad ``.net`` S-expression format. The parser is stdlib-only
(a tiny S-expression reader) and hands the raw ``components`` / ``nets`` to
``board_model.build_board_description`` for normalization and pin-function
inference. Additional formats (Altium, OrCAD, CSV pin-maps) are planned in later
tiers; see ``docs/superpowers/plans/2026-07-01-netlist-board-model.md``.
"""

from mcp_server.board_model import build_board_description

# --- S-expression reader -----------------------------------------------------


def _tokenize_sexpr(text: str) -> list:
    """Tokenize S-expression text into ``(``, ``)`` and ``(kind, value)`` atoms."""
    tokens: list = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char in "()":
            tokens.append(char)
            i += 1
        elif char == '"':
            i += 1
            buf: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1  # consume closing quote
            tokens.append(("str", "".join(buf)))
        elif char.isspace():
            i += 1
        else:
            buf = []
            while i < n and not text[i].isspace() and text[i] not in '()"':
                buf.append(text[i])
                i += 1
            tokens.append(("sym", "".join(buf)))
    return tokens


def _parse_sexpr(tokens: list) -> list:
    """Parse a flat token list into nested lists of atoms."""
    pos = 0

    def parse_list() -> list:
        nonlocal pos
        node: list = []
        while pos < len(tokens):
            token = tokens[pos]
            if token == "(":
                pos += 1
                node.append(parse_list())
            elif token == ")":
                pos += 1
                return node
            else:
                node.append(token)
                pos += 1
        return node

    top: list = []
    while pos < len(tokens):
        if tokens[pos] == "(":
            pos += 1
            top.append(parse_list())
        else:
            pos += 1
    return top


def _head(node) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], tuple) and node[0][0] == "sym":
        return node[0][1]
    return None


def _find(node: list, tag: str):
    for child in node:
        if isinstance(child, list) and _head(child) == tag:
            return child
    return None


def _find_all(node, tag: str) -> list:
    if not isinstance(node, list):
        return []
    return [child for child in node if isinstance(child, list) and _head(child) == tag]


def _field(node: list, tag: str) -> str | None:
    """Return the atom value of a ``(tag "value")`` child."""
    child = _find(node, tag)
    if child and len(child) >= 2 and isinstance(child[1], tuple):
        return child[1][1]
    return None


# --- KiCad netlist -----------------------------------------------------------


def parse_kicad_netlist(text: str) -> tuple[list, list]:
    """Parse KiCad ``.net`` text into ``(components, nets)``."""
    top = _parse_sexpr(_tokenize_sexpr(text))
    export = next((node for node in top if _head(node) == "export"), None)
    if export is None:
        export = top[0] if top else []

    comps_node = _find(export, "components") or []
    nets_node = _find(export, "nets") or []

    components: list[dict] = []
    for comp in _find_all(comps_node, "comp"):
        components.append(
            {
                "ref": _field(comp, "ref"),
                "value": _field(comp, "value"),
                "footprint": _field(comp, "footprint"),
                "pins": {},
            }
        )
    comp_index = {c["ref"]: c for c in components}

    nets = []
    for net in _find_all(nets_node, "net"):
        name = _field(net, "name")
        nodes = []
        for node in _find_all(net, "node"):
            ref = _field(node, "ref")
            pin = _field(node, "pin")
            entry = {"ref": ref, "pin": pin}
            port_pin = _field(node, "pinfunction")
            if port_pin:
                entry["port_pin"] = port_pin
            nodes.append(entry)
            if ref in comp_index and pin is not None:
                comp_index[ref]["pins"][pin] = name
        nets.append({"name": name, "nodes": nodes})

    return components, nets


# --- Dispatch ----------------------------------------------------------------


def detect_format(text: str) -> str:
    """Best-effort netlist format detection."""
    head = text.lstrip()[:256].lower()
    if head.startswith("(export") or "(netlist" in head or "(components" in head:
        return "kicad"
    return "unknown"


def parse_netlist(text: str, fmt: str = "auto", source: str = "<memory>") -> dict:
    """Parse netlist text into a normalized BoardDescription."""
    resolved = detect_format(text) if fmt in (None, "auto") else fmt.lower()
    if resolved == "kicad":
        components, nets = parse_kicad_netlist(text)
    else:
        raise ValueError(f"Unsupported or undetected netlist format: {resolved!r}. Supported: kicad.")
    return build_board_description(components, nets, source=source, fmt=resolved)


def load_netlist_file(path: str, fmt: str = "auto") -> dict:
    """Read a netlist file and parse it into a BoardDescription."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return parse_netlist(text, fmt=fmt, source=path)
