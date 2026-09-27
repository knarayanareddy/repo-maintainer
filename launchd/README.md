# launchd job for daily curation

`com.antigravity.repo-maintainer.plist` is a **template**. It ships with
`__PLACEHOLDER__` markers instead of absolute paths so it can be read and
audited on any machine. `daily_runner.py` renders and installs it.

## Install

```bash
# Render, write to ~/Library/LaunchAgents, and load it. Defaults to 09:17 local.
python3 daily_runner.py --install-launchd

# Choose the local time the daily run should start.
python3 daily_runner.py --install-launchd --hour 7 --minute 30
```

`--install-launchd` is idempotent: it writes the plist, runs `launchctl
bootout` (ignoring "not loaded"), then `launchctl bootstrap` into the
`gui/$UID` domain, falling back to the older `launchctl load -w` on macOS
versions where `bootstrap` is unavailable.

## Verify without installing

```bash
python3 daily_runner.py --print-plist            # resolved XML on stdout
python3 daily_runner.py --print-plist | plutil -lint
python3 daily_runner.py --list                   # which repos would run
```

`plutil -lint` intentionally **fails** on the raw template in this directory:
`__HOUR__` and `__MINUTE__` sit inside `<integer>` elements. Lint the
*rendered* copy, as above.

## Trigger it immediately

launchd does not wait until tomorrow:

```bash
launchctl kickstart -k "gui/$(id -u)/com.antigravity.repo-maintainer"

# Follow the job's own output
tail -f logs/launchd.err.log logs/launchd.out.log

# Inspect the loaded job
launchctl print "gui/$(id -u)/com.antigravity.repo-maintainer" | head -40
```

## Uninstall

```bash
python3 daily_runner.py --uninstall-launchd
```

## Scheduling behaviour

* `StartCalendarInterval` fires once a day. launchd does **not** run a
  calendar job while the Mac is asleep; it coalesces the missed fire and
  starts the job the first time the machine wakes, so a laptop closed
  overnight still curates the next morning.
* `RunAtLoad` is `false`, so installing the agent never triggers an
  immediate five-repository run.
* The job is a per-user **LaunchAgent** (`~/Library/LaunchAgents`), not a
  system LaunchDaemon: it only runs while you are logged in, and it inherits
  your `PATH`, your `gh` credentials, and `~/.hermes/idea-dump/keys.env`.
* `ThrottleInterval` of 60 s stops launchd from hot-looping a job that fails
  instantly.

## Overlap protection

`daily_runner.py` takes an exclusive `flock` on `logs/daily-runner.lock`. If a
previous run is still going (a long `harvest_timeout` is easy to outlast a
calendar day), the new one logs the fact and exits `0` instead of doubling the
load on GitHub and the Gemini free tier. Use `--no-lock` only when debugging.

## Environment

The template pins a minimal, explicit `PATH` because launchd gives a job
almost none. If `git` or `gh` live elsewhere, add it to `EnvironmentVariables`
in the template and re-install. To pin a fine-grained token for the harvest
recipes instead of using the `gh` keyring, uncomment the `GITHUB_TOKEN` block
in the template.

Run summaries land in `reports/daily_run_<date>.{json,md}` and the full
transcript in `logs/daily_<date>.log`.
