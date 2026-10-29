# tvsign patches on top of the vendored Impactor

VENDORED.txt says "do not edit by hand": the tree is regenerated from the Impactor fork
(branch `tvsign`). The changes below were made in place for the 1.0.1 and 1.0.2 hotfixes. Port every one of
them to the fork before the next `scripts/vendor-impactor.sh` run, otherwise they are lost.

Each entry names the files, what changed and why. Tests added with a change are listed.

## Why: what Apple accepts changes every few weeks (October 2026)

Since 2026-10-12 Apple's GrandSlam (`gsa.apple.com`) refuses a sign-in whose anisette data was generated
by an Android-based anisette-v3 server: error `-80045` ("This action cannot be completed at this time") at
the SRP `init`, then (2026-10-14) an HTML `503 Service Temporarily Unavailable` page for client infos
like `<Pixel 7> <Android;...>`. What worked then was Windows anisette from a v3 server that serves it
(`https://ani.jkcoxson.com`, `https://ani2.stikstore.app`), sent with the client info AND the user agent
the server reports at `GET <server>/v3/client_info`. A client info rewritten from that
(`com.apple.iTunes` -> `com.apple.akd`), a different user agent (the fixed macOS
`akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0` used to be sent), or stale one-time passwords all get the 503 page.
Reference implementation (MIT): [isideload](https://github.com/nab138/isideload), commits `9111948`,
`ac8976d`, `fdb1788`, `d424b17`, `8ca39b6`; the protocol handling below follows it.

Since about 2026-10-22 the servers serve macOS data (client info
`<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>`, user agent
`akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0`), `ani.jkcoxson.com` and `ani2.stikstore.app` included, and the Windows
servers were retired. 1.0.1 refused every server whose client info did not contain `Windows`, before sending
anything to Apple: every install broke the moment the servers changed. Checked live with `plumesign probe-gsa`
on 2026-10-28: Apple answers `-20970` (an answer about the account, so the anisette data are accepted) to the
macOS data of `https://ani.jkcoxson.com` and `https://ani.iloader.app`.

The lesson, which 1.0.2 applies: never hard-code which platform Apple accepts. The data of a server go out with
the pair the server announces; Apple's answer decides; the next server is tried when a server fails or Apple
refuses its data; all of it is visible (log lines, Settings > Diagnostics) and can be overridden without a rebuild
(the environment variables below).

## Anisette servers (omnisette)

`3rdparty/omnisette/src/lib.rs`, `remote_anisette_v3.rs`, `anisette_headers_provider.rs`, `Cargo.toml`

- Servers (1.0.2): `DEFAULT_ANISETTE_SERVERS_V3` is `https://mac-ani.jkcoxson.com`, `https://ani.iloader.app`,
  `https://new.ani.sidestore.io`, `https://ani3.stikstore.app`, `https://ani.jkcoxson.com`, tried in this order
  (`DEFAULT_ANISETTE_URL_V3` is the first). `resolve_servers(requested, fallback_override)`: nothing requested ->
  the defaults; a server requested (`PLUMESIGN_ANISETTE_URL` / `ANISETTE_URL_V3`, the user's own) -> tried first,
  the defaults behind it, each server once whatever the case or the trailing slash (an address without scheme is
  `https://`); `PLUMESIGN_ANISETTE_FALLBACK_URL` replaces what comes behind the first server (URLs separated by
  commas, semicolons or spaces, which is how the default list is changed without a rebuild; `none` or `off`
  leaves the first server alone). No host is refused or replaced because of its name: 1.0.1's
  `ANDROID_ANISETTE_HOSTS` (six hosts replaced by the default), the single `FALLBACK_ANISETTE_URL_V3`,
  `is_android_anisette_server()` and `url_host()` are gone.
  `AnisetteConfiguration`: `configured_servers_v3()`, `set_servers_v3()`, `set_anisette_url_v3()` (that server
  alone), `servers_v3()` (the order this sign-in knows), `ordered_servers_v3()` (the same, with the server
  remembered on disk when the sign-in has no preference yet), `refuse_server_v3()`, `only_server_v3()`,
  `client_overrides()`. Gone: `fallback_url_v3()`, `set_fallback_url_v3()`, `prefer_other_server_v3()`.
- No platform gate. `RemoteAnisetteProviderV3::serves_windows()`, `AnisetteError::UnsupportedServer` ("... Apple
  only accepts Windows anisette since 2026-10-12 ...") and `allow_any_platform()` are removed. Whatever a
  server announces, its data go out with its client info and user agent as a pair (and under no other pair).
  `PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM` is still accepted (one info line says it is ignored) and does nothing.
- Failover (`RemoteAnisetteProviderV3::headers_with_failover`, in the order of `ordered_servers_v3()`): a server
  that cannot be reached, answers an HTTP error, a page or bad JSON, fails to provision (socket error or
  timeout, Apple's provisioning service answering an HTML 503 or an error code), or keeps answering `-45061`
  after one new identity, gives way to the next one; only a disk problem on this machine ends the loop. A single
  server reports its own error, several report all of them (`AllServersFailed`, one `server: error` each).
  `identity_is_new()` describes the identity of the server that served the headers, not one made for a server
  that failed afterwards. What the sign-in does when Apple, not the server, refuses the data is in "Sign-in".
  `RemoteAnisetteProviderV3`: `new()` (one server), `with_servers()`, `with_fallbacks()`, `with_preferred()`,
  `with_overrides()`.
- One identity per server: `anisette/<server key>.plist` in the configuration folder (`STATE_DIR`,
  `state_path()`). The key (`server_key()`) is the lower-case host (the port when it is not 80/443, the path when
  there is one) with everything but letters, digits, dots and dashes written `_`: a file name that is safe, never
  a device name of Windows (`con`, `nul`, `com1`...), 80 characters at most with a digest when it is longer. The
  single identity of the earlier versions (`state.plist` of 1.0.0, `anisette-state-windows.plist` of 1.0.1,
  `LEGACY_STATE_FILES`) is not read any more and is deleted once an identity is saved. Files are written
  atomically; an unreadable one (or an identifier of the wrong length, which used to panic) means a new
  identity; `adi_pb` is optional in the file (an unprovisioned identity round-trips).
- The server that worked last: `anisette/last-server.json` (`LAST_SERVER_FILE`: `server`, `servers`, `at`).
  plume_core writes it when Apple accepted the data of a server (an answer about the account to the SRP `init`, the
  developer portal answering a session), `remember_server()`; `ordered_servers_v3()` reads it when the sign-in has
  no preference of its own, `remembered_server()`, so a dead server costs its connect timeout once, not at every
  command. It is only valid for the list of servers it was made for (`servers`): another server chosen in the
  settings, or other defaults in a new version, start from the top again. A server that only served headers is not
  remembered, nor is a server Apple refused. Nothing is written without a configuration folder.
- Within one sign-in, the server that served the last data is tried first by the next refresh
  (`AnisetteConfiguration::preferred_url_v3()`, `server_order()`) and the servers whose data Apple refused are left
  out (`refuse_server_v3()`). It lives in the configuration that travels with the data, not in a process-wide
  variable. Trait method `AnisetteHeadersProvider::server_used()`.
- Advanced escape hatch, empty by default: `PLUMESIGN_ANISETTE_CLIENT_INFO` and `PLUMESIGN_ANISETTE_USER_AGENT`
  (`ClientOverrides`, `AnisetteConfiguration::client_overrides()`), each on its own; a value that is not printable
  ASCII is ignored with a warning. They replace what the server announces everywhere it goes to Apple: the headers
  (so GrandSlam, 2FA, SMS and developer portal requests) and the requests of the provisioning. The log says what
  the server announced and what is sent instead.
- A server answer of `-45061` ("not provisioned") makes a NEW identity with that server (new random device
  identifier, as isideload does with `force_reset`) and provisions it, once, then asks again for the headers. The
  synchronization data (`sim`) of Apple belonged to the old identity, so the second request goes without
  them; GrandSlam asks the new identity to synchronize by itself. `omnisette::reset_identity(dir, server)` (used by
  plume_core when GrandSlam itself answers `-45061`) removes the identity with that server, and only that one; it
  used to keep the identifier (`invalidate_provisioning`), which fails for ever with a server that refuses to
  provision that identifier again. Other `GetHeadersError`s are an `AnisetteError::ServerError` (it used to be
  `panic!("Unknown error")`, which aborts the process in a release build). The status of the answer is not checked
  before reading its JSON, so an error status does not hide the `-45061` message.
- Apple's synchronization request: `get_headers` takes an optional `sim` (the `X-Apple-I-MD-DATA` of a
  GrandSlam answer with `hsc` 434) and sends `sim`, `device_id`, `md_lu` to the server. The `sim` belongs to the
  identity of the server that produced the data Apple asked to synchronize: it goes to that server and to no other
  (`AnisetteData::resync` gives the provider `only_server_v3()`). New trait methods of
  `AnisetteHeadersProvider`, all with defaults: `user_agent()` (the `user_agent` of `/v3/client_info`, or its
  override, next to the client info that was already returned), `identity_is_new()`,
  `resync_anisette_headers()`, `server_used()`.
- Provisioning: the loop used to spin at 100% CPU when the server closed the connection (`continue` on
  a `None`); it returns an error now, with a 60 s timeout for the whole session (tokio `time` feature).
  New server messages `Timeout`, `InvalidIdentifier`, `StartProvisioningError`, `EndProvisioningError`.
  Apple's answers to the lookup / start / finish requests are checked (an HTML 503 page ->
  `AnisetteError::AppleRefused`, `Response.Status.ec != 0` -> `ProvisioningFailed`) instead of `unwrap`.
  `http://` servers use `ws://` for the websocket. Timeouts on all clients: connect 10 s, request 30 s.
- The requests to Apple's provisioning service (URL bag lookup, start, finish) carry the same set of
  headers a GrandSlam request does, as isideload sends them: client info and user agent of the server (or the
  override) as a pair, `Accept: text/x-xml-plist`, `X-Xcode-Version`, `X-Apple-App-Info`, `Connection: close`, plus
  the identity (`X-Apple-I-MD-LU`, `X-Mme-Device-Id`, client time, time zone, locale). The URL bag is asked
  with the server's own client info first; when the bag lacks `midStartProvisioning` /
  `midFinishProvisioning` (or Apple answers an HTML refusal), it is asked once more with a Mac client info,
  which is what isideload always sends for this one request (an Android client info got a wrong bag). The
  start and finish requests keep the server's client info.
- Errors: `ServerError`, `ProvisioningFailed`, `AppleRefused { status, message }`, `ResyncUnsupported`,
  `AllServersFailed`.
- A test seam: `remote_anisette_v3::set_lookup_url_for_tests()` (hidden) points the URL bag lookup of the
  calling thread at a local imitation of Apple, so that the sign-in tests of plume_core can provision an
  identity without Apple. Nothing sets it in a normal run.
- Tests: server resolution (defaults in order, a server of the user first, no host judged by its name, scheme,
  `none` / `off` / lists), server order with a preference and refused servers, the server key, identities per
  server (isolation, legacy removal, reset of one, unreadable state), the memory of the server that worked (only for
  its list, folder-less, unreadable), overrides (each half alone, headers, provisioning requests), websocket URL,
  Apple error handling; against a local imitation of the anisette server (HTTP and the provisioning socket) and of
  Apple's provisioning service: any client info is used (macOS, Windows, Android, unknown), the order of the
  servers until one works (dead, HTML, error, good), the preferred server, the headers of the three provisioning
  requests, the second URL bag lookup, `-45061` (new identity, no `sim`, one provisioning and no loop, then the
  next server with an identity of its own), a server that cannot provision (the next one gets its own identity),
  the `identity_is_new` of the server that served. They need the feature set of `plumesign`:
  `cargo test -p omnisette -p plumesign -- --skip fetch_anisette_remote_v3 --skip fetch_anisette_ssc_async`
  (the two skipped tests use the network / Android libraries).

## Sign-in (plume_core, plumesign)

`crates/plume_core/src/auth/anisette_data.rs`, `auth/mod.rs`, `auth/account/{mod,login,token,two_factor_auth}.rs`,
`developer/{mod,session}.rs`, `lib.rs`, `Cargo.toml`; `apps/plumesign/src/commands/account.rs`

- Headers: GrandSlam, 2FA and developer-portal requests carry the client info of the anisette server
  unchanged (or the override) and its user agent (`AnisetteData::user_agent`, `grandslam_headers()`),
  `X-Xcode-Version` `27.0 (27A5218g)`, `X-Apple-App-Info`, `Accept: text/x-xml-plist`, `Connection: close` (the
  fixed macOS user agent, `Xcode` as developer-portal user agent and the `Accept: */*` are gone). Only a
  client identifier GrandSlam blocks (`com.apple.dt.Xcode`) is replaced by `com.apple.akd/1.0`; any other
  client info (the Windows `com.apple.iTunes/...` of 1.0.1's servers, the macOS one of today's) is never
  rewritten. `get_header()` no longer lower-cases values. `XCODE_VERSION` is the one of omnisette (the
  provisioning requests send it too). `AnisetteData` knows the server that produced it (`server`), which the
  identity, the synchronization and Apple's verdict belong to.
- The `init` step is run by a small plan (`InitPlan`, `login.rs`) so that no answer of Apple can make the
  sign-in loop: at most 16 `init` requests, 4 for the data of one server, at most 8 switches to another server,
  and each correction once per anisette identity.
  - hsc 434: when the answer is `Status.hsc == 434` with an `X-Apple-I-MD-DATA`, the data is relayed to
    the anisette server that produced the data (`AnisetteData::resync`), the new data replace the stored ones and
    the `init` is sent again with the same SRP ephemeral. A fresh anisette identity gets this answer on its first
    request, and Apple remembers the synchronization afterwards (checked: an identity synchronized by a previous
    process is not asked again).
  - `-45061` from GrandSlam at `init`: the identity with that server is forgotten and a new one made and
    provisioned (`reprovision_anisette`), once. A new identity has never been seen by Apple: it is asked to
    synchronize in its turn, which the first version of the hotfix did not allow (the second 434 became `-22421`
    and a reset of the identity by tvsign). The same holds when a resync made the server lose and recreate the
    identity (`AnisetteData::fresh_identity` of the new data).
  - Apple refusing the data of the server in use (`data_refusal()`): `-80045` at `init`, an HTML 503 that lasts
    (after the retries below), `-45061` again after the new identity, `-22421` again after the synchronization,
    `-22409`, `-80009`, "MID is invalid". `-80045` can also be the account or the connection, but the data of the
    next server are the only thing this sign-in can change, and each server gets one request for it. The server is
    left out for the rest of the sign-in (`AnisetteConfiguration::refuse_server_v3`), the data of the next server
    in the order are fetched (`AnisetteData::from_other_server`, `Account::switch_anisette_server`) and the `init`
    is sent again with the same SRP ephemeral; the new server has its own budget and its own corrections. A server
    that cannot synchronize or provision its identity (an anisette error) is left the same way. No other server
    left (a server chosen by the user without fallback, or all of them refused): the last answer of Apple is the
    error. An answer about the account (`-20970`, a wrong password...) is not a refusal: the data were accepted,
    that server is remembered (`AnisetteData::confirm_server`) and nothing else is tried. Log lines say which
    server was given up, why, and which one is used instead.
- Retries of GrandSlam requests that were shed (`send_with_retry`): HTTP 429 up to three times (2 s, 4 s,
  6 s, or `Retry-After`, 30 s at most), HTTP 502/503/504 once (3 s). A 503 that is a refusal of the data gives
  the same page every time, so more tries would only be more requests to Apple (they were three before); the
  edge also sheds good requests now and then, which one more try absorbs. A 429 is not a reason to change
  servers: Apple asks to slow down.
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
  synchronizes, brings it back). A portal that answers HTTP 503 (its page, not a plist) is refusing the data of
  the server in use, or is overloaded: the data of the next server are tried, once per server, at most 8 times
  (`new_synchronizing`); `1100` (session expired) is the portal talking about the session and does not change
  servers. The server whose data the portal accepted is remembered.
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
  For an account that has no trusted device it is the end of the sign-in, with a clear message. The client
  info override reaches these requests like all the others (iloader offers the same override for the 403).
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
  (bounded per server and in all, once per identity, the refusals that point at the data and the answers about the
  account that do not); and `auth/account/flow_tests.rs`, the whole `Account::login_with_anisette` and
  `Account::login` against local imitations of GrandSlam (real SRP, encrypted SPD, 2FA endpoints, Apple's
  provisioning service) and of anisette servers (HTTP and the provisioning socket): code already pushed (no second
  push), synchronization and the use of the new one-time password by the following requests, headers of
  every request whatever the servers announce (macOS, Windows, Android, unknown), the override on every request
  of a sign-in with 2FA and SMS, SMS refused (back to the device prompt / clear error for an SMS-only account),
  shed requests retried, 503 reported after the retry, 503 that makes the next anisette server take over (and
  all refused: no loop), `-80045` for the data of one server (the next server's data, no repeat to the same one),
  `-80045` from every server (one request each, then the error), servers tried in order (-80045, -80009, 503...)
  until Apple accepts one, a synchronization that does not take (-22421 again: the next server, the `sim` never
  sent to another server), `-45061` (a new identity is provisioned through the imitation, then synchronized; a
  provisioning that stays invalid is not made again for ever, and gives way to the next server), a dead server
  skipped before anything goes to Apple, the server Apple accepted remembered on disk (and not when it refused
  or when Apple did not answer about the account), an unknown Apple ID that does not make the sign-in change
  servers, `s2k` / `s2k_fo`, wrong password, wrong code, an Apple that keeps asking for codes; and the developer
  session against a local portal: headers of the portal requests, 503 pages reported as such, synchronization of
  a new identity the portal refuses (and none for an identity Apple knows, or when Apple does not ask), a portal
  that answers 503 to the data of a server (the next server, remembered; all refused: one request each) and a
  `1100` that changes nothing.

## What tvsign parses

`app/plume.py` classifies the error lines of plumesign. The texts that matter, by Rust `Display`:
`Authentication SRP error -80045: ...`, `Apple GrandSlam answered HTTP 503 without a usable reply: ...`,
`Apple developer services answered HTTP 503 without a usable reply: ...` (and 429 for both),
`Apple answered HTTP 503 to the anisette provisioning: ...`, `SMS 2FA refused by Apple (HTTP 403: ...)`,
`Authentication SRP error -22411: ...`, `-22421` / `-22409` / `-80009`, `Anisette not provisioned!`,
`Anisette error: ...` (`... no anisette server could be used: <server>: <error> | <server>: <error>` when every
server failed; the pages of the anisette servers themselves, `answered HTTP 502 without client info: ...`, are
not Apple's and `classify()` leaves them out),
and the prompt `Enter 2FA code from a trusted Apple device (SMS refused by Apple):`. The 1.0.1 text
`... Apple only accepts Windows anisette ...` does not exist any more.
Changing one of these strings means changing `tests/test_hotfix.py` and `web/app.js` (`CAUSES`).
`app/plume.py` also reads `anisette/last-server.json` (Settings > Diagnostics shows it) and removes the identity
files `anisette/*.plist` when Apple refuses the anisette data repeatedly.

## Diagnostic: `plumesign probe-gsa` (hidden)

`crates/plume_core/src/auth/probe.rs`, `apps/plumesign/src/commands/probe.rs`, `commands/mod.rs`, `main.rs`

Sends the SRP `init` request of GrandSlam for a throwaway Apple ID (`tvsign-probe-...@example.com`, no
password, no `complete` step) with the anisette data of `--server` (used as given: whatever it announces, no
fallback; `--client-info` / `--user-agent`, or `PLUMESIGN_ANISETTE_CLIENT_INFO` / `_USER_AGENT`, send another
pair; `--legacy-user-agent` sends the fixed macOS user agent of tvsign 1.0.0)
and prints the headers, `ec`/`em`/`hsc` and the HTTP status. A first answer hsc 434 is followed once (new
identity). At most two requests to Apple per run, no retry. An account-level answer (-20970, salt and
B...) means the configuration is accepted; -80045 or an HTML 503 means it is refused. `--state-dir` reuses
an identity (a second run on the same directory shows whether Apple still asks it to synchronize); the
identity is the one of that server in `<state-dir>/anisette/`.
