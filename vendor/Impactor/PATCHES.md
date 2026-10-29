# tvsign patches on top of the vendored Impactor

VENDORED.txt says "do not edit by hand": the tree is regenerated from the Impactor fork
(branch `tvsign`). The changes below were made in place for the 1.0.1 and 1.0.2 hotfixes and the groups of
changes of 1.1. Port every one of
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

The lesson, which 1.0.2 applies and 1.1 keeps: never hard-code which platform Apple accepts. The data of a server go out with
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
  `https://`); `PLUMESIGN_ANISETTE_FALLBACK_URL` is the list of servers (URLs separated by commas, semicolons or
  spaces, which is how the default list is changed without a rebuild, the first default included: the day a default
  server is retired nothing has to be recompiled): behind the server requested when there is one, else the whole
  list instead of the defaults; `none` or `off` leaves the first server alone. tvsign computes the list itself
  (`settings.resolve_servers()`: the servers chosen in Settings, in order, then the `anisette_servers` list or the
  defaults, nothing behind them when "If this server fails" is off) and hands it to plumesign whole, the first
  server in `PLUMESIGN_ANISETTE_URL` and the others in `PLUMESIGN_ANISETTE_FALLBACK_URL` (`none` when there is no
  other): the diagnostic, `last-server.json` and plumesign then use one and the same list, whatever default list
  this build of plumesign has. No host is refused or replaced because of its name: 1.0.1's
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
  `LEGACY_STATE_FILES`) is taken over, not lost: a server that is not one of `DEFAULT_ANISETTE_SERVERS_V3` (the
  user's own, a self-hosted one, the one of the 1.0.1 workaround `PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM=1`) and has
  no identity file yet copies it to its own file (`load_state`; the most recent version's first), so its device
  identifier, still valid there, is kept and nothing is provisioned or synchronized again. The old files are
  deleted once a server has served headers with its identity (`drop_legacy_state`), or when a default server saves
  one (the defaults served other data then: they never adopt it, and it can be of use to nobody else). A server
  that does not know the identity answers `-45061` and gets a new one (below). Files are written
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
  variable. Trait method `AnisetteHeadersProvider::server_used()`. The servers that could not serve while the data
  were fetched (`AnisetteHeadersProvider::failed_servers()`: unreachable, an error, no provisioning) are left out
  of the sign-in the same way, so that a dead server costs its connect timeout once and not at every switch of
  server (a sign-in with two dead servers and four switches took about 80 s).
- Data of a conversation stay with one server: `AnisetteData::refresh()` (the data older than a minute, e.g. while
  the user types a 2FA code) fetches from the server that produced the data and from no other
  (`only_server_v3`), and an unreachable server is an error. Only `AnisetteData::new` (the start of a sign-in or of
  a portal session) and the explicit switches (`from_other_server`) may change server. Re-provisioning after a
  `-45061` of GrandSlam sticks to the server as well.
- Advanced escape hatch, empty by default: `PLUMESIGN_ANISETTE_CLIENT_INFO` and `PLUMESIGN_ANISETTE_USER_AGENT`
  (`ClientOverrides`, `AnisetteConfiguration::client_overrides()`), each on its own; a value that is not printable
  ASCII is ignored with a warning. They replace what the server announces everywhere it goes to Apple: the headers
  (so GrandSlam, 2FA, SMS and developer portal requests) and the requests of the provisioning. The log says what
  the server announced and what is sent instead. Three more values that followed the ecosystem are changeable
  without a rebuild, empty by default: `PLUMESIGN_XCODE_VERSION` (the `X-Xcode-Version`, `omnisette::xcode_version()`,
  default `XCODE_VERSION`), `PLUMESIGN_ANISETTE_BAG_CLIENT_INFO` (the Mac client info of the second URL bag lookup,
  default `URL_BAG_CLIENT_INFO`) and `PLUMESIGN_ANISETTE_CLIENT_ID` (the client identifier written in place of
  `com.apple.dt.Xcode`, default `com.apple.akd/1.0`; `keep`, `none` or `off` send the client info as announced). The
  user agent override also covers the fallback `DEFAULT_USER_AGENT` (used only when a provider reports none).
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
  rewritten (`PLUMESIGN_ANISETTE_CLIENT_ID` changes or switches off that replacement). `get_header()` no longer
  lower-cases values. `xcode_version()` is the one of omnisette (the provisioning requests send it too). `AnisetteData` knows the server that produced it (`server`), which the
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
    `-22409`, `-80009`, "MID is invalid". `-80045` is as likely the account, the IP address or a throttle as the
    data, and every other server means a new device identifier, a provisioning, a synchronization and two more
    `init` requests for one Apple ID within seconds, which feeds the throttle: it makes ONE switch at most per
    sign-in (`MAX_SWITCHES_FOR_80045`), and none when the refused server is the one in `last-server.json` (Apple
    accepted its data recently, `AnisetteData::from_remembered_server()`, `InitPlan::set_server_trusted`), then the
    answer is the error. The other refusals keep the cap of `MAX_SERVER_SWITCHES`. The server is
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
  `1100` that changes nothing; the data of a conversation (a refresh that keeps to its server and does not fail
  over to another one when it is down), the servers that failed to serve left alone by the later switches, a
  `-80045` that costs one other server at most (and none from the server Apple accepted last), the identity of an
  earlier version taken over by a server of the user's own (`omnisette` tests) and not by the defaults.

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
file `anisette/<server key>.plist` of the ONE server whose data Apple refuses (the `using Y instead` of the log
line, else the server of `last-server.json`, else the first one; `plume.server_key()` is `server_key()` ported,
once an hour per server) when the error is `-22409` / `-80009` / `-22421`: the other servers work, and a new
identity of each costs Apple requests. The error text shown to the user is made of the error lines only
(`plume._tail`), never of the WARN lines of a server switch, which the web interface would file under "Apple
blocked". The failover between servers is plumesign's alone: `plume._plume` runs a command once, with the whole list
in its environment, and copies into tvsign's journal what plumesign says about a server it left
(`plume.note_anisette_events`, one line per message, success or not). It does not run the command again with the
next server: for one Apple ID that is one more device identifier, provisioning and sign-in request to Apple per
server, and plumesign already keeps the data of a conversation with the server that produced them.
`app/settings.py` computes the list of servers (`resolve_servers`; the chosen servers, the `anisette_servers` list,
`anisette_fallback`), and after an update, at start and after a restore, it cleans once (`settings.migrate`): a
server of 1.0.0 (Android) still stored in the settings is cleared, and the servers chosen before the update keep
being used alone (`anisette_fallback` stored as `off`), as they were in 1.0.1.

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

## Anisette: TLS, timeouts and refresh (omnisette, plume_core)

`3rdparty/omnisette/src/remote_anisette_v3.rs`, new `src/apple_root.der`
(copy of `crates/plume_core/src/apple_root.der`)

This builds on the entries "Anisette servers" and "Sign-in" above (websocket scheme, provisioning
loop and its 60 s timeout, no `unwrap` on server or Apple answers, connect 10 s / request 30 s are
described there and exist once).

- TLS is verified. `make_apple_reqwest()` (URL bag lookup, start/finish provisioning) adds the Apple
  root. `make_reqwest()` (anisette server: `client_info`, `get_headers`) verifies with the system roots;
  a self-signed https home server needs `PLUMESIGN_ANISETTE_INSECURE=1`. (`adi_proxy.rs`, the local
  provider, is untouched.) The old blanket `danger_accept_invalid_certs(true)` is gone.

`crates/plume_core/src/auth/anisette_data.rs`, `developer/session.rs`, `auth/account/login.rs`

- `AnisetteData::refresh_in_place`, used by both `get_anisette()`: three tries with a pause (each try asks
  the server that produced the data and no other, see "Anisette servers": the one-time passwords of a
  conversation with Apple belong to one server), then the previous data are kept when they are still valid
  (under 90 s) instead of failing in the middle of a sign or a 2FA wait; data that are not valid any more
  are an error (Apple would only refuse them). After an abandoned refresh no new attempt is made for 30 s
  while the previous data are valid, so an outage does not add the retries to every Apple call.
- `crates/plume_core/src/lib.rs` `client()`: connect timeout 10 s, request timeout 60 s.

## Login (plume_core, plume_store)

- `auth/account/login.rs`: a wrong 2FA code (device or SMS; `is_bad_code`) is asked again (three tries)
  instead of ending the login; the code is reduced to its characters by `normalize_code` (see "Sign-in",
  `123 456`, `123-456`), an empty one counts as wrong. The prompt line is printed again, so tvsign's
  session goes back to "waiting".
- `crates/plume_store/src/store.rs`: `accounts_add_from_session` fails with `NO_TEAM:` (new
  `plume_core::Error::NoTeam`) when the Apple ID has no developer team, instead of saving an account
  with an empty team and failing at the first renewal.
- Tests: bad code detection.

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

## Unicast discovery (plume_utils, plumesign)

`crates/plume_utils/src/wireless/unicast.rs` (new), `tunnel.rs`, `discovery.rs`, `mod.rs`,
`apps/plumesign/src/commands/pair.rs`

Unlike the entries above, this one is also kept as `vendor/patches/0001-tvsign-unicast-discovery.patch`,
which `scripts/vendor-impactor.*` applies on top of a fresh export of the fork (and lists in
VENDORED.txt). The patch is made against the fork alone: its `pair.rs` and `tunnel.rs` hunks do not know
the changes of "Wireless pairing" above, so after those are ported to the fork, rebase the patch (the
script stops with "does not apply" until then). The merged tree is the reference.

- `unicast.rs`: a plain DNS query sent straight to UDP 5353 of one address (RFC 6762 legacy unicast) for
  `_remotepairing._tcp` / `_remotepairing-manual-pairing._tcp`; builds the same `DiscoveredDevice` the
  multicast browse would. `Hint { addr }` keeps the IPv6 scope id (`Hint::at`, `Hint::new`,
  `Hint::with_port`); the address of the answering device keeps it too.
- `tunnel.rs`: `locate_via` / `locate_and_connect_via` ask the hint first, then browse. One `judge()`
  decides what an announcement means for both paths: found, refused (the device asked for rejects our
  keys), or foreign (collected for `judge_foreign`, the "only device with that name" rule of "Wireless
  pairing"). The remembered `service_identifier` of the record is used by both.
- `pair.rs`: `pair discover --ip`, `--hint-ip` (a numeric scope is accepted: `fe80::1%12`) and
  `--hint-port` (hidden) on the targeted commands; `pair --ip` verifies the identifier through the same
  direct query.
- Environment `TVSIGN_UNICAST_HINT=0` (read by tvsign, not by plumesign) stops tvsign from passing a hint.
- Tests: DNS encoding/parsing, compressed names and pointer loops, assembling a device, a fake responder on
  loopback (direct answer, split PTR/SRV/TXT, silence), `locate_via` through a hint, a foreign tag never
  taken for our device.

## iPhone and iPad (plume_utils, plume_core, plumesign)

`crates/plume_utils/src/wireless/usb.rs` (new), `mod.rs`, `crates/plume_core/src/developer/platform.rs`,
`apps/plumesign/src/commands/pair.rs`, `apps/plumesign/src/commands/account.rs`

Like the unicast entry, the fork's `tvsign` branch does not know these changes: port them there before the
next `scripts/vendor-impactor` run, and rebase `vendor/patches/0001-tvsign-unicast-discovery.patch` over them
(its `pair.rs` and `mod.rs` hunks touch the same lines). The merged tree is the reference.

- `usb.rs`: `pair_usb` pairs an iPhone or iPad plugged in over USB (usbmuxd, iOS 17 or later), `select_device`
  picks the one device (or the one named by `--udid`), `UsbPairing` / `UsbDeviceInfo` carry what lockdown
  reported. The pairing is stored like a wireless one, without an identifier (the device is not on mDNS yet).
- `pair.rs`: `pair usb [-u UDID]` (one JSON line, then `PAIRED - <udid>`), `pair import FILE -u UDID` (stores a
  pairing file made elsewhere after checking that it parses), `pair devmode` (`DEVMODE on` / `DEVMODE off`
  through AMFI), and a `DeviceFamily` field (`tvos`, `ios`, `ipados`, `visionos`) in `pair info`.
- `platform.rs`: `DeveloperPlatform::from_name`; `account.rs`: `--platform ios|tvos` on `register-device`
  and `devices` (iOS when absent, as before the flag worked).
- Tests: family from class then model, import of a valid file and of garbage, platform names.

## Imported certificates (plume_core, plume_utils, plumesign)

`plumesign sign -p app.ipa --pem cert.pem key.pem --provision profile.mobileprovision -o out.ipa` is the
upstream route for a certificate of your own: no Apple session, no anisette, nothing sent to Apple. tvsign
uses it for the apps set to an imported certificate (`app/imported_cert.py` turns the .p12 into that PEM
pair). The wildcard profile that providers hand out did not work with it, so:

- `crates/plume_core/src/utils/mod.rs`: `resolve_wildcard` turns `TEAMID.*` and the prefix form
  `TEAMID.com.example.*` into `TEAMID.<application id>`. `merge_entitlements` uses it; the old rule replaced
  the star by the application id, which repeated the prefix of `TEAMID.com.example.*`
  (`TEAMID.com.example.com.example.app`). The result for `TEAMID.*` (the Apple ID route's keychain group) is
  unchanged.
- `crates/plume_core/src/utils/provision.rs`: `MobileProvision::resolve_wildcards(application_id)`.
  `merge_entitlements` only runs when the binary already carries entitlements; an unsigned binary (the usual
  IPA) has none, so the profile's `application-identifier = TEAMID.*` was signed as it was and installd
  refuses it.
- `crates/plume_utils/src/options.rs`, `signer.rs`: `SignerOptions::resolve_profile_wildcards`. When the merge
  fails and the flag is set, `sign_single_bundle` calls `resolve_wildcards` for each bundle (apps and
  extensions, each with its own identifier).
- `apps/plumesign/src/commands/sign.rs`: the flag is set when `--provision` is given. The `--apple-id` route
  never sets it, so what it signs is unchanged.
- Tests: `utils::tests` in plume_core (full wildcard, prefix wildcard, other strings, the merge). End to end,
  `tests/test_imported_cert.py` (`PlumesignEndToEnd`) signs a minimal unsigned tvOS IPA with a wildcard, a
  prefix and an exact profile, with an extension too, and reads the entitlements back (skipped when
  plumesign is not built).

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
