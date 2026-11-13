# Zabbix monitoring for AllStarLink app_rpt

Monitors an AllStarLink node host with Zabbix: IAX2 call quality, how many
nodes are connected, and whether a high Linux load average reflects real
stalls or is only an artefact of how load is counted.

A small collector script runs on the ASL host once a minute, asks Asterisk a
handful of CLI questions, and prints one JSON document. A Zabbix template
turns that document into items, triggers and graphs.

## Requirements

- AllStarLink 3 (Asterisk 20 or newer, running as the `asterisk` user)
- Zabbix agent 2 on the ASL host, running as the `zabbix` user
- Zabbix server 7.0 or newer
- `python3`, `sudo` and `make` on the ASL host

## Install

### 1. On the ASL host

Copy this directory to the host and run:

```bash
sudo make install
make check
```

`make install` puts three files in place and restarts `zabbix-agent2`:

| File | Purpose |
|---|---|
| `/usr/local/bin/asl-zabbix-stats` | The collector |
| `/etc/sudoers.d/zabbix-asl` | Lets `zabbix` run the collector, and nothing else, as `asterisk` |
| `/etc/zabbix/zabbix_agent2.d/asl.conf` | Defines the `asl.stats` agent key |

`make check` runs the collector the same way the agent does and prints the
JSON. Before going further, confirm:

