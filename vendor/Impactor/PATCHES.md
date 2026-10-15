# tvsign patches on top of the vendored Impactor

VENDORED.txt says "do not edit by hand": the tree is regenerated from the Impactor fork
(branch `tvsign`). The changes below were made in place for the 1.0.1 hotfix. Port every one of
them to the fork before the next `scripts/vendor-impactor.sh` run, otherwise they are lost.

Each entry names the files, what changed and why. Tests added with a change are listed.

## Why: Apple refuses Android anisette (October 2026)

Since 2026-10-12 Apple's GrandSlam (`gsa.apple.com`) refuses a sign-in whose anisette data was generated
by an Android-based anisette-v3 server: error `-80045` ("This action cannot be completed at this time") at
the SRP `init`, then (2026-10-14) an HTML `503 Service Temporarily Unavailable` page for client infos
like `<Pixel 7> <Android;...>`. What works is Windows anisette from a v3 server that serves it
(`https://ani.jkcoxson.com`, `https://ani2.stikstore.app`), sent with the client info AND the user agent
the server reports at `GET <server>/v3/client_info`. A client info rewritten from that
(`com.apple.iTunes` -> `com.apple.akd`), a different user agent (the fixed macOS
`akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0` used to be sent), or stale one-time passwords all get the 503 page.
Reference implementation (MIT): [isideload](https://github.com/nab138/isideload), commits `9111948`,
`ac8976d`, `fdb1788`, `d424b17`, `8ca39b6`; the protocol handling below follows it.

## Anisette servers (omnisette)

`3rdparty/omnisette/src/lib.rs`, `remote_anisette_v3.rs`, `anisette_headers_provider.rs`, `Cargo.toml`

- Default server `https://ani.jkcoxson.com` (was `https://ani.sidestore.app`), fallback
  `https://ani2.stikstore.app`. The fallback is used when the default server cannot be reached or does
  not answer properly (HTTP error, bad JSON, provisioning failure); a dead server costs its connect
  timeout once per process (the server that worked last is tried first).
- `resolve_servers()`: no URL requested -> default + fallback; the default asked explicitly -> same; a
  host in `ANDROID_ANISETTE_HOSTS` (`ani.sidestore.app`, `ani.stikstore.app`, `anisette.crystall1ne.dev`,
  `ani3server.fly.dev`, `ani.jaydenha.uk`, `omni.ethxn.xyz`) -> replaced by the default, with a warning;
  any other URL -> used alone, no fallback (a private server is never silently swapped for a public one).
  Environment: `PLUMESIGN_ANISETTE_URL` / `ANISETTE_URL_V3` (server),
  `PLUMESIGN_ANISETTE_FALLBACK_URL` (replaces the fallback; `none` or `off` removes it).
  `AnisetteConfiguration`: new `anisette_url_v3()`, `fallback_url_v3()`, `set_anisette_url_v3()`,
  `set_fallback_url_v3()`.
- The identity (device identifier + provisioning) lives in `anisette-state-windows.plist`. The Android-era
  `state.plist` is not read any more and is deleted once the new file is saved. The file is written
  atomically; an unreadable one (or an identifier of the wrong length, which used to panic) means a new
  identity; `adi_pb` is optional in the file (an unprovisioned identity round-trips).
  `omnisette::invalidate_provisioning()` forgets the provisioning and keeps the identifier.
- A server answer of `-45061` ("not provisioned") provisions the identity again, once, and retries.
  Other `GetHeadersError`s are an `AnisetteError::ServerError` (it used to be `panic!("Unknown error")`,
  which aborts the process in a release build). The status of the answer is not checked before reading
  its JSON, so an error status does not hide the `-45061` message.
- Apple's synchronization request: `get_headers` takes an optional `sim` (the `X-Apple-I-MD-DATA` of a
  GrandSlam answer with `hsc` 434) and sends `sim`, `device_id`, `md_lu` to the server. New trait methods
  of `AnisetteHeadersProvider`, all with defaults: `user_agent()` (the `user_agent` of `/v3/client_info`,
  next to the client info that was already returned), `identity_is_new()`, `resync_anisette_headers()`.
- Provisioning: the loop used to spin at 100% CPU when the server closed the connection (`continue` on
  a `None`); it returns an error now, with a 60 s timeout for the whole session (tokio `time` feature).
  New server messages `Timeout`, `InvalidIdentifier`, `StartProvisioningError`, `EndProvisioningError`.
  Apple's answers to the lookup / start / finish requests are checked (an HTML 503 page ->
  `AnisetteError::AppleRefused`, `Response.Status.ec != 0` -> `ProvisioningFailed`) instead of `unwrap`.
  `http://` servers use `ws://` for the websocket. Timeouts on all clients: connect 10 s, request 30 s.
  The URL bag lookup keeps sending the client info of the server (checked: with the Windows one Apple
  returns the `midStartProvisioning` / `midFinishProvisioning` URLs; isideload sends a Mac client info
  there because Android ones got a wrong bag).
- A server whose client info does not name Windows gets a warning (Apple refuses what is not Windows).
- Errors: `ServerError`, `ProvisioningFailed`, `AppleRefused { status, message }`, `ResyncUnsupported`,
  `AllServersFailed`.
- Tests: server resolution (hosts, lookalikes, fallback override), server order, websocket URL, state
  round trip / legacy removal / unreadable state, Apple error handling, Windows detection. They need
  the feature set of `plumesign`: `cargo test -p omnisette -p plumesign -- --skip fetch_anisette_remote_v3
  --skip fetch_anisette_ssc_async` (the two skipped tests use the network / Android libraries).

## Sign-in (plume_core, plumesign)

