# tvsign patches on top of the vendored Impactor

VENDORED.txt says "do not edit by hand": the tree is regenerated from the Impactor fork
(branch `tvsign`). For this release the changes below were made in place. Port every one of
them to the fork before the next `scripts/vendor-impactor.sh` run, otherwise they are lost.

Each entry names the files, what changed and why. Tests added with a change are listed.

## Certificates (plume_core)

`crates/plume_core/src/utils/certificate.rs`, `utils/mod.rs`, `lib.rs`, `Cargo.toml`

- New `CertificatePolicy` / `RevokeMode` (`never`, `own`, `any`) and
  `CertificateIdentity::new_with_session_policy`. `new_with_session` keeps its signature and its old
  behaviour (`any`) for the GUI.
- On error 7460 (account full) only certificates the policy allows are revoked, one at a time, each
  serial at most once, six attempts at most (dead ones do not count), in this order: dead ones
  (status Expired or expiry in the past; an unknown status is NOT dead; a certificate already
  Revoked is skipped, it frees nothing), then the ones created by this install (same machine name), then other installs of the same family, then
  foreign ones (Xcode, SideStore, AltStore). Default for plumesign is `own`: a foreign certificate is
  never revoked unless the user asks for `any`. With nothing left to revoke the error starts with
  `CERT_LIMIT:` and lists every certificate (name, machine, serial, status, expiry).
- Log lines: `Removed dead certificate ...`, `Revoked own certificate ...`, and
  `Revoked certificate (FOREIGN) ...` for another install or another app. Only the last one makes
  tvsign warn that other sideloaded apps will stop working.
- A certificate is reused only when its status is `Issued` and it has more than 30 days left. A
  certificate close to its end is replaced without revoking anything (the old one keeps working when
  the account has no free slot). A revoked or expired one is replaced.
- The certificate is found by the public key of `key.pem` (the machine name no longer has to match:
  certificates created by older tvsign versions are named `AltStore`).
- Never orphan a certificate: the new private key is written (atomically, 0600) to `key.pending.pem`
  before the CSR is sent, the listing after the CSR is retried five times, and `key.pem` is replaced
  atomically at the end. A run that finds a pending key whose certificate exists adopts it.
- `CertificateIdentity.expires_at` / `expires_on()`.
- Tests: ordering and filtering of revocation candidates, usability margins, atomic write, date
  formatting, limit error text.

`apps/plumesign/src/commands/sign.rs`

- Certificate machine name is `tvsign-<install id>` (id stored in `<data>/install_id`), or
  `$PLUMESIGN_MACHINE_NAME`. Family prefix `tvsign`.
- `--revoke never|own|any` / `$PLUMESIGN_REVOKE`, `--no-revoke` / `$PLUMESIGN_NO_REVOKE=1`.
- Prints `CERT <serial> <YYYY-MM-DD>` (certificate expiry) on stdout before signing.

## Temp files (plume_utils, plumesign)

`crates/plume_utils/src/package.rs`, `lib.rs`, `apps/plumesign/src/commands/sign.rs`

- `Package::stage_guard()` returns a `StageGuard` that removes `plume_stage_*` on drop, so every
  early return (`?`) of `sign` cleans up. `Package` itself stays `Clone` without `Drop` (the GUI
  keeps clones). `PLUME_DELETE_AFTER_FINISHED` still keeps the folder.
- `Package::new` removes its folder when the package cannot be opened.
- `sweep_stale_stages(max_age)` removes `plume_stage_*` older than one hour at the start of `sign`
  (process killed by a timeout never runs a destructor).
- Tests: guard removal, sweep age filter.

## Anisette (omnisette, plume_core)

`3rdparty/omnisette/src/remote_anisette_v3.rs`, `Cargo.toml`, new `src/apple_root.der`
(copy of `crates/plume_core/src/apple_root.der`)

- `http://` anisette servers use `ws://` for the provisioning websocket (`https://` still `wss://`),
  trailing `/` of the URL ignored.
- The provisioning loop returns an error when the connection is closed or fails (it used to spin at
  100% CPU), and the whole provisioning has a 60 s timeout. Every `unwrap` on server or Apple answers
  is an error now, `panic!("Unknown error")` too.
- Timeouts on all clients: connect 10 s, request 30 s.
- TLS is verified. Calls to Apple (gsa.apple.com lookup, start/finish provisioning) use the Apple
  root. The anisette server is verified with the system roots; a self-signed https home server needs
  `PLUMESIGN_ANISETTE_INSECURE=1`. (`adi_proxy.rs`, the local provider, is untouched.)
- Tests: websocket URL building.

`crates/plume_core/src/auth/anisette_data.rs`, `developer/session.rs`, `auth/account/login.rs`

