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
  `https://ani2.stikstore.app`. The fallback is used when the default server cannot be reached, does
  not answer properly (HTTP error, bad JSON, provisioning failure) or does not serve Windows anisette.
- `resolve_servers()`: no URL requested -> default + fallback; the default asked explicitly -> same; a
  host in `ANDROID_ANISETTE_HOSTS` (`ani.sidestore.app`, `ani.stikstore.app`, `anisette.crystall1ne.dev`,
  `ani3server.fly.dev`, `ani.jaydenha.uk`, `omni.ethxn.xyz`) -> replaced by the default, with a warning;
  any other URL -> used alone, no fallback (a private server is never silently swapped for a public one).
  Environment: `PLUMESIGN_ANISETTE_URL` / `ANISETTE_URL_V3` (server),
  `PLUMESIGN_ANISETTE_FALLBACK_URL` (replaces the fallback; `none` or `off` removes it).
  `AnisetteConfiguration`: new `anisette_url_v3()`, `fallback_url_v3()`, `set_anisette_url_v3()`,
  `set_fallback_url_v3()`.
- The server that served the last data of a sign-in is tried first by the next refresh (a dead default
  server costs its connect timeout once per sign-in, not at every refresh). It lives in the configuration
  that travels with the data (`AnisetteConfiguration::preferred_url_v3()`, `servers_v3()`, the free function
  `server_order()`), not in a process-wide variable; `prefer_other_server_v3()` puts the other server first
  (see "Sign-in"). New trait method `AnisetteHeadersProvider::server_used()`.
- A server whose client info does not name Windows is not used: Apple answers `-80045` or a 503 page to
  anything else (a Mac client info over Android data included, which is what `ani.sidestore.app` announces
  now), and every such request counts against the account and the connection. The error
  `AnisetteError::UnsupportedServer` ("... Apple only accepts Windows anisette since 2026-10-12 ...") makes the
  fallback take over, or is the clear error of a server used alone. Nothing is sent to Apple. The
  environment variable `PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM=1` lets such a server through (for the day Apple
  accepts more than Windows); `RemoteAnisetteProviderV3::allow_any_platform()` does it for the diagnostic
  command below.
- The identity (device identifier + provisioning) lives in `anisette-state-windows.plist`. The Android-era
  `state.plist` is not read any more and is deleted once the new file is saved. The file is written
  atomically; an unreadable one (or an identifier of the wrong length, which used to panic) means a new
  identity; `adi_pb` is optional in the file (an unprovisioned identity round-trips).
- A server answer of `-45061` ("not provisioned") makes a NEW identity (new random device identifier, as
  isideload does with `force_reset`) and provisions it, once, then asks again for the headers. The
  synchronization data (`sim`) of Apple belonged to the old identity, so the second request goes without
  them; GrandSlam asks the new identity to synchronize by itself. `omnisette::reset_identity()` (used by
  plume_core when GrandSlam itself answers `-45061`) removes the state file; it used to keep the identifier
  (`invalidate_provisioning`), which fails for ever with a server that refuses to provision that identifier
  again. Other `GetHeadersError`s are an `AnisetteError::ServerError` (it used to be `panic!("Unknown error")`,
  which aborts the process in a release build). The status of the answer is not checked before reading
  its JSON, so an error status does not hide the `-45061` message.
- Apple's synchronization request: `get_headers` takes an optional `sim` (the `X-Apple-I-MD-DATA` of a
  GrandSlam answer with `hsc` 434) and sends `sim`, `device_id`, `md_lu` to the server. New trait methods
  of `AnisetteHeadersProvider`, all with defaults: `user_agent()` (the `user_agent` of `/v3/client_info`,
  next to the client info that was already returned), `identity_is_new()`, `resync_anisette_headers()`,
  `server_used()`.
- Provisioning: the loop used to spin at 100% CPU when the server closed the connection (`continue` on
  a `None`); it returns an error now, with a 60 s timeout for the whole session (tokio `time` feature).
  New server messages `Timeout`, `InvalidIdentifier`, `StartProvisioningError`, `EndProvisioningError`.
  Apple's answers to the lookup / start / finish requests are checked (an HTML 503 page ->
  `AnisetteError::AppleRefused`, `Response.Status.ec != 0` -> `ProvisioningFailed`) instead of `unwrap`.
  `http://` servers use `ws://` for the websocket. Timeouts on all clients: connect 10 s, request 30 s.
