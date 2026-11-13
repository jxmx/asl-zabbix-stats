#!/usr/bin/env python3
"""Collect AllStarLink app_rpt / IAX2 / system-pressure stats as one JSON blob.

Intended to be run once a minute by Zabbix agent 2 (as the asterisk user, via
sudo) and consumed by a single master item with dependent items.

Always prints valid JSON and exits 0; failures are reported inside the JSON
("asterisk_up": 0 and the "errors" list) so the template can alert on them.
"""

import json
import re
import subprocess
import time

ASTERISK = "/usr/sbin/asterisk"
CMD_TIMEOUT = 5  # seconds per CLI command

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def cli(command):
    """Run one Asterisk CLI command and return its output lines."""
    proc = subprocess.run(
        [ASTERISK, "-rx", command],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=CMD_TIMEOUT,
    )
    if proc.returncode != 0:
        msg = (proc.stdout + proc.stderr).strip().splitlines()
        raise RuntimeError(msg[0] if msg else "asterisk -rx exited %d" % proc.returncode)
    return [ANSI.sub("", line).rstrip() for line in proc.stdout.splitlines()]


# --------------------------------------------------------------------------
# Parsers (pure functions over CLI output lines)
# --------------------------------------------------------------------------

def parse_localnodes(lines):
    """`rpt localnodes` -> ['1999', '2000']"""
    return [line.strip() for line in lines if re.fullmatch(r"\s*\d+\s*", line)]


