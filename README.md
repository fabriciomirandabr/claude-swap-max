# Claude Swap Max

**Unlimited Claude Code.** For devs with several Claude accounts who want them to feel like one: it switches before the limit hits, and your Remote Control, phone, browser and Artifacts stay on the same account.

<img src="assets/tui-watch.png" width="760" alt="cswap watch — live 5h/7d usage bars for every account, with reset times and the active account marked">

You are in the middle of a long task and the 5-hour limit hits. With one account, you stop. With several, you log out, log in and lose the thread. Then the phone says "signed-in account or organization changed" and Chrome says "Browser extension is not connected".

Claude Swap Max fixes that. Many accounts, one experience: it watches every account and moves you to the one with room, while Remote Control, push notifications, Artifacts and Claude in Chrome stay on ONE account.

## What only Max has

- **One Remote Control, unlimited accounts.** Add as many subscriptions as you want. Sessions, push notifications and Artifacts stay on your main account, and inference bills whichever account has room.
- **Claude in Chrome on any account.** The extension stays signed in to one account and a session on any account commands it. Measured driving Chrome from another account: the pinned account's 5-hour window stayed at 0%.
- **A limit per account.** `cswap threshold 1 95` makes the rotation leave account 1 at 95% while another account has room, keeping a reserve for Desktop and mobile. `cswap standby 3` holds an account in reserve.
- **Balanced weekly pacing.** The `balance` strategy spreads usage across accounts so none burns ahead of its reset.
- **Desktop notifications** when the auto loop switches, quarantines an account, or runs out of room.

On top of everything upstream [`claude-swap`](https://github.com/realiti4/claude-swap) already does: automatic switching, a live usage dashboard, parallel sessions per account, backup and import.

## Install

```bash
uv tool install 'claude-swap[pin] @ git+https://github.com/fabriciomirandabr/claude-swap-max@main'
```

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12+. Same package name and same `cswap` command as upstream, and the same backup directory for your accounts. Update: the same command with `--force --no-cache`.

Use uv. The fixed proxy for Remote Control and Chrome is vendored in this repo, and only uv installs it. pipx and pip do not.

## Start in 3 steps

1. **Add your accounts.** Log into Claude Code with one account and run `cswap add`. For the next one, log in with it (no `/logout` first, it can revoke the token of the account you leave) and run `cswap add` again.
2. **Pin your main account.** `cswap list` shows the numbers. Then `cswap pin 1`: Remote Control, push, Artifacts and Chrome now live on account 1.
3. **Let it switch for you.** `cswap auto` in a spare terminal.

Optional:

```bash
cswap threshold 1 95                    # rotation leaves account 1 at 95%
cswap config set autoswitch.strategy balance
cswap config set autoswitch.notify true
```

## How it works

- `cswap auto` checks every account's 5-hour and 7-day usage and swaps the Claude login to the account with the most room before the limit, while Claude Code is running.
- The pin is a small local proxy. It answers Remote Control, Artifacts and the Chrome bridge as your pinned account. Your messages are never touched, so usage bills the account you swapped onto.
- Chrome: the pinned account only identifies the browser; the spend is the active account's.

## Good to know

- Chrome is proven with Claude Code 2.1.287. Anthropic does not document the browser bridge, so an update can change it.
- A Remote Control session that is already open stays on the account that created it. Reconnect inside it (`/rc`, Disconnect, `/rc`) to move it.
- Needs Claude Code logged in.

## Docs

Every command, setting, backup and JSON option is in [docs/reference.md](docs/reference.md). Design notes: [docs/design](docs/design).

## Credits and license

Fork of [realiti4/claude-swap](https://github.com/realiti4/claude-swap). The pin is [cswap-pin](https://github.com/codeslake/cswap-pin) by Junyong Lee (codeslake), MIT, vendored in `vendor/cswap-pin` with the Chrome patch. Per-account limits, `balance` and notifications come from upstream pull requests [#318](https://github.com/realiti4/claude-swap/pull/318), [#385](https://github.com/realiti4/claude-swap/pull/385) and [#285](https://github.com/realiti4/claude-swap/pull/285).

MIT