- The requests to Apple's provisioning service (URL bag lookup, start, finish) carry the same set of
  headers a GrandSlam request does, as isideload sends them: client info and user agent of the server
  as a pair, `Accept: text/x-xml-plist`, `X-Xcode-Version`, `X-Apple-App-Info`, `Connection: close`, plus the
  identity (`X-Apple-I-MD-LU`, `X-Mme-Device-Id`, client time, time zone, locale). The URL bag is asked
  with the server's own client info first; when the bag lacks `midStartProvisioning` /
  `midFinishProvisioning` (or Apple answers an HTML refusal), it is asked once more with a Mac client info,
  which is what isideload always sends for this one request (an Android client info got a wrong bag). The
  start and finish requests keep the server's client info.
- Errors: `ServerError`, `ProvisioningFailed`, `AppleRefused { status, message }`, `UnsupportedServer`,
  `ResyncUnsupported`, `AllServersFailed`.
- A test seam: `remote_anisette_v3::set_lookup_url_for_tests()` (hidden) points the URL bag lookup of the
  calling thread at a local imitation of Apple, so that the sign-in tests of plume_core can provision an
  identity without Apple. Nothing sets it in a normal run.
- Tests: server resolution (hosts, lookalikes, fallback override), server order and preference, websocket URL,
  state round trip / legacy removal / identity reset / unreadable state, Apple error handling, Windows
  detection; against a local imitation of the anisette server (HTTP and the provisioning socket) and of Apple's
  provisioning service: the headers of the three provisioning requests, the second URL bag lookup, `-45061`
  (new identity, no `sim`, one provisioning and no loop), a server that is not Windows (not used, nothing sent
  to Apple, fallback, override), preferred server. They need the feature set of `plumesign`:
  `cargo test -p omnisette -p plumesign -- --skip fetch_anisette_remote_v3 --skip fetch_anisette_ssc_async`
  (the two skipped tests use the network / Android libraries).

## Sign-in (plume_core, plumesign)

`crates/plume_core/src/auth/anisette_data.rs`, `auth/mod.rs`, `auth/account/{mod,login,token,two_factor_auth}.rs`,
`developer/{mod,session}.rs`, `lib.rs`, `Cargo.toml`; `apps/plumesign/src/commands/account.rs`

- Headers: GrandSlam, 2FA and developer-portal requests carry the client info of the anisette server
  unchanged and its user agent (`AnisetteData::user_agent`, `grandslam_headers()`), `X-Xcode-Version`
  `27.0 (27A5218g)`, `X-Apple-App-Info`, `Accept: text/x-xml-plist`, `Connection: close` (the fixed
  macOS user agent, `Xcode` as developer-portal user agent and the `Accept: */*` are gone). Only a
  client identifier GrandSlam blocks (`com.apple.dt.Xcode`) is replaced by `com.apple.akd/1.0`; the
  Windows `com.apple.iTunes/...` is never rewritten. `get_header()` no longer lower-cases values.
  `XCODE_VERSION` is the one of omnisette (the provisioning requests send it too).
- The `init` step is run by a small plan (`InitPlan`, `login.rs`) so that no answer of Apple can make the
  sign-in loop: at most 4 `init` requests, and each correction once per anisette identity.
  - hsc 434: when the answer is `Status.hsc == 434` with an `X-Apple-I-MD-DATA`, the data is relayed to
    the anisette server (`AnisetteData::resync`), the new data replace the stored ones and the `init` is sent
    again with the same SRP ephemeral. A fresh anisette identity gets this answer on its first request, and
    Apple remembers the synchronization afterwards (checked: an identity synchronized by a previous process is
    not asked again).
  - `-45061` from GrandSlam at `init`: the identity is forgotten and a new one made and provisioned
    (`reprovision_anisette`), once. A new identity has never been seen by Apple: it is asked to synchronize in
    its turn, which the first version of the hotfix did not allow (the second 434 became `-22421` and a reset
    of the identity by tvsign). The same holds when a resync made the server lose and recreate the identity
    (`AnisetteData::fresh_identity` of the new data).
  - an HTML 503 that lasts (after the retries below): the anisette server may be one Apple refuses data
    from. The other configured server is put first (`AnisetteConfiguration::prefer_other_server_v3`), fresh
    data are fetched from it (`switch_anisette_server`) and the `init` is sent again, once. No other server
    (a server chosen by the user is used alone): the 503 is reported. `-80045` is never repeated, and
    nothing switches servers for it: it depends on the account and the connection, not on the data.
- Retries of GrandSlam requests that were shed (`send_with_retry`): HTTP 429 up to three times (2 s, 4 s,
  6 s, or `Retry-After`, 30 s at most), HTTP 502/503/504 once (3 s). A 503 that is a refusal of the data gives
  the same page every time, so more tries would only be more requests to Apple (they were three before); the
  edge also sheds good requests now and then, which one more try absorbs.