`crates/plume_core/src/auth/anisette_data.rs`, `auth/mod.rs`, `auth/account/{mod,login,token,two_factor_auth}.rs`,
`developer/session.rs`, `lib.rs`, `Cargo.toml`; `apps/plumesign/src/commands/account.rs`

- Headers: GrandSlam, 2FA and developer-portal requests carry the client info of the anisette server
  unchanged and its user agent (`AnisetteData::user_agent`, `grandslam_headers()`), `X-Xcode-Version`
  `27.0 (27A5218g)`, `X-Apple-App-Info`, `Accept: text/x-xml-plist`, `Connection: close` (the fixed
  macOS user agent, `Xcode` as developer-portal user agent and the `Accept: */*` are gone). Only a
  client identifier GrandSlam blocks (`com.apple.dt.Xcode`) is replaced by `com.apple.akd/1.0`; the
  Windows `com.apple.iTunes/...` is never rewritten. `get_header()` no longer lower-cases values.
- hsc 434: when the first `init` answer is `Status.hsc == 434` with an `X-Apple-I-MD-DATA`, the data is
  relayed to the anisette server (`AnisetteData::resync`), the new data replace the stored ones and the
  `init` is sent again with the same SRP ephemeral. Once. A fresh anisette identity gets this answer
  on its first request, and Apple remembers the synchronization afterwards. A renewal (stored session, no sign-in) used to have nothing that
  synchronizes a fresh identity: `DeveloperSession::new` asks once, with a throwaway Apple ID
  (`tvsign-sync-<random>@example.invalid`, no account is touched), and synchronizes
  (`AnisetteData::synchronize_with_apple`). Best effort.
- `-45061` from GrandSlam at `init`: the provisioning is forgotten and made again, once.
- 2FA: `Status.hsc == 409` without `au` means the code was already pushed to the trusted devices: the
  next state is the entry of the code (`state_after_complete`), no second push, no loop (at most 15 steps
  and 4 prompts for a code). HTTP 409 on the push request counts as "already sent". A code typed as
  `123 456` is reduced to its characters.
- SMS: HTTP 403 ("Access to this Apple Developer Service was denied") from the SMS endpoints is
  `Error::SmsRefused`. Asked from the device prompt, it does not end the sign-in: the code is already on
  the trusted devices, the prompt comes back as `Enter 2FA code from a trusted Apple device (SMS refused
  by Apple):` without the SMS offer (`TwoFactorRequest::sms_refused`, `trusted_phone_numbers` empty).
  For an account that has no trusted device it is the end of the sign-in, with a clear message.
- Answers that are not a plist (the HTML 503 page, an empty 401/403) are `Error::GsaHttp { status, body }`
  with the title of the page, not a `Plist error` and not a panic (`parse_response`, `verify_2fa`).
  HTTP 429/502/503/504 from GrandSlam are tried again up to three times (2 s, 4 s, 6 s, or
  `Retry-After`): the edge sheds load in bursts, and the shared anisette machines of the public servers
  get 503 on perfectly good requests now and then.
- No more panics on Apple's or the anisette server's answers: `check_error` without `ec`/`em`,
  `get_anisette()` (returns a `Result`; a failing refresh used to `unwrap` in the middle of a sign-in),
  `generate_headers` on old data (a warning), clock going backwards, missing SRP fields and session
  data (`Error::Parse`), SRP failures.
- The password follows the SRP protocol Apple selects in its `init` answer (`sp`): `s2k` the SHA-256 of
  the password as before, `s2k_fo` its lower-case hex text (`password_for_protocol`). Both were offered
  in the request, but only `s2k` was ever computed.
- `Cargo.toml` of `plume_core`: `tweaks` is a default feature (`lib.rs` and `utils/provision.rs` use
  `MachO` without it, so `cargo test -p plume_core` did not compile alone; every dependent already
  enables it); `tokio` asks for its `sync` and `time` features explicitly.
- A seam for the tests: GrandSlam URLs are built by `auth::gsa_url(path)`; under `cfg(test)` a
  thread-local can point them at a local server. Nothing changes in a normal build.
- Tests (`cargo test -p plume_core`): header pairing, client info untouched / Xcode replaced, header
  case, hsc 434 / 409 decisions, sim as string or data, HTML 503 / odd bodies, retry policy against a
  local server, SMS 403, SRP protocols, code normalisation; and `auth/account/flow_tests.rs`, the whole
  `Account::login_with_anisette` against local imitations of GrandSlam (real SRP, encrypted SPD, 2FA
  endpoints) and of an anisette server: code already pushed (no second push), synchronization and the use
  of the new one-time password by the following requests, headers of every request, SMS refused (back to
  the device prompt / clear error for an SMS-only account), shed requests retried, 503 reported after the
  retries, `-80045` reported and never repeated, `s2k` / `s2k_fo`, wrong password, wrong code, an Apple
  that keeps asking for codes.

## What tvsign parses

`app/plume.py` classifies the error lines of plumesign. The texts that matter, by Rust `Display`:
`Authentication SRP error -80045: ...`, `Apple GrandSlam answered HTTP 503 without a usable reply: ...`,
`Apple answered HTTP 503 to the anisette provisioning: ...`, `SMS 2FA refused by Apple (HTTP 403: ...)`,
`Authentication SRP error -22411: ...`, `-22421` / `-22409` / `-80009`, `Anisette not provisioned!`,
`Anisette error: ...`, and the prompt `Enter 2FA code from a trusted Apple device (SMS refused by Apple):`.
Changing one of these strings means changing `tests/test_hotfix.py` and `web/app.js` (`CAUSES`).