- `AnisetteData::refresh_in_place`: three tries with a pause, then the previous data is kept instead
  of `unwrap()` (which aborted plumesign in the middle of a sign or a 2FA wait). After an abandoned
  refresh no new attempt is made for 30 s, so an outage does not add the retries to every Apple call. `generate_headers`
  no longer panics on old data; clock going backwards no longer panics.
- `crates/plume_core/src/lib.rs` `client()`: connect timeout 10 s, request timeout 60 s.
- `auth/account/mod.rs` `check_error`: no `unwrap` on a missing `ec`.

## Login (plume_core, plume_store)

- `auth/account/login.rs`: a wrong 2FA code is asked again (three tries) instead of ending the
  login; the code is reduced to its digits (`123 456`, `123-456`). The prompt line is printed again,
  so tvsign's session goes back to "waiting".
- `crates/plume_store/src/store.rs`: `accounts_add_from_session` fails with `NO_TEAM:` (new
  `plume_core::Error::NoTeam`) when the Apple ID has no developer team, instead of saving an account
  with an empty team and failing at the first renewal.
- Tests: code normalisation, bad code detection.

## Teams (plumesign)

`apps/plumesign/src/commands/account.rs`, `sign.rs`

- Team resolution order: `--team` / `-t`, `$PLUMESIGN_TEAM_ID`, the team stored in `accounts.json`
  (`team_id` of the account), the only team. An explicit or stored team that Apple no longer lists is
  an error `TEAM_NOT_FOUND:` (it used to fall through to `teams[0]` silently), except when the account
  has exactly one team left: it is used and stored in place of the vanished one. `account teams` marks
  no team when the choice would fail. With several teams and
  nothing chosen, without a terminal, the first team is used (as before) with a `MULTIPLE_TEAMS:`
  warning and is stored, so a later change of Apple's ordering cannot move the app to another team.
- New `account teams [--json]` and `account set-team <TEAM_ID>`. `sign` takes `--team`.
- Tests: `choose_team` precedence and errors.

## Wireless pairing (plume_utils, plumesign)

- `wireless/tunnel.rs`: `locate()` reports a lost pairing (`PairingNotAccepted`) when the wanted
  device never matched and either (a) a service with the identifier given on the command line, or with
  the identifier remembered in the `.info.plist` sidecar (`service_identifier`, written after every
  successful locate), shows a tag none of our keys produce, or (b) exactly ONE distinct identifier
  carries the wanted device's name and matches none of our keys (tvsign passes the pairing UDID, which
  differs from the advertised identifier, so a reset TV is only recognisable this way). Two or more
  distinct same-name identifiers (names are user-editable, "Apple TV" by default) stay a not-found with
  a hint. `open_tunnel` maps only `PairVerifyFailed` / `PairingRejected` to
  `PairingNotAccepted`; any other pair-verify error is `Unreachable` (retry later, exit 3). The tunnel
  address is built from the located address, so an IPv6 scope id is kept.
- `wireless/discovery.rs`: a 169.254/16 IPv4 address is only chosen when nothing else is advertised.
- `wireless/store.rs`: pairing file and info sidecar are written to a `.tmp` neighbour then renamed.
- `pair.rs`: `pair --ip` accepts `fe80::1%12` (numeric interface index) and `pair discover` prints
  the address that way (`ip` field of `--json`). An empty PIN, an EOF on stdin or no answer for 120 s
  ends with `ERROR CANCELLED ...` (exit 6) before anything is sent to the TV. `running` and
  `processes` match only the main executable `<bundle>/<CFBundleExecutable>`; extensions (Top Shelf,
  XPC) are reported with `kind: extension` and `parent_bundle_id` and do not count as the app. The
  os_trace `PidList` reply is checked (status byte, length <= 16 MiB, 30 s) and a bad reply is
  `UNSUPPORTED`.
- Tests: tunnel address scope, verify error mapping, link-local choice, atomic store, scoped ip
  parsing, extension matching, PIN normalisation.

## Misc

- `crates/plume_utils/src/cgbi.rs`: a CgBI PNG with a zero or huge dimension, or a huge inflated size,
  is left unchanged instead of dividing by zero or allocating without limit (the process aborts on
  panic). Tests with forged headers.
- `crates/plume_core/src/developer/mod.rs`: `sanitize_app_name` keeps the old letters-only name when
  it is not empty, otherwise falls back to letters and digits, then the last component of the
  identifier, then `App` (an empty App ID name was rejected by Apple). Used by `qh_add_app_id` and
  `qh_add_app_group`.
- `crates/plume_core/src/developer/v1/capabilities.rs`: no PATCH when there is no capability to
  enable.

## Not changed (see the group report)

- The capability list is still requested for the `IOS` platform and the free-account block list is
  unchanged (unverified against Apple).
- `WAITING` is still printed on any stall of `PercentComplete`.
- The IPA is still read into memory by `pair install`.
- `adi_proxy.rs` still accepts any TLS certificate (local provider, not used by tvsign).