def parse_variables(lines):
    """`rpt show variables <node>` -> {'RPT_NUMLINKS': '2', ...}"""
    out = {}
    for line in lines:
        m = re.match(r"\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_connected_nodes(lines):
    """`rpt nodes <node>` -> every connected node, direct and indirect."""
    nodes = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("*") or "<NONE>" in line:
            continue
        for tok in line.split(","):
            tok = tok.strip()
            if re.fullmatch(r"[A-Za-z0-9_-]+", tok):
                nodes.append(tok)
    return nodes


def parse_connect_time(text):
    """'01:02:03' (optionally with trailing fields) -> seconds, or None."""
    try:
        parts = [int(p) for p in re.split(r"[:.]", text)[:3]]
    except ValueError:
        return None
    if len(parts) != 3:
        return None
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_lstats(lines):
    """`rpt lstats <node>` -> list of directly connected links.

    Columns: NODE PEER RECONNECTS DIRECTION CONNECT-TIME CONNECT-STATE
    Parsed from the right so an odd PEER value cannot shift the fields.
    """
    links = []
    for line in lines:
        tok = line.split()
        if len(tok) < 5 or tok[0] in ("NODE", "----"):
            continue
        state, ctime, direction, reconnects = tok[-1], tok[-2], tok[-3], tok[-4]
        if direction not in ("IN", "OUT") or not reconnects.isdigit():
            continue
        link = {
            "node": tok[0],
            "peer": " ".join(tok[1:-4]),
            "direction": direction,
            "reconnects": int(reconnects),
            "established": 1 if state.upper() == "ESTABLISHED" else 0,
        }
        secs = parse_connect_time(ctime)
        if secs is not None:
            link["connect_seconds"] = secs
        links.append(link)
    return links


NETSTAT_FIELDS = (
    "rtt",
    "rx_jitter", "rx_delay", "rx_lost", "rx_loss_pct", "rx_dropped", "rx_ooo", "rx_kpkts",
    "tx_jitter", "tx_delay", "tx_lost", "tx_loss_pct", "tx_dropped", "tx_ooo", "tx_kpkts",
)


def parse_netstats(lines):
    """`iax2 show netstats` -> one dict per active IAX2 call.

    rx_* is the LOCAL block (what we receive, from our jitter buffer);
    tx_* is the REMOTE block (what the far end reports receiving from us).
    Asterisk prints -1 where it has no data; those fields are omitted.
    """
    chans = []
    for line in lines:
        tok = line.split()
        if len(tok) < 16:
            continue
        try:
            nums = [int(t) for t in tok[1:16]]
        except ValueError:
            continue
        chan = {"channel": tok[0]}
        for name, val in zip(NETSTAT_FIELDS, nums):
            if val >= 0:
                chan[name] = val
        chans.append(chan)
    return chans


def channel_matches_peer(channel, peer):
    """True if an IAX2 channel name (IAX2/<host>[:port][-callno]) is for peer."""
    if not peer or not channel.startswith("IAX2/"):
        return False
    rest = channel[5:]
    return rest == peer or rest.startswith(peer + ":") or rest.startswith(peer + "-")


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def worst(chans, field):
    vals = [c[field] for c in chans if field in c]
    return max(vals) if vals else 0


def summarize_iax(chans):
    rtts = [c["rtt"] for c in chans if "rtt" in c]
    return {
        "channels": len(chans),
        "rx_reporting": sum(1 for c in chans if "rx_jitter" in c),
        "rtt_max": max(rtts) if rtts else 0,
        "rtt_avg": round(sum(rtts) / len(rtts), 1) if rtts else 0,
        "rx_jitter_max": worst(chans, "rx_jitter"),
        "tx_jitter_max": worst(chans, "tx_jitter"),
        "rx_delay_max": worst(chans, "rx_delay"),
        "tx_delay_max": worst(chans, "tx_delay"),
        "rx_loss_pct_max": worst(chans, "rx_loss_pct"),
        "tx_loss_pct_max": worst(chans, "tx_loss_pct"),
    }


QUALITY_FIELDS = ("rtt", "rx_jitter", "tx_jitter", "rx_delay", "tx_delay",
                  "rx_loss_pct", "tx_loss_pct")


def attach_quality(link, chans):
    """Copy the worst matching IAX2 call's figures onto a link (matched by IP)."""
    matched = [c for c in chans if channel_matches_peer(c["channel"], link["peer"])]
    for field in QUALITY_FIELDS:
        vals = [c[field] for c in matched if field in c]
        if vals:
            link[field] = max(vals)


def int_or(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def keyed_links(alinks):
    """RPT_ALINKS is '<count>,<node><mode><K|U>,...'; count the keyed ones."""
    return sum(1 for entry in alinks.split(",")[1:] if entry.strip().endswith("K"))


# --------------------------------------------------------------------------
# System side: is the load average real?
# --------------------------------------------------------------------------

def read_system():
    sysinfo = {}
    try:
        with open("/proc/loadavg") as fh:
            parts = fh.read().split()
        sysinfo["load1"], sysinfo["load5"], sysinfo["load15"] = (float(p) for p in parts[:3])
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("procs_running "):
                    sysinfo["procs_running"] = int(line.split()[1])
                elif line.startswith("procs_blocked "):
                    sysinfo["procs_blocked"] = int(line.split()[1])
    except (OSError, ValueError):
        pass
    # Pressure stall totals are cumulative microseconds of stalled time.
    # Absent keys (PSI disabled) are simply discarded by the template.
    psi = {}
    for resource in ("cpu", "io", "memory"):
        try:
            with open("/proc/pressure/" + resource) as fh:
                for line in fh:
                    m = re.match(r"(some|full)\s.*\btotal=(\d+)", line)
                    if m:
                        psi["%s_%s_total" % (resource, m.group(1))] = int(m.group(2))
        except OSError:
            pass
    sysinfo["psi"] = psi
    return sysinfo


# --------------------------------------------------------------------------

def collect():
    started = time.monotonic()
    data = {
        "asterisk_up": 0,
        "errors": [],
        "system": read_system(),
        "iax": summarize_iax([]),
        "nodes": [],
        "links": [],
    }

    def run(command):
        try:
            return cli(command)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            data["errors"].append("%s: %s" % (command, exc))
            return None

    chans_lines = run("iax2 show netstats")
    if chans_lines is not None:
        data["asterisk_up"] = 1
        chans = parse_netstats(chans_lines)
        data["iax"] = summarize_iax(chans)

        for node in parse_localnodes(run("rpt localnodes") or []):
            variables = parse_variables(run("rpt show variables " + node) or [])
            links = parse_lstats(run("rpt lstats " + node) or [])
            connected = parse_connected_nodes(run("rpt nodes " + node) or [])

            data["nodes"].append({
                "node": node,
                "numlinks": int_or(variables.get("RPT_NUMLINKS"), len(links)),
                "links_keyed": keyed_links(variables.get("RPT_ALINKS", "")),
                "links_connecting": sum(1 for l in links if not l["established"]),
                "connected_total": len(connected),
                "rxkeyed": int_or(variables.get("RPT_RXKEYED")),
                "txkeyed": int_or(variables.get("RPT_TXKEYED")),
            })
            for link in links:
                link["local"] = node
                link["id"] = "%s-%s" % (node, link["node"])
                attach_quality(link, chans)
                data["links"].append(link)

    data["collect_ms"] = int((time.monotonic() - started) * 1000)
    return data


if __name__ == "__main__":
    print(json.dumps(collect(), separators=(",", ":")))