- `asterisk_up` is `1` and `errors` is empty.
- `nodes` lists your local node numbers.
- `system.psi` is not empty (see [Pressure figures are empty](#pressure-figures-are-empty)).

### 2. On the Zabbix server

1. Go to **Data collection → Templates → Import** and import
   `template_asl_app_rpt.yaml`.
2. Open the ASL host and link the template
   **AllStarLink app_rpt by Zabbix agent 2**.
3. After a minute or two, check **Monitoring → Latest data** for the host.
   Node and link items appear once discovery has run.

## What you get

### Is the load average real?

Linux load counts tasks waiting for a CPU plus tasks in uninterruptible sleep,
sampled every five seconds. Asterisk with app_rpt runs many threads on 20 ms
timers, which can push that figure up without anything actually waiting.

The pressure items settle it. They report the share of wall time in which
tasks were genuinely stalled:

| Item | Meaning |
|---|---|
| CPU pressure (some) | Time at least one task waited for a CPU |
| I/O pressure (some) | Time at least one task waited on I/O |
| I/O pressure (full) | Time every active task waited on I/O at once |
| Memory pressure (some) | Time at least one task waited on memory reclaim |
| Collector run time | How long the CLI commands took; rises when the host is truly stalled |

The graph **ASL: Load average vs real stalls** puts load and pressure
together. High load with pressure near zero and clean call quality means the
load figure can be ignored on this host.

### IAX2 call quality

Taken from `iax2 show netstats`, as the worst value across all active calls:

- Round trip time (worst and average)
- Jitter, receive and transmit
- Packet loss, receive and transmit
- Jitter-buffer delay
- Number of active calls

**rx** is what this host receives, measured by its own jitter buffer.
**tx** is what the far end reports receiving from this host.

### Nodes and links

For each local node (found automatically from `rpt localnodes`):

- Direct links, and how many are keyed
- Links still connecting
- Connected nodes across the whole network, direct and indirect
- RX keyed and TX keyed

For each directly connected node: established state, reconnect count, round
trip time, jitter and packet loss.

## Triggers and tuning

Every threshold is a macro. Override them on the host under **Macros**.

| Macro | Default | Trigger |
|---|---|---|
| `{$ASL.IAX.JITTER.WARN}` | `80` | Worst-call jitter above this many ms for 5 minutes |
| `{$ASL.IAX.LOSS.WARN}` | `3` | Worst-call packet loss above this % for 5 minutes |
| `{$ASL.IAX.RTT.WARN}` | `300` | Worst-call round trip time above this many ms for 5 minutes |
| `{$ASL.PSI.CPU.WARN}` | `10` | CPU pressure above this % for 5 minutes |
| `{$ASL.PSI.IO.WARN}` | `10` | I/O pressure above this % for 5 minutes |
| `{$ASL.LINKS.MIN}` | `0` | A node has fewer direct links than this for 5 minutes |
| `{$ASL.LINK.RECONNECTS.WARN}` | `3` | One link reconnects this many times in 15 minutes |
| `{$ASL.TXKEYED.MAX}` | `15m` | A transmitter stays keyed this long |
| `{$ASL.LINK.MATCHES}` | `.*` | Not a threshold: which remote nodes get per-link items |

Two triggers have no macro: Asterisk not answering CLI commands for 3
minutes, and no data from the collector for 5 minutes.

Settings most hosts will want to change:

- **`{$ASL.LINKS.MIN}`** is `0`, which leaves the minimum-links trigger off.
  Set it to `1` or more on hubs and permanently linked nodes.
- **`{$ASL.LINK.MATCHES}`** creates seven items for every node that connects.
  On a hub with many short-lived connections, set it to a regular expression
  matching only your permanent links, for example `^(2000|2001)$`. Items for
  links that have gone away are removed after one day.
- **`{$ASL.TXKEYED.MAX}`** should be longer than your longest normal
  transmission, such as a net.

## Troubleshooting

### `make check` fails with a sudo error

The sudoers rule names the `zabbix` user and the exact path
`/usr/local/bin/asl-zabbix-stats`. If your agent runs as a different user or
you installed to a different `BINDIR`, edit `sudoers.d/zabbix-asl` and
`zabbix_agent2.d/asl.conf` to match and reinstall.

### `asterisk_up` is 0

The `errors` list in the JSON carries Asterisk's own message. The usual cause
is that Asterisk is not running, or the control socket is not where the
`asterisk` binary expects it.

### Pressure figures are empty

The kernel has pressure-stall information switched off, so `/proc/pressure/`
does not exist. Raspberry Pi kernels commonly ship this way. Add `psi=1` to
the kernel command line (`/boot/firmware/cmdline.txt` on Raspberry Pi OS) and
reboot. Until then the pressure items stay empty and their triggers do not
fire; everything else works.

### Receive jitter and loss always read 0

Asterisk only has receive-side figures when the IAX2 jitter buffer is in use.
The item **IAX2 calls reporting rx jitter** shows how many calls have them.
If it stays at 0 while calls are up, check `jitterbuffer` in
`/etc/asterisk/iax.conf`.

### Per-link items have no quality figures

Link quality is matched by comparing the peer address from `rpt lstats` with
the IAX2 channel name. Links that are not IAX2 (EchoLink, for example) have no
figures, and a channel named after a configured peer rather than an address
will not match. The host-wide worst-call items are unaffected.

### An item shows the wrong value, or a node or link is missing

The collector parses human-readable CLI output. To see what it is working
from, compare `make check` with:

```bash
sudo asterisk -rx "iax2 show netstats"
sudo asterisk -rx "rpt localnodes"
sudo asterisk -rx "rpt show variables <node>"
sudo asterisk -rx "rpt lstats <node>"
sudo asterisk -rx "rpt nodes <node>"
```

### The agent reports a timeout

The master item asks for a 20 second timeout, which Zabbix 7.0 agents honour.
A host with many local nodes runs three CLI commands per node; if that takes
longer, raise the timeout on the **ASL: Get stats** item.

## Uninstall

```bash
sudo make uninstall
```

Then unlink the template from the host in Zabbix, choosing **Unlink and
clear** to remove its items and history.

## Files

| File | Purpose |
|---|---|
| `asl_zabbix_stats.py` | Collector; installed as `asl-zabbix-stats` |
| `template_asl_app_rpt.yaml` | Zabbix 7.0 template to import |
| `zabbix_agent2.d/asl.conf` | Agent 2 `UserParameter` |
| `sudoers.d/zabbix-asl` | Sudo rule for the agent |
| `Makefile` | `install`, `check`, `uninstall`; honours `DESTDIR` for staged installs |