- A renewal (stored session, no sign-in) opens a developer session with `DeveloperSession::new`. A new
  anisette identity has never been seen by Apple, and nothing synchronizes it, so the portal may refuse its
  data. When the first request fails for such an identity (`fresh_identity`; anything but a network error),
  the identity is synchronized once through GrandSlam (`AnisetteData::synchronize_with_apple`: an `init` for
  a throwaway Apple ID `tvsign-sync-<random>@example.invalid`, no account is touched; `None` when Apple does
  not ask for a synchronization) and the request is sent again with the synchronized data. The first version
  did this before every first request of a new identity; it is only done when needed now, so a migrated
  install whose portal accepts the data costs Apple no extra request. Best effort: the first error is
  reported when the synchronization does not happen, and the identity is not fresh in the next process, so a
  failed synchronization is not retried (the account then shows as signed out, and a sign-in, which
  synchronizes, brings it back).
- Developer portal (`developer/session.rs`): an answer that is not a plist (QH) or not JSON with an HTTP
  error or an HTML body (v1), the 503 page, is `Error::DeveloperHttp { status, body }` ("Apple developer
  services answered HTTP 503 without a usable reply: ..."), not `Plist error` / `Serde JSON error`.
  `developer_endpoint!` goes through `developer::developer_url()`, which a test can point at a local portal.
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
- A seam for the tests: GrandSlam URLs are built by `auth::gsa_url(path)` and the developer portal's by
  `developer::developer_url(endpoint)`; under `cfg(test)` a thread-local can point them at a local server.
  Nothing changes in a normal build.
- Tests (`cargo test -p plume_core`): header pairing, client info untouched / Xcode replaced, header
  case, hsc 434 / 409 decisions, sim as string or data, HTML 503 / odd bodies, retry policy against a
  local server (429 three times, 5xx once), SMS 403, SRP protocols, code normalisation, the `InitPlan`
  (bounded, once per identity); and `auth/account/flow_tests.rs`, the whole `Account::login_with_anisette`
  against local imitations of GrandSlam (real SRP, encrypted SPD, 2FA endpoints, Apple's provisioning
  service) and of an anisette server (HTTP and the provisioning socket): code already pushed (no second
  push), synchronization and the use of the new one-time password by the following requests, headers of
  every request, SMS refused (back to the device prompt / clear error for an SMS-only account), shed
  requests retried, 503 reported after the retry, 503 that makes the other anisette server take over (and both
  refused: no loop), `-80045` reported and never repeated, `-45061` (a new identity is provisioned through
  the imitation, then synchronized; a provisioning that stays invalid is not made again for ever), `s2k` /
  `s2k_fo`, wrong password, wrong code, an Apple that keeps asking for codes; and the developer session
  against a local portal: headers of the portal requests, 503 pages reported as such, synchronization of a
  new identity the portal refuses (and none for an identity Apple knows, or when Apple does not ask).

## What tvsign parses

`app/plume.py` classifies the error lines of plumesign. The texts that matter, by Rust `Display`:
`Authentication SRP error -80045: ...`, `Apple GrandSlam answered HTTP 503 without a usable reply: ...`,
`Apple developer services answered HTTP 503 without a usable reply: ...` (and 429 for both),
`Apple answered HTTP 503 to the anisette provisioning: ...`, `SMS 2FA refused by Apple (HTTP 403: ...)`,
`Authentication SRP error -22411: ...`, `-22421` / `-22409` / `-80009`, `Anisette not provisioned!`,
`Anisette error: ...` (`... Apple only accepts Windows anisette ...` for a server that is not Windows),
and the prompt `Enter 2FA code from a trusted Apple device (SMS refused by Apple):`.
Changing one of these strings means changing `tests/test_hotfix.py` and `web/app.js` (`CAUSES`).

## Diagnostic: `plumesign probe-gsa` (hidden)

`crates/plume_core/src/auth/probe.rs`, `apps/plumesign/src/commands/probe.rs`, `commands/mod.rs`, `main.rs`

Sends the SRP `init` request of GrandSlam for a throwaway Apple ID (`tvsign-probe-...@example.com`, no
password, no `complete` step) with the anisette data of `--server` (used as given: no Android
replacement, no fallback, any platform; `--legacy-user-agent` sends the fixed macOS user agent of tvsign 1.0.0)
and prints the headers, `ec`/`em`/`hsc` and the HTTP status. A first answer hsc 434 is followed once (new
identity). At most two requests to Apple per run, no retry. An account-level answer (-20970, salt and
B...) means the configuration is accepted; -80045 or an HTML 503 means it is refused. `--state-dir` reuses
an identity (a second run on the same directory shows whether Apple still asks it to synchronize).
