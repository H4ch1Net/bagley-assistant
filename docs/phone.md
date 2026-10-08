# Bagley on the phone

The phone (here a Samsung Galaxy S25 Ultra) reaches Bagley over Tailscale, installs the web UI as
an app from Chrome, and gets notifications through ntfy. Nothing is exposed to the internet.
Set this up on the machine that is always on, usually the runner ([runner.md](runner.md)).

## Requirements

- Tailscale on the phone (Play Store) and on the Bagley machine, same tailnet.
- MagicDNS and HTTPS certificates turned on for the tailnet: Tailscale admin console > DNS.
  Certificates are issued by Let's Encrypt, so the machine name (`surface.tail1234.ts.net`)
  appears in public certificate transparency logs; the address is still only reachable from
  your tailnet.
- Chrome on the phone (Samsung Internet works too).
- The ntfy app (Play Store or F-Droid) for notifications.

## 1. HTTPS on the tailnet

On the Bagley machine:

```sh
bagley tailscale
```

```
TAILNET      tail1234.ts.net  RUNNING
NODE         SURFACE  surface.tail1234.ts.net  100.101.102.103
USER         you@example.com

» PEERS
  B1T                LINUX     100.64.0.2       OFFLINE
  H4CH1              WINDOWS   100.64.0.3       ONLINE
  S25-ULTRA          ANDROID   100.64.0.4       ONLINE
```

and the steps that follow it:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8765
bagley tailscale --apply
systemctl --user restart bagley
```

`tailscale serve` publishes Bagley's loopback port at `https://surface.tail1234.ts.net` to your
tailnet only, with a valid certificate. If it says access denied, run
`sudo tailscale set --operator=$USER` once. Don't use Tailscale Funnel for Bagley: Funnel puts
it on the public internet.

`--apply` writes two lines to `~/.config/bagley/env`:

- `BAGLEY_ALLOWED_HOSTS=surface.tail1234.ts.net`: Bagley refuses requests for host names it
  doesn't know, which stops DNS rebinding attacks from web pages. The tailnet name has to be on
  the list. Hosts already there are kept.
- `BAGLEY_PUBLIC_URL=https://surface.tail1234.ts.net`: notification links open this address, so
  tapping one on the phone opens the right chat.

## 2. Who gets in

Anyone who can reach `https://surface.tail1234.ts.net` can use Bagley unless you limit it. On a
tailnet you share, or for defense in depth, use one or both of these.

### Tailscale identity check

```sh
bagley env set BAGLEY_TAILSCALE_USERS=you@example.com    # comma separated, any case
systemctl --user restart bagley
```

Requests for the tailnet name then need a `Tailscale-User-Login` header with one of those
logins, or Bagley's token. Requests for `127.0.0.1` and `localhost` are not checked.

Why trusting a header is safe here:

- `tailscale serve` connects to Bagley from this machine (127.0.0.1) and sets
  `Tailscale-User-Login` from the caller's tailnet identity, which Tailscale has authenticated.
  It replaces any value the caller sent, so a phone or browser can't pick its own login.
- Bagley listens on 127.0.0.1 only. Nobody on the tailnet or the LAN can connect to that port
  to send a forged header; the only way in from outside is through `tailscale serve`.
- Programs on the machine itself could send the header, but they can already reach Bagley at
  `http://127.0.0.1:8765`, which was never checked.
- If you make Bagley listen beyond loopback (`--host 0.0.0.0`), anyone who reaches the port
  could send the header, so Bagley always requires its token in that case.

Tagged devices and Funnel requests carry no user login; give those the token.

### Token

```sh
bagley env set BAGLEY_TOKEN=-       # paste a long random value
```

Open `https://surface.tail1234.ts.net/?token=<token>` once on the phone; Bagley swaps it for a
cookie that lasts a year. Command line clients send it from `BAGLEY_TOKEN`; a laptop moving
automations to the runner sends it from its runner token setting.

## 3. Install the app

On the S25 Ultra, open `https://surface.tail1234.ts.net` in Chrome, then menu (⋮) >
**Add to Home screen** > **Install** (Chrome may offer **Install app** directly). In Samsung
Internet: menu > **Add page to** > **Home screen**.

- It opens full screen, without the address bar, in Bagley's colours.
- Long-press the icon for the **New chat** and **Approvals** shortcuts.
- Tapping a Bagley notification link opens the installed app.
- Updates arrive by themselves: the app checks the server first every time it starts.
- If Tailscale is off or the runner is down, the app shows a `NO SIGNAL` page and retries every
  15 seconds. Nothing from the API is cached on the phone.

Installing needs HTTPS, which is why the app is installed from the `ts.net` address and not from
`http://100.x.y.z:8765`.

## 4. Notifications with ntfy

Bagley sends phone notifications to an ntfy topic. Pick one of these:

### ntfy.sh with a private topic

The simplest. Make up a topic nobody can guess:

```sh
python -c "import secrets; print('bagley-' + secrets.token_hex(8))"
```

In the ntfy app, subscribe to that topic on `ntfy.sh`. In Bagley, Settings > Notifications, set
the ntfy URL to `https://ntfy.sh/bagley-…`. Anyone who knows the topic can read the messages and
they pass through ntfy.sh, so treat the topic like a password, or self-host.

### Self-hosted ntfy on the tailnet

Run ntfy on the runner (package, binary or `docker run -p 127.0.0.1:2586:80 binwiederhier/ntfy serve`)
and publish it to the tailnet on another port:

```sh
tailscale serve --bg --https=8443 http://127.0.0.1:2586
```

In the ntfy app add the server `https://surface.tail1234.ts.net:8443` and subscribe to `bagley`.
In Bagley set the ntfy URL to `https://surface.tail1234.ts.net:8443/bagley`, and the ntfy token
if you turned on ntfy's access control. A self-hosted server reaches the phone through the app's
own connection (instant delivery), so let ntfy run in the background: Settings > Apps > ntfy >
Battery > **Unrestricted**, and keep it out of Samsung's sleeping apps.

### Levels

Every notification has a level, and the **ntfy level** setting picks the lowest one that reaches
the phone:

| Level | Sent as ntfy priority | Used for |
| --- | --- | --- |
| info | default | Reminders, automation results, replies |
| important | high | Failed automations, anything a feature marks important |
| critical | urgent | Things that need you now |

With the default setting (`important`), only important and critical notifications buzz the
phone; choose `all` to get every reminder there too.

### Test it

```sh
bagley notify test            # desktop and phone
bagley notify test --phone
```

```
LEVEL        IMPORTANT
WINDOWS      001
PHONE        [OK]   Sent to ntfy as important.
```

The web UI has the same test (`POST /api/notify/test`).
